"""Recording controls: where the file goes, how it is encoded, and live status.

Every widget here is touched from the main thread only. The recorder's own
threads report back through a callback that only touches plain data, and the
panel's own threads either write plain data or hand their answer to this
thread by queued signal:

- _DiskProbe measures free space, because that measurement can block for half
  a minute and must not do so in a timer slot during a take.
- Encoder detection runs ffmpeg -encoders and then a test encode on each GPU,
  measured at 1.37 s at launch on the rig.
- The audio check lists the inputs (ffmpeg -list_devices, 0.30 s there) and
  then asks each which sample formats it offers (0.47 s for two inputs).

The last two used to run on this thread as the window came up, and everything
queued behind them waited as well -- the console's connection and the
camera's start among it; wer.ui.startup has the timeline. Neither may make a
Record press wait on its answer.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from wer.core.avsync import (
    CLAP,
    NO_SOUND,
    OFFSET_LIMIT_MS,
    TYPED,
    describe_offset,
    offset_for,
    set_offset,
    sync_legacy_offset,
    use_automatic,
)
from wer.core.showfile import RecordingConfig, render_filename
from wer.video.devices import (
    AudioDevice,
    AudioFormat,
    best_audio_format,
    default_audio_input_name,
    enumerate_audio_devices,
    probe_audio_formats,
)
from wer.video.encoder import (
    AUTO_ENCODER,
    HARDWARE_ENCODERS,
    QUALITY_PRESETS,
    SOFTWARE_ENCODER,
    Container,
    EncoderAvailability,
    OutputSettings,
    detect_encoders,
    remember_detected_encoders,
    resolve_encoder_name,
)
from wer.video.recorder import DiskSpace, RecorderState, check_disk
from wer.video.snapshot import SnapshotFormat

log = logging.getLogger(__name__)

__all__ = ["RecordPanel"]


#: Marks the "<name> -- not connected" row, so a later call can take it out
#: again instead of leaving a dead row per absent device.
_ABSENT_ROLE = Qt.ItemDataRole.UserRole + 1

#: How long the GUI thread will wait for a free-space measurement before
#: leaving it to finish on its own. A local disk answers in about 0.2 ms, so
#: in practice this is never spent; it exists so the readout still appears
#: immediately on every machine that is working.
_DISK_GRACE_SECONDS = 0.05

#: Leave at least this long between measurements. Free space moves by roughly
#: 4 GB an hour while recording; the point of refreshing mid-take is that the
#: number moves at all, not that it moves twice a second.
_DISK_INTERVAL_SECONDS = 1.0

#: After this long with no answer, stop showing the last number as though it
#: were current and say the volume is not responding instead.
_DISK_STALE_SECONDS = 5.0

#: Mid-take, a size measured longer ago than this is not shown as the size.
#: The recorder measures it every couple of seconds on its own threads, so a
#: figure this old means the writer is stuck or the drive has stopped
#: answering -- and a number that has quietly stopped moving is the readout
#: lying, the same failure _DISK_STALE_SECONDS exists for.
_SIZE_STALE_SECONDS = 15.0

_AMBER = "color: #e67e22; font-weight: bold;"

_DISK_NO_ANSWER = (
    "⚠ This drive is not answering, so free space cannot be measured. If the "
    "recordings folder is on a network share, the share has probably gone."
)


def _warning_for(stop_gb: float) -> float:
    """The low-space warning level that goes with a given stop level."""
    return max(stop_gb * 2, RecordingConfig.low_disk_warning_gb)


class _DiskProbe:
    """One free-space measurement, taken off the GUI thread.

    ``check_disk`` is not safe to call from a QTimer slot. Its ``try`` covers
    only ``shutil.disk_usage``; the ``Path.exists()`` walk above it is what
    blocks, and on a recording folder whose network share has dropped that
    walk was measured at 28.7 s before raising OSError -- against 0.2 ms on a
    local disk. Called inline it froze the whole window (preview, transport,
    the Stop Recording button) in half-minute blocks and put a traceback in
    the log every tick, for the rest of the take. A booth machine recording to
    \\\\server\\share that goes away mid-tech is not a hypothetical, and it is
    the moment the operator most needs the UI to answer.

    So the measurement happens here, on a throwaway daemon thread, and the
    panel renders whatever the last one that finished came back with.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = str(directory)
        self.started = time.monotonic()
        self.space: DiskSpace | None = None
        #: True when the volume raised rather than simply not being there.
        self.failed = False
        #: Written last, so a reader that sees it True sees ``space`` too.
        self.done = False
        self._thread = threading.Thread(
            target=self._run, name="wer-disk-probe", daemon=True
        )
        self._thread.start()

    def wait(self, seconds: float) -> None:
        self._thread.join(seconds)

    def _run(self) -> None:
        failed = False
        try:
            space = check_disk(Path(self.directory))
        except OSError as exc:
            # A drive pulled or a share gone is expected here, not
            # exceptional -- and this runs on a timer, so a traceback per
            # probe would bury the log for no gain. The panel says so in
            # words instead.
            log.debug("Could not measure free space on %s: %s", self.directory, exc)
            space, failed = None, True
        except Exception:  # noqa: BLE001 - a probe must never take the app down
            log.exception("Free-space check failed on %s", self.directory)
            space, failed = None, True
        self.space = space
        self.failed = failed
        self.done = True


class _Answer(QObject):
    """Carries one worker's answer to the interface thread.

    A signal emitted on a worker is queued to its receiver's thread, which is
    the hand-off the rest of the app relies on. The emitter is deliberately not
    the panel, nor anything the panel owns. The window can be closed while
    ffmpeg is still answering, and the panel is deleted with it; emitting a
    signal on a deleted object raises "Signal source has been deleted" on the
    worker. A parentless emitter held by the worker outlives the panel, and Qt
    drops a queued call whose receiver has gone. Both were checked on PySide6
    6.9.3, as was the answer still arriving once the worker has let go of its
    emitter.

    That last point means the emitter is destroyed on the worker, off the
    thread it was created on. Qt calls that unsafe for an object with events
    pending for it, timers or children, and this has none of them: a queued
    call is posted to its receiver, never to its sender. If a PySide upgrade,
    or a connection to the emitter's destroyed() signal, ever changes that, the
    likely symptom is a crash as the app closes. tests/test_app_lifecycle.py
    closes the window while detection is still running, to catch exactly that.
    """

    ready = Signal(object)


def _detect_encoders(answer: _Answer, detect) -> None:
    """Encoder detection, on the wer-encoder-detect thread.

    ``detect`` is looked up on the interface thread when the worker is started,
    and handed in. Looked up here, it would be read whenever this thread first
    got to run, and by then a test's stand-in can have been taken away again:
    conftest.windows_stay_off_real_hardware names that as the likely way real
    probes reached the hardware during a test run.
    """
    try:
        results = detect()
    except Exception:  # noqa: BLE001 - a probe must never take the app down
        # Answer anyway. The camera's automatic start is waiting for this, and
        # silence would cost it the whole start-up wait; a box that lists each
        # hardware encoder as unavailable for this reason says what happened.
        log.exception("Encoder detection failed; only software encoding is offered")
        results = [
            EncoderAvailability(SOFTWARE_ENCODER, True),
            *(
                EncoderAvailability(encoder, False, "detection failed; see the log")
                for encoder in HARDWARE_ENCODERS
            ),
        ]
    answer.ready.emit(results)


def _check_audio_inputs(
    listed: _Answer,
    checked: _Answer,
    *,
    configured: str | None,
    list_inputs,
    probe,
    formats: dict[str, AudioFormat | None],
    lock: threading.Lock,
) -> None:
    """List the audio inputs, then ask each for its formats. On wer-audio-probe.

    Plain data only: ``formats`` and ``lock`` are the panel's, written exactly
    as the format probe always wrote them. ``list_inputs`` and ``probe`` are
    handed in for the reason _detect_encoders gives.
    """
    try:
        devices = list(list_inputs())
    except Exception:  # noqa: BLE001 - a probe must never take the app down
        log.exception("Could not list the audio inputs")
        devices = []
    listed.ready.emit(devices)
    # The configured input first: it is the one a take will ask for, so if the
    # start-up wait runs out partway through, its format is the one to have.
    for device in sorted(devices, key=lambda each: each.name != configured):
        try:
            chosen = best_audio_format(probe(device))
        except Exception:  # noqa: BLE001 - a probe must never take the app down
            log.exception("Could not read the audio formats of %s", device.name)
            chosen = None
        with lock:
            formats[device.name] = chosen
        if chosen is None:
            log.info("Audio input %s stays on its own default format", device.name)
        else:
            log.info("Audio input %s will be asked for %s", device.name, chosen)
    checked.ready.emit(None)


class RecordPanel(QWidget):
    """Output settings, the record button, and live encoder health."""

    #: Emitted when the user presses Record / Stop.
    record_requested = Signal()
    stop_requested = Signal()
    #: Emitted whenever a setting changes, so the show file can autosave.
    settings_changed = Signal()
    #: Encoder detection has answered, and the Encoder box shows what it found.
    encoders_detected = Signal()
    #: Every audio input has been listed and asked which formats it offers.
    audio_inputs_checked = Signal()
    #: The operator asked for the clap test.
    clap_test_requested = Signal()

    def __init__(
        self,
        config: RecordingConfig,
        parent: QWidget | None = None,
        *,
        detect_encoders_now: bool = True,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self._recorder = None
        #: What detection found, and whether it has answered yet. Until it has,
        #: the Encoder box shows a placeholder and cannot be changed.
        self._encoders: list[EncoderAvailability] = []
        self._encoders_pending = True
        self._detection_started = False
        #: Set for a first launch's audio check, which chooses an input once the
        #: inputs are listed; see _choose_first_input. What Windows named as its
        #: default recording device, looked up as the check starts.
        self._choosing_input = False
        self._windows_default_input: str | None = None
        #: The camera in use, by name. With the audio input it picks the A/V
        #: offset shown and armed; see set_camera and wer.core.avsync.
        self._camera_name = ""
        #: The measurement in flight, and the last one that came back.
        self._disk_probe: _DiskProbe | None = None
        self._disk_reading: _DiskProbe | None = None
        #: The audio inputs ffmpeg listed, or None until it has. The Audio box
        #: shows a placeholder and cannot be changed while this is None.
        self._audio_devices: list[AudioDevice] | None = None
        self._audio_checked = False
        #: The sample format each present audio input will be asked for, filled
        #: in off the GUI thread by _check_audio_inputs. No entry yet, or an
        #: entry of None, means the device is left on its own default.
        self._audio_formats: dict[str, AudioFormat | None] = {}
        self._audio_formats_lock = threading.Lock()
        self._audio_probe: threading.Thread | None = None

        # The settings scroll and the Recording box does not. Stacked, the five
        # boxes needed 1022 px, and a tab widget is as tall as its tallest
        # page, so the window could not be made shorter than 1106 px: taller
        # than the 1067 px Windows leaves on a 2560x1600 laptop at 150%, which
        # put the bottom of the window off the screen with no way to shrink
        # it. The Record button, the take's state and the encoder queue are
        # what matter mid-take, so they stay below the scroll, always in sight.
        settings = QWidget()
        column = QVBoxLayout(settings)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        column.addWidget(self._build_destination())
        column.addWidget(self._build_encoding())
        column.addWidget(self._build_markers())
        column.addWidget(self._build_snapshots())
        column.addStretch(1)
        self._settings_scroll = QScrollArea()
        self._settings_scroll.setWidgetResizable(True)
        self._settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._settings_scroll.setWidget(settings)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self._settings_scroll, 1)
        layout.addWidget(self._build_transport())

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        # On a worker, not in a zero-length timer. Deferred to the event loop,
        # detection stayed out of construction but still ran on this thread:
        # measured on the rig it held the window from 0.50 s to 1.87 s after
        # launch, with the console's connection and the camera's start queued
        # behind it. The main window passes detect_encoders_now=False and starts
        # detection itself once the camera has opened, and the audio check once
        # the cameras' formats are in; see start_encoder_detection and
        # start_audio_inputs.
        if detect_encoders_now:
            self.start_encoder_detection()
        self._refresh_disk()

    def attach(self, recorder) -> None:
        self._recorder = recorder

    def load_settings(self) -> None:
        """Sync every widget from the config.

        The inverse of the individual change handlers, and necessary the moment
        the config can be set from anywhere but this panel -- loading a show
        file does exactly that. Without it the checkboxes keep showing the old
        values while recording uses the new ones, which is the same divergence
        that bit the Eos settings panel.

        Signals are blocked throughout so that repopulating does not look like
        the user changing things and trigger an autosave of what we just read.
        """
        widgets = (
            self._folder, self._template, self._encoder_box, self._quality_box,
            self._resolution_box, self._container_box, self._audio_box,
            self._offset, self._auto_markers, self._embed,
            self._keep_sidecars, self._bus_log,
            self._snapshot_folder, self._snapshot_template,
            self._snapshot_format, self._snapshot_marks, self._stop_below,
        )
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self._folder.setText(str(self.config.resolved_directory()))
            self._refresh_folder_warning()
            self._template.setText(self.config.filename_template)
            self._show_offset()

            index = self._quality_box.findData(self.config.quality_preset)
            self._quality_box.setCurrentIndex(max(0, index))
            index = self._encoder_box.findData(self.config.encoder)
            if index >= 0:
                self._encoder_box.setCurrentIndex(index)
            size = (self.config.width, self.config.height)
            index = self._resolution_box.findData(size if all(size) else None)
            self._resolution_box.setCurrentIndex(max(0, index))
            index = self._container_box.findData(self.config.container)
            self._container_box.setCurrentIndex(max(0, index))
            self._select_audio()

            self._auto_markers.setChecked(self.config.auto_markers)
            self._embed.setChecked(self.config.embed_markers)
            self._keep_sidecars.setChecked(self.config.keep_sidecar_files)
            self._bus_log.setChecked(self.config.write_bus_log)

            self._snapshot_folder.setText(
                str(self.config.resolved_snapshot_directory())
            )
            self._snapshot_template.setText(self.config.snapshot_template)
            index = self._snapshot_format.findData(self.config.snapshot_format)
            self._snapshot_format.setCurrentIndex(max(0, index))
            self._snapshot_marks.setChecked(self.config.snapshot_marks_recording)
            self._stop_below.setValue(int(self.config.low_disk_stop_gb))
        finally:
            for widget in widgets:
                widget.blockSignals(False)

        self._keep_sidecars.setEnabled(self.config.embed_markers)
        self._update_example()
        self._update_snapshot_example()
        self._quality_hint_update()
        self._refresh_disk()

    # ----------------------------------------------------------- destination

    def _build_destination(self) -> QWidget:
        box = QGroupBox("Where recordings go")
        form = QFormLayout(box)

        row = QHBoxLayout()
        self._folder = QLineEdit(str(self.config.resolved_directory()))
        self._folder.setToolTip("Recordings are written here.")
        self._folder.editingFinished.connect(self._folder_typed)
        row.addWidget(self._folder, 1)

        browse = QPushButton("Browse...")
        browse.clicked.connect(self._choose_folder)
        row.addWidget(browse)

        reveal = QPushButton("Open")
        reveal.setToolTip("Open this folder in Explorer.")
        reveal.clicked.connect(self._open_folder)
        row.addWidget(reveal)
        form.addRow("Folder:", row)

        self._folder_warning = QLabel()
        self._folder_warning.setWordWrap(True)
        self._folder_warning.setStyleSheet("color:#e0a03a;")
        form.addRow("", self._folder_warning)

        self._template = QLineEdit(self.config.filename_template)
        self._template.setToolTip(
            "Tokens: {show} {date} {time} {take}.\n"
            "Illegal filename characters are replaced automatically."
        )
        self._template.textChanged.connect(self._template_changed)
        form.addRow("Filename:", self._template)

        self._example = QLabel()
        self._example.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow("Will be:", self._example)

        self._disk = QLabel()
        self._disk.setWordWrap(True)
        form.addRow("Disk:", self._disk)

        self._stop_below = QSpinBox()
        self._stop_below.setRange(1, 500)
        self._stop_below.setSuffix(" GB")
        self._stop_below.setValue(int(self.config.low_disk_stop_gb))
        self._stop_below.setToolTip(
            "Recording stops automatically when free space reaches this "
            "level.\n\n"
            "The remaining space is not spare: the file has to be closed, and "
            "embedding the markers rewrites it, which briefly needs its size "
            "again. Stopping with room in hand is what keeps the recording "
            "intact."
        )
        self._stop_below.valueChanged.connect(self._stop_below_changed)
        form.addRow("Stop recording at:", self._stop_below)

        self._update_example()
        return box

    def _choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose where recordings go", self._folder.text()
        )
        if chosen:
            self._folder.setText(chosen)
            self._folder_typed()

    def _folder_typed(self) -> None:
        self.config.directory = self._folder.text().strip()
        self._refresh_disk()
        self._refresh_folder_warning()
        self._update_example()
        self.settings_changed.emit()

    def _refresh_folder_warning(self) -> None:
        """Say so, on the panel, when the folder is not somewhere usable.

        Finding this out at the end of a tech is the expensive way.
        """
        problem = self.config.directory_problem()
        self._folder_warning.setText(
            f"This folder is not there: {problem}." if problem else ""
        )
        self._folder_warning.setVisible(bool(problem))

    def _open_folder(self) -> None:
        import os
        import subprocess

        directory = self.config.resolved_directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            startfile = getattr(os, "startfile", None)
            if startfile is not None:
                startfile(str(directory))
            else:  # pragma: no cover
                subprocess.run(["xdg-open", str(directory)], check=False)
        except OSError as exc:
            QMessageBox.warning(self, "Could not open folder", f"{directory}\n\n{exc}")

    def _stop_below_changed(self, value: int) -> None:
        previous = self.config.low_disk_stop_gb
        self.config.low_disk_stop_gb = float(value)
        # Keep the warning comfortably above the stop, so there is notice
        # rather than a stop out of nowhere.
        #
        # Derived from the stop level, not ratcheted up from whatever it
        # already was. It used to be max(previous, value * 2), and since
        # nothing else in the app writes this field and no widget can lower it,
        # it only ever climbed: a spinbox emits on every keystroke, so typing
        # 150 and correcting it to 15 passed through 1, 15, 150 and pinned the
        # warning at 300 GB in the show file for good. Every take afterwards
        # then opened with a "Record anyway?" modal that nobody is in the booth
        # to answer. The floor is the shipped default read off the dataclass,
        # so the two cannot drift apart.
        #
        # Only recompute while the value still looks derived. No widget sets
        # the warning level, so the only way to have a deliberate one is to
        # hand-edit the show file -- which a human-readable file invites -- and
        # recomputing unconditionally threw that away the first time anyone
        # nudged this spinbox, silently and in the direction of less warning.
        derived = _warning_for(previous)
        if self.config.low_disk_warning_gb == derived:
            self.config.low_disk_warning_gb = _warning_for(float(value))
        # A running take re-reads these thresholds every ten seconds on the
        # writer thread, so push them through and the floor on screen is the
        # floor being enforced. They were only ever copied in at arm time,
        # which left this spinbox -- and its tooltip, which says recording
        # stops automatically at this level -- describing a number the recorder
        # was not using. Raising it mid-take to protect a filling disk did
        # nothing at all.
        if self._recorder is not None:
            self._recorder.stop_below_bytes = int(
                self.config.low_disk_stop_gb * 1_000_000_000
            )
            self._recorder.low_disk_bytes = int(
                self.config.low_disk_warning_gb * 1_000_000_000
            )
        self._refresh_disk()
        self.settings_changed.emit()

    def _template_changed(self, text: str) -> None:
        self.config.filename_template = text
        self._update_example()
        self.settings_changed.emit()

    def _update_example(self, show_name: str = "", take: int = 1) -> None:
        name = render_filename(
            self.config.filename_template, show_name=show_name, take=take
        )
        self._example.setText(f"{name}.{self.config.container}")

    def _is_recording(self) -> bool:
        return self._recorder is not None and self._recorder.is_recording

    def _refresh_disk(self) -> None:
        """Show free space, without ever measuring it on the GUI thread.

        See _DiskProbe: the measurement can block for half a minute and then
        raise, and this is called from a 500 ms timer slot -- during a take,
        now, which is the whole point of the readout. So a probe runs on its
        own thread and this only renders the answer.
        """
        directory = str(self.config.resolved_directory())
        probe = self._disk_probe
        if probe is not None and probe.done:
            self._disk_reading, self._disk_probe, probe = probe, None, None
        elif probe is not None and probe.directory != directory:
            # The folder changed while a measurement was in flight. Its answer
            # is about the volume we have just left, so let it fall away.
            self._disk_probe, probe = None, None

        now = time.monotonic()
        reading = self._disk_reading
        if probe is None and (
            reading is None
            or reading.directory != directory
            or now - reading.started >= _DISK_INTERVAL_SECONDS
        ):
            probe = _DiskProbe(Path(directory))
            # A working disk answers inside the grace, so the number appears
            # at once the way it always has; a dead one costs 50 ms and is
            # left to the thread.
            probe.wait(_DISK_GRACE_SECONDS)
            if probe.done:
                self._disk_reading, reading, probe = probe, probe, None
            else:
                self._disk_probe = probe

        # Nothing below here may show a number as though it were current when
        # it is not. A frozen "528 GB free" on a share that has gone is the
        # readout lying, which is the failure this line exists to prevent.
        stale = probe is not None and now - probe.started >= _DISK_STALE_SECONDS
        if reading is None or reading.directory != directory:
            self._disk.setText(_DISK_NO_ANSWER if stale else "Checking free space…")
            self._disk.setStyleSheet(_AMBER if stale else "")
            return

        space = reading.space
        if space is None:
            # Distinguish a drive that is simply not plugged in -- check_disk
            # says so by returning None, and the folder warning above already
            # covers it -- from one that took the measurement and never came
            # back, which is a share that has dropped mid-tech.
            if reading.failed:
                self._disk.setText(_DISK_NO_ANSWER)
                self._disk.setStyleSheet(_AMBER)
            else:
                self._disk.setText("Free space could not be determined.")
                self._disk.setStyleSheet("")
            return

        if stale:
            self._disk.setText(_DISK_NO_ANSWER)
            self._disk.setStyleSheet(_AMBER)
            return

        text = f"{space.free_gb:.1f} GB free"
        # Estimate at a rough 4 GB/hour for 1080p at Standard. Approximate on
        # purpose - the useful signal is "hours", not a byte count.
        hours = space.hours_at(4000)
        text += f"  (about {hours:.0f} hours at 1080p Standard)"
        if space.free_gb <= self.config.low_disk_stop_gb:
            # This line is reachable mid-take now that it keeps up, so it has
            # to make sense there: "recording will not start" printed under a
            # red Stop Recording button, next to a state label reading
            # Recording, contradicts itself at the exact moment the readout
            # finally became live. During a take the recorder is re-reading
            # this same floor on its writer thread every ten seconds and is
            # already stopping.
            tail = (
                "this take is being stopped."
                if self._is_recording()
                else "recording will not start."
            )
            self._disk.setText(f"⚠ {text} — at or below the stop level; {tail}")
            self._disk.setStyleSheet("color: #c0392b; font-weight: bold;")
        elif space.free_gb < self.config.low_disk_warning_gb:
            self._disk.setText(
                f"⚠ {text} — low for a full tech. Recording stops at "
                f"{self.config.low_disk_stop_gb:.0f} GB."
            )
            self._disk.setStyleSheet(_AMBER)
        else:
            self._disk.setText(text)
            self._disk.setStyleSheet("")

    # -------------------------------------------------------------- encoding

    def _build_encoding(self) -> QWidget:
        box = QGroupBox("Quality and format")
        form = QFormLayout(box)

        self._encoder_box = QComboBox()
        self._encoder_box.currentIndexChanged.connect(self._encoder_changed)
        self._show_placeholder(self._encoder_box, "Checking which encoders work…")
        form.addRow("Encoder:", self._encoder_box)

        self._quality_box = QComboBox()
        for preset in QUALITY_PRESETS:
            self._quality_box.addItem(preset.label, preset.key)
            self._quality_box.setItemData(
                self._quality_box.count() - 1, preset.description, Qt.ItemDataRole.ToolTipRole
            )
        index = self._quality_box.findData(self.config.quality_preset)
        self._quality_box.setCurrentIndex(max(0, index))
        self._quality_box.currentIndexChanged.connect(self._quality_changed)
        form.addRow("Quality:", self._quality_box)

        self._quality_hint = QLabel()
        self._quality_hint.setWordWrap(True)
        form.addRow("", self._quality_hint)

        self._resolution_box = QComboBox()
        # Recording resolution is independent of capture, so a 4K camera can be
        # recorded at 1080p without touching the camera settings.
        self._resolution_box.addItem("Same as camera", None)
        for width, height in ((3840, 2160), (1920, 1080), (1280, 720), (854, 480)):
            self._resolution_box.addItem(f"{width} x {height}", (width, height))
        current = (self.config.width, self.config.height)
        found = self._resolution_box.findData(current if all(current) else None)
        self._resolution_box.setCurrentIndex(max(0, found))
        self._resolution_box.currentIndexChanged.connect(self._resolution_changed)
        form.addRow("Record at:", self._resolution_box)

        self._container_box = QComboBox()
        self._container_box.addItem("MKV — survives a crash (recommended)", "mkv")
        self._container_box.addItem("MP4 — fragmented, for compatibility", "mp4")
        self._container_box.setCurrentIndex(0 if self.config.container == "mkv" else 1)
        self._container_box.currentIndexChanged.connect(self._container_changed)
        form.addRow("Container:", self._container_box)

        self._audio_box = QComboBox()
        self._audio_box.currentIndexChanged.connect(self._audio_changed)
        self._show_placeholder(self._audio_box, "Listing audio inputs…")
        form.addRow("Audio:", self._audio_box)

        self._offset = QSpinBox()
        self._offset.setRange(-OFFSET_LIMIT_MS, OFFSET_LIMIT_MS)
        self._offset.setSingleStep(10)
        self._offset.setSuffix(" ms")
        self._offset.setToolTip(
            "Positive delays the sound; negative delays the picture.\n"
            "Kept for each camera and audio input. Change it and it sticks for "
            "them; until then the automatic value is used."
        )
        self._offset.valueChanged.connect(self._offset_changed)
        self._offset_automatic = QPushButton("Use automatic")
        self._offset_automatic.setToolTip(
            "Forget the offset set for this camera and audio input, and use the "
            "automatic value again."
        )
        self._offset_automatic.clicked.connect(self._use_automatic_offset)
        self._clap_test = QPushButton("Clap test…")
        self._clap_test.setToolTip(
            "Measure the offset from a take of someone clapping on stage."
        )
        # A bound method, not a lambda. Measured 13 Sep 2026: with
        # `lambda: self.clap_test_requested.emit()` here, every test process
        # that built this panel exited 0xC0000409 after its tests had passed
        # (2 of 2), and with this method it exited cleanly (3 of 3). Why that
        # lambda and not the others in the layout editor was not established.
        self._clap_test.clicked.connect(self._request_clap_test)
        offset_row = QHBoxLayout()
        offset_row.setContentsMargins(0, 0, 0, 0)
        offset_row.addWidget(self._offset)
        offset_row.addWidget(self._clap_test)
        offset_row.addWidget(self._offset_automatic)
        offset_row.addStretch(1)
        form.addRow("A/V offset:", offset_row)
        # Where the number came from, in words. An offset nobody can see the
        # origin of is one nobody trusts, or one everybody trusts too much.
        self._offset_source = QLabel()
        self._offset_source.setWordWrap(True)
        form.addRow("", self._offset_source)
        self._show_offset()

        self._quality_hint_update()
        return box

    @staticmethod
    def _show_placeholder(box: QComboBox, text: str) -> None:
        """Say a list is still on its way, in a way that cannot become a setting.

        The first row added to an empty combo box becomes current, and that
        emits currentIndexChanged. Both boxes that use this write whatever is
        current straight into the show file -- the Encoder box as "libx264" for
        a row with no data, the Audio box as None -- so a placeholder added
        with signals live would replace a saved Quick Sync or UltraStudio
        choice the moment the panel was built, and the next autosave would keep
        it. The box is disabled as well, and _refresh leaves it so until the
        list is in: a placeholder is not something to choose.
        """
        blocked = box.blockSignals(True)
        try:
            box.addItem(text, None)
        finally:
            box.blockSignals(blocked)
        box.setEnabled(False)

    @property
    def encoders_pending(self) -> bool:
        """True until encoder detection has answered."""
        return self._encoders_pending

    @property
    def audio_inputs_pending(self) -> bool:
        """True until every audio input has been listed and asked for its formats."""
        return not self._audio_checked

    def start_encoder_detection(self) -> None:
        """Find out which encoders work, off this thread. Once only.

        From the constructor unless it was told not to. The main window starts
        it itself once the camera's automatic start is over, so that no test
        encode runs while the camera is opened; see
        MainWindow._detect_encoders_after_the_camera.
        """
        if self._detection_started:
            return
        self._detection_started = True
        answer = _Answer()
        answer.ready.connect(self._populate_encoders)
        thread = threading.Thread(
            target=_detect_encoders, args=(answer, detect_encoders),
            name="wer-encoder-detect", daemon=True,
        )
        thread.start()

    def _populate_encoders(self, results: list[EncoderAvailability]) -> None:
        """Fill the Encoder box from detection's answer. Main thread, queued signal."""
        self._encoders = list(results)
        self._encoders_pending = False
        # Hand the result to the encoder module so "auto" has something to
        # choose from. This is the only place the probe happens: doing it on
        # demand cost five seconds of startup, so until this runs "auto"
        # resolves to software.
        remember_detected_encoders(self._encoders)
        self._quality_hint_update()
        self._encoder_box.blockSignals(True)
        self._encoder_box.clear()

        # The default setting is "auto", and for a long time there was no row
        # for it. findData("auto") returned -1, max(0, -1) selected row zero,
        # and the panel sat there showing "Software (x264)" while recordings
        # went out on Quick Sync. It cost an afternoon to work out why a
        # three-hour stress run's settings file disagreed with the ffmpeg
        # command it had actually built.
        chosen = next(
            (r for r in self._encoders
             if r.encoder.name == resolve_encoder_name(AUTO_ENCODER)),
            None,
        )
        picked = chosen.encoder.label if chosen else "Software (x264)"
        if chosen and chosen.hardware:
            picked += f" on {chosen.hardware}"
        self._encoder_box.addItem(f"Automatic — {picked}", AUTO_ENCODER)
        self._encoder_box.setItemData(
            0,
            "Uses the best hardware encoder this machine actually has, and "
            "software if it has none. Checked at launch, so it follows the "
            "machine rather than needing to be set per venue.",
            Qt.ItemDataRole.ToolTipRole,
        )

        for result in self._encoders:
            self._encoder_box.addItem(str(result), result.encoder.name)
            index = self._encoder_box.count() - 1
            # An encoder that is present but unusable stays visible and
            # explains itself, rather than vanishing.
            self._encoder_box.model().item(index).setEnabled(result.available)
            if not result.available:
                self._encoder_box.setItemData(
                    index, result.reason, Qt.ItemDataRole.ToolTipRole
                )
        wanted = self._encoder_box.findData(self.config.encoder)
        self._encoder_box.setCurrentIndex(max(0, wanted))
        self._encoder_box.blockSignals(False)
        self._encoder_box.setEnabled(not self._is_recording())
        self.encoders_detected.emit()

    def start_audio_inputs(self, *, choose_an_input: bool = False) -> None:
        """List the audio inputs and ask each which formats it offers, off this thread.

        Why ask at all: ffmpeg takes whichever format DirectShow lists first,
        which on both inputs measured here is 44.1 kHz, so the Blackmagic's
        48 kHz HDMI audio was resampled in the driver on every take. Why at
        launch: it cannot wait until Record, because an OSC start must not
        stall on a question it could have asked earlier. Until an answer lands,
        output_settings asks for nothing and the device keeps its default,
        which is exactly the old behaviour.

        Why only when the main window says so, and not from the constructor:
        listing and probing are device queries, and on the Blackmagic rig they
        would otherwise overlap the camera format probe asking the same unit;
        see MainWindow._camera_formats_probed. Once only.

        ``choose_an_input`` is for a first launch, which starts on a microphone
        rather than on video only; see _choose_first_input.
        """
        if self._audio_probe is not None:
            return
        configured = self.config.audio_device
        if choose_an_input and configured is None:
            self._choosing_input = True
            # Asked here, on the interface thread, and not by the worker:
            # default_audio_input_name is COM, and says why that stays on this
            # thread. Handed to the worker as the configured input, so it is
            # the first asked for its formats, as a saved input would be.
            self._windows_default_input = default_audio_input_name()
            configured = self._windows_default_input
        listed, checked = _Answer(), _Answer()
        listed.ready.connect(self._populate_audio)
        checked.ready.connect(self._audio_inputs_answered)
        thread = threading.Thread(
            target=_check_audio_inputs,
            args=(listed, checked),
            kwargs={
                "configured": configured,
                "list_inputs": enumerate_audio_devices,
                "probe": probe_audio_formats,
                "formats": self._audio_formats,
                "lock": self._audio_formats_lock,
            },
            name="wer-audio-probe", daemon=True,
        )
        self._audio_probe = thread
        thread.start()

    def _populate_audio(self, devices: list[AudioDevice]) -> None:
        """Fill the Audio box from the inputs ffmpeg listed. Main thread, queued signal."""
        self._audio_devices = list(devices)
        chosen = self._choose_first_input() if self._choosing_input else None
        blocked = self._audio_box.blockSignals(True)
        try:
            self._audio_box.clear()
            self._audio_box.addItem("None — video only", None)
            for device in self._audio_devices:
                self._audio_box.addItem(device.name, device.name)
            self._select_audio()
        finally:
            self._audio_box.blockSignals(blocked)
        self._audio_box.setEnabled(not self._is_recording())
        # The input may have been chosen just now, on a first launch, or a saved
        # one may be missing: either way the offset shown is the one for the
        # input the Audio box now says.
        self._show_offset()
        if chosen is not None:
            # A choice, unlike filling the box, so it is saved -- and the next
            # launch, finding an autosave, is not a first launch.
            self.settings_changed.emit()

    def _choose_first_input(self) -> str | None:
        """Start a first launch on a microphone rather than on video only.

        A fresh install on a Surface Pro 8 (12 Sep 2026) came up on "None --
        video only", with the Surface's own microphone in the list and nothing
        on the Recording tab to say a take would be silent. Nothing had decided
        that None should be the default; it was only the empty setting.

        Windows' default recording device if it is listed: it is the one the
        machine's owner set in Sound settings. Otherwise the first input listed.

        Only on a first launch, and only once. After that the Audio box keeps
        whatever was last chosen, None included: None is a real choice as well
        (a booth recording the desk's sound separately), and once saved it
        cannot be told apart from never having chosen. For the same reason an
        input set by the time the list lands -- another show file opened
        meanwhile -- is left alone.
        """
        self._choosing_input = False
        if self.config.audio_device is not None:
            return None
        names = [device.name for device in self._audio_devices or []]
        windows_default = self._windows_default_input
        if windows_default in names:
            chosen, why = windows_default, "Windows' default recording device"
        elif names:
            chosen = names[0]
            why = (
                f"the first input listed, as Windows' default ({windows_default}) is not"
                if windows_default
                else "the first input listed, as Windows named no default"
            )
        else:
            log.info("First launch, and no audio input is listed, so takes are video only")
            return None
        self.config.audio_device = chosen
        log.info("First launch, so audio starts on %s: %s", chosen, why)
        return chosen

    def _audio_inputs_answered(self, _nothing: object) -> None:
        """Every input listed has been asked for its formats. Main thread, queued."""
        self._audio_checked = True
        self._label_audio_formats()
        self.audio_inputs_checked.emit()

    def _label_audio_formats(self) -> None:
        """Say what each input will actually be recorded at, on the box itself.

        The format was decided at launch and written to the log, and nowhere
        else. That is fine for a laptop microphone, which is stereo and stays
        stereo. It is not fine for an interface: a Focusrite Scarlett 4i4 that
        lists four channels is recorded with four, two of them silent if only
        one pair is patched, and a take can be cut before anyone notices. The
        opposite is worse -- a device whose stereo mode is chosen over its
        four-channel one loses inputs 3 and 4 outright, with the box still
        reading exactly as it did.

        So the panel says it, for the same reason the encoder box says which
        encoder it resolved: a panel that disagrees with the file is how an
        afternoon gets spent (see _fill_encoders).

        Only the text changes. The item's data is the device name and the rest
        of this panel selects on it.
        """
        with self._audio_formats_lock:
            formats = dict(self._audio_formats)

        blocked = self._audio_box.blockSignals(True)
        try:
            for index in range(self._audio_box.count()):
                name = self._audio_box.itemData(index)
                if not name:
                    continue                        # "None - video only"
                chosen = formats.get(name)
                if chosen is None:
                    # Left on the driver's own default, which is what happens
                    # when the probe found nothing usable. Saying so is better
                    # than an unqualified device name that implies a choice.
                    self._audio_box.setItemText(index, f"{name} — default format")
                    tip = (
                        "This input did not offer 48 kHz 16-bit, so Wer asks "
                        "for nothing and takes whatever the driver gives."
                    )
                else:
                    self._audio_box.setItemText(index, f"{name} — {chosen}")
                    tip = (
                        f"Recorded at {chosen}. "
                        + (
                            f"All {chosen.channels} channels go into the take; "
                            "any you have not patched are recorded silent."
                            if chosen.channels > 2
                            else "Stereo."
                        )
                    )
                self._audio_box.setItemData(index, tip, Qt.ItemDataRole.ToolTipRole)
        finally:
            self._audio_box.blockSignals(blocked)

    def _select_audio(self) -> None:
        """Point the Audio box at the configured device, present or not.

        An interface that is not plugged in today is not a choice to throw
        away, and it must not be hidden either. findData returns -1 for it and
        max(0, -1) used to select row zero, so the box read "None -- video only"
        while the recorder was still being handed the missing device -- and then
        refused to start, telling the operator to set Audio to None, which is
        exactly what the box was already showing. It also made the setting
        impossible to clear: "None" was already the current row, so choosing it
        emitted nothing, and on a booth PC whose only input was the unplugged
        interface there was no other row to go via.

        So a device that is not there stays listed and says so, the way an
        unusable encoder does. It cannot be re-picked while it is absent,
        "None" is now a real change, and the saved choice comes back by itself
        when the interface is plugged in again.
        """
        if self._audio_devices is None:
            # Not listed yet, so there is nothing to look the saved input up
            # in, and calling it "not connected" would be a guess. The
            # placeholder stays until _populate_audio, which comes back here.
            return
        wanted = self.config.audio_device
        # Never let this look like the user changing the setting, whoever
        # calls it: taking a row out can move the current index, and
        # _audio_changed would write that back over the config.
        blocked = self._audio_box.blockSignals(True)
        try:
            # Clear any placeholder an earlier call left. load_settings is the
            # one that can run twice -- that is what its docstring says it is
            # for, opening another show file -- and without this each absent
            # interface leaves its own dead " -- not connected" row behind, so
            # the list slowly fills with interfaces nobody is using.
            for row in range(self._audio_box.count() - 1, -1, -1):
                if self._audio_box.itemData(row, _ABSENT_ROLE):
                    self._audio_box.removeItem(row)

            index = self._audio_box.findData(wanted)
            if index < 0 and wanted:
                self._audio_box.addItem(f"{wanted} — not connected", wanted)
                index = self._audio_box.count() - 1
                self._audio_box.model().item(index).setEnabled(False)
                self._audio_box.setItemData(index, True, _ABSENT_ROLE)
            self._audio_box.setCurrentIndex(max(0, index))
        finally:
            self._audio_box.blockSignals(blocked)

    def _encoder_changed(self) -> None:
        self.config.encoder = self._encoder_box.currentData() or "libx264"
        self._quality_hint_update()
        self.settings_changed.emit()

    def _quality_changed(self) -> None:
        self.config.quality_preset = self._quality_box.currentData()
        self._quality_hint_update()
        self.settings_changed.emit()

    def _quality_hint_update(self) -> None:
        preset = next(
            (p for p in QUALITY_PRESETS if p.key == self.config.quality_preset), None
        )
        settings = self.output_settings()
        encoder = settings.resolve_encoder()
        text = preset.description if preset else ""
        self._quality_hint.setText(
            f"{text}  ({encoder.quality_flag.lstrip('-')} {settings.resolve_quality()})"
        )

    def _resolution_changed(self) -> None:
        data = self._resolution_box.currentData()
        self.config.width, self.config.height = data if data else (None, None)
        self.settings_changed.emit()

    def _container_changed(self) -> None:
        self.config.container = self._container_box.currentData()
        self._update_example()
        self.settings_changed.emit()

    def _audio_changed(self) -> None:
        self.config.audio_device = self._audio_box.currentData()
        self._show_offset()
        self.settings_changed.emit()

    def set_camera(self, name: str) -> None:
        """Say which camera is in use, so the offset shown is the one kept for it.

        By name, as the show file keeps the camera. Not a settings change:
        nothing the operator chose has changed.
        """
        if name == self._camera_name:
            return
        self._camera_name = name
        self._show_offset()

    def _show_offset(self) -> None:
        """Show the offset for the camera and audio input in use, and its origin.

        Written with signals blocked, so showing a value is never taken for the
        operator setting one.
        """
        offset = offset_for(self.config, self._camera_name)
        blocked = self._offset.blockSignals(True)
        try:
            self._offset.setValue(offset.value_ms)
        finally:
            self._offset.blockSignals(blocked)
        self._offset_source.setText(describe_offset(offset))
        self._offset_automatic.setVisible(offset.source in (TYPED, CLAP))
        self._offset.setEnabled(not self._is_recording() and offset.source != NO_SOUND)
        sync_legacy_offset(self.config, self._camera_name)

    def _offset_changed(self, value: int) -> None:
        """The operator set the offset. It sticks to this camera and audio input."""
        if not self.config.audio_device:
            return
        set_offset(self.config, self._camera_name, value, source=TYPED)
        self._show_offset()
        self.settings_changed.emit()

    def _use_automatic_offset(self) -> None:
        use_automatic(self.config, self._camera_name)
        self._show_offset()
        self.settings_changed.emit()

    def _request_clap_test(self) -> None:
        self.clap_test_requested.emit()

    def apply_clap_test(self, result) -> None:
        """Set the offset a clap test measured, for the devices its take used.

        ``result`` is a wer.ui.clap_dialog.ClapResult. Its take's camera and
        audio input, not the ones in use now: the operator may have switched
        since recording the claps.
        """
        set_offset(
            self.config, result.camera, result.offset_ms, source=CLAP,
            measured_lag_ms=result.lag_ms, recorded_offset_ms=result.recorded_offset_ms,
            claps=result.claps, frame_period_ms=result.frame_period_ms, audio=result.audio,
        )
        self._show_offset()
        self.settings_changed.emit()

    # --------------------------------------------------------------- markers

    def _build_markers(self) -> QWidget:
        box = QGroupBox("Markers")
        outer = QVBoxLayout(box)

        explain = QLabel(
            "Ctrl+M drops a marker with an optional note at any time while "
            "recording."
        )
        explain.setWordWrap(True)
        outer.addWidget(explain)

        self._auto_markers = QCheckBox("Drop a marker on every cue fire")
        self._auto_markers.setChecked(self.config.auto_markers)
        self._auto_markers.setToolTip(
            "Uses the console's own cue-fire event, which carries the cue "
            "number and label. Turn off to keep only your manual markers."
        )
        self._auto_markers.toggled.connect(self._auto_markers_changed)
        outer.addWidget(self._auto_markers)

        self._embed = QCheckBox("Put markers inside the video file")
        self._embed.setChecked(self.config.embed_markers)
        # MP4 is named in both tooltips because "one self-contained file" is
        # only true of MKV. An MP4 cannot hold attachments, so an MP4 take ends
        # with its marker CSV and bus log beside it however these switches are
        # set; see wer.video.remux._HOLDS_ATTACHMENTS.
        self._embed.setToolTip(
            "Writes the cue list into the video as chapters. An .mkv also "
            "carries the marker CSV and bus log inside it as attachments.\n\n"
            "The result is one self-contained file: open it in VLC and every "
            "cue is a chapter you can jump between. Costs a lossless rewrite "
            "of the file when the take ends.\n\n"
            "An .mp4 gets the chapters too, but cannot hold attachments, so "
            "its marker CSV and bus log stay beside it."
        )
        self._embed.toggled.connect(self._embed_changed)
        outer.addWidget(self._embed)

        self._keep_sidecars = QCheckBox("Also leave the .csv and .jsonl beside it")
        self._keep_sidecars.setChecked(self.config.keep_sidecar_files)
        self._keep_sidecars.setToolTip(
            "Off by default: with embedding on, the point is one file rather "
            "than four. They can always be pulled back out of an .mkv.\n\n"
            "An .mp4 cannot hold them, so an MP4 take keeps them beside it "
            "either way."
        )
        self._keep_sidecars.toggled.connect(self._sidecars_changed)
        outer.addWidget(self._keep_sidecars)

        self._bus_log = QCheckBox("Log all console data alongside the recording")
        self._bus_log.setChecked(self.config.write_bus_log)
        self._bus_log.setToolTip(
            "Records every value the console sent, timestamped against the "
            "video. It is what would let a different overlay be burnt onto "
            "this footage later without re-running the tech.\n\n"
            "Small: a few hundred KB per hour."
        )
        self._bus_log.toggled.connect(self._bus_log_changed)
        outer.addWidget(self._bus_log)

        self._marker_list = QListWidget()
        self._marker_list.setMaximumHeight(150)
        self._marker_list.setAlternatingRowColors(True)
        outer.addWidget(self._marker_list)
        return box

    def _auto_markers_changed(self, checked: bool) -> None:
        self.config.auto_markers = checked
        self.settings_changed.emit()

    def _bus_log_changed(self, checked: bool) -> None:
        self.config.write_bus_log = checked
        self.settings_changed.emit()

    def _embed_changed(self, checked: bool) -> None:
        self.config.embed_markers = checked
        self._keep_sidecars.setEnabled(checked)
        self.settings_changed.emit()

    def _sidecars_changed(self, checked: bool) -> None:
        self.config.keep_sidecar_files = checked
        self.settings_changed.emit()

    def show_markers(self, markers) -> None:
        """Refresh the marker list. Cheap enough to do on the status timer."""
        if self._marker_list.count() == len(markers):
            return
        self._marker_list.clear()
        for marker in markers:
            minutes, seconds = divmod(int(marker.timestamp), 60)
            self._marker_list.addItem(
                f"{minutes:>3}:{seconds:02d}   {marker.title}"
            )
        self._marker_list.scrollToBottom()

    def _build_snapshots(self) -> QWidget:
        box = QGroupBox("Snapshots")
        form = QFormLayout(box)

        explain = QLabel(
            "F12, or the Snapshot button on the Preview tab, saves a still of "
            "what you can see — overlay included. It works whether or not you "
            "are recording."
        )
        explain.setWordWrap(True)
        form.addRow(explain)

        row = QHBoxLayout()
        self._snapshot_folder = QLineEdit(
            str(self.config.resolved_snapshot_directory())
        )
        self._snapshot_folder.editingFinished.connect(self._snapshot_folder_typed)
        row.addWidget(self._snapshot_folder, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._choose_snapshot_folder)
        row.addWidget(browse)
        form.addRow("Folder:", row)

        self._snapshot_template = QLineEdit(self.config.snapshot_template)
        self._snapshot_template.setToolTip(
            "Tokens: {show} {date} {time} {take} {cue}\n"
            "{cue} is the cue that was live when you pressed it, which is "
            "usually the reason you took the picture."
        )
        self._snapshot_template.textChanged.connect(self._snapshot_template_changed)
        form.addRow("Filename:", self._snapshot_template)

        self._snapshot_example = QLabel()
        self._snapshot_example.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow("Will be:", self._snapshot_example)

        self._snapshot_format = QComboBox()
        for image_format in SnapshotFormat:
            self._snapshot_format.addItem(image_format.label, image_format.value)
        index = self._snapshot_format.findData(self.config.snapshot_format)
        self._snapshot_format.setCurrentIndex(max(0, index))
        self._snapshot_format.currentIndexChanged.connect(self._snapshot_format_changed)
        form.addRow("Format:", self._snapshot_format)

        self._snapshot_marks = QCheckBox(
            "Drop a marker when a snapshot is taken while recording"
        )
        self._snapshot_marks.setChecked(self.config.snapshot_marks_recording)
        self._snapshot_marks.setToolTip(
            "So the still and the moment in the video can be found from each "
            "other afterwards."
        )
        self._snapshot_marks.toggled.connect(self._snapshot_marks_changed)
        form.addRow("", self._snapshot_marks)

        self._update_snapshot_example()
        return box

    def _choose_snapshot_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose where snapshots go", self._snapshot_folder.text()
        )
        if chosen:
            self._snapshot_folder.setText(chosen)
            self._snapshot_folder_typed()

    def _snapshot_folder_typed(self) -> None:
        self.config.snapshot_directory = self._snapshot_folder.text().strip()
        self.settings_changed.emit()

    def _snapshot_template_changed(self, text: str) -> None:
        self.config.snapshot_template = text
        self._update_snapshot_example()
        self.settings_changed.emit()

    def _snapshot_format_changed(self) -> None:
        self.config.snapshot_format = self._snapshot_format.currentData()
        self._update_snapshot_example()
        self.settings_changed.emit()

    def _snapshot_marks_changed(self, checked: bool) -> None:
        self.config.snapshot_marks_recording = checked
        self.settings_changed.emit()

    def _update_snapshot_example(self, show_name: str = "", take: int = 1,
                                 cue: str = "58") -> None:
        name = render_filename(
            self.config.snapshot_template,
            show_name=show_name, take=take, cue=cue,
        )
        self._snapshot_example.setText(f"{name}.{self.config.snapshot_format}")

    # ------------------------------------------------------------- transport

    def _build_transport(self) -> QWidget:
        box = QGroupBox("Recording")
        outer = QVBoxLayout(box)

        row = QHBoxLayout()
        self._record_button = QPushButton("● Record")
        record_font = QFont()
        record_font.setPointSize(12)
        record_font.setBold(True)
        self._record_button.setFont(record_font)
        self._record_button.setMinimumHeight(44)
        self._record_button.clicked.connect(self._toggle)
        row.addWidget(self._record_button, 1)
        outer.addLayout(row)

        self._state_label = QLabel("Idle")
        outer.addWidget(self._state_label)

        self._detail_label = QLabel("")
        self._detail_label.setWordWrap(True)
        outer.addWidget(self._detail_label)

        depth_row = QHBoxLayout()
        depth_row.addWidget(QLabel("Encoder queue:"))
        self._queue_bar = QProgressBar()
        self._queue_bar.setMaximum(90)
        self._queue_bar.setTextVisible(True)
        self._queue_bar.setToolTip(
            "How far behind the encoder is. A rising queue is the early warning "
            "that frames are about to be dropped."
        )
        depth_row.addWidget(self._queue_bar, 1)
        outer.addLayout(depth_row)

        self._stats_label = QLabel("")
        self._stats_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        outer.addWidget(self._stats_label)
        return box

    def _toggle(self) -> None:
        if self._recorder is not None and self._recorder.is_recording:
            self.stop_requested.emit()
        else:
            self.record_requested.emit()

    # --------------------------------------------------------------- refresh

    def output_settings(self) -> OutputSettings:
        """Build encoder settings from the current configuration.

        Automatic is resolved here, to the encoder it means now, rather than
        handed on as "auto". The main window asks for these once when a take is
        armed and the recorder keeps them, but the recorder builds a new ffmpeg
        command for every part it restarts, and "auto" would be resolved again
        each time. Detection can land during a take: one started after the
        start-up wait ran out before detection answered (wer.ui.startup) goes
        out on software, and a part restarted once detection had found the GPU
        would come out on NVENC after parts on x264, with nothing but the
        ffmpeg line in the log to show for it. Resolved once, a take keeps one
        encoder.
        """
        with self._audio_formats_lock:
            chosen = self._audio_formats.get(self.config.audio_device or "")
        return OutputSettings(
            width=self.config.width,
            height=self.config.height,
            fps=self.config.fps,
            encoder=resolve_encoder_name(self.config.encoder),
            quality=self.config.quality,
            quality_preset=self.config.quality_preset,
            speed_preset=self.config.speed_preset,
            container=Container(self.config.container),
            audio_device=self.config.audio_device,
            audio_bitrate_kbps=self.config.audio_bitrate_kbps,
            av_offset_ms=offset_for(self.config, self._camera_name).value_ms,
            audio_sample_rate=chosen.rate if chosen else None,
            audio_sample_bits=chosen.bits if chosen else None,
            audio_channels=chosen.channels if chosen else None,
        )

    def _refresh(self) -> None:
        recorder = self._recorder
        if recorder is None:
            return

        state = recorder.state
        recording = recorder.is_recording

        self._record_button.setText("■ Stop Recording" if recording else "● Record")
        self._record_button.setStyleSheet(
            "background-color: #c0392b; color: white;" if recording else ""
        )
        # Changing the output mid-take would be meaningless; the ffmpeg process
        # is already committed to what it was given. That includes the A/V
        # offset, which is an argument in the running command: leaving it
        # editable invited nudging it to fix the lip sync of a take it cannot
        # reach, silently changing the next take instead.
        #
        # "Stop recording at" is deliberately NOT in this list. It is live
        # policy the recorder re-reads every ten seconds rather than part of
        # the command, and _stop_below_changed pushes it through, so an
        # operator watching a disk fill can still move it.
        for widget in (self._folder, self._template,
                       self._quality_box, self._resolution_box,
                       self._container_box,
                       self._embed, self._auto_markers, self._bus_log):
            widget.setEnabled(not recording)
        # With no sound there is nothing for an offset to line up.
        self._offset.setEnabled(not recording and bool(self.config.audio_device))
        self._offset_automatic.setEnabled(not recording)
        self._keep_sidecars.setEnabled(not recording and self._embed.isChecked())
        # Nor while their lists are still on the way: until then each shows a
        # placeholder, and enabling it here twice a second would offer that
        # placeholder as a choice (see _show_placeholder).
        self._encoder_box.setEnabled(not recording and not self._encoders_pending)
        self._audio_box.setEnabled(not recording and self._audio_devices is not None)

        self._state_label.setText(state.value)
        colour = {
            RecorderState.RECORDING: "#c0392b",
            RecorderState.STARTING: "#f39c12",
            RecorderState.STOPPING: "#f39c12",
            RecorderState.ERROR: "#c0392b",
        }.get(state, "")
        self._state_label.setStyleSheet(
            f"color: {colour}; font-weight: bold;" if colour else ""
        )
        self._detail_label.setText(recorder.detail)
        self._detail_label.setVisible(bool(recorder.detail))

        depth = recorder.queue_depth
        self._queue_bar.setMaximum(max(1, recorder.queue_capacity))
        self._queue_bar.setValue(depth)

        stats = recorder.stats
        if recording or stats.frames_written:
            bits = [
                _duration(stats.elapsed),
                f"{stats.frames_written} frames",
                f"{stats.write_fps:.1f} fps",
                _size_readout(stats, recording),
            ]
            if stats.frames_dropped:
                bits.append(f"⚠ {stats.frames_dropped} DROPPED")
            self._stats_label.setText("   ".join(bits))
            if stats.frames_dropped:
                self._stats_label.setStyleSheet("color: #c0392b; font-weight: bold;")
            else:
                self._stats_label.setStyleSheet("")
        else:
            self._stats_label.setText("")

        # Every tick, recording or not. This used to be skipped for the whole
        # take, so the "X GB free (about N hours)" line and both of its
        # warnings showed the headroom from the instant Record was pressed and
        # never moved again -- four hours and twenty gigabytes later, the tab
        # an operator might actually have open was still reporting 132 hours
        # left, and the amber and red warnings could never appear mid-take.
        # (The condition it hid behind, `self._timer.interval() < 2000`, was
        # dead: nothing anywhere changes the 500 ms interval.)
        #
        # This is only safe to do on the GUI thread because the measurement
        # itself no longer happens here -- see _DiskProbe. It is not
        # shutil.disk_usage that costs, it is the Path.exists() walk in front
        # of it, 28.7 s on a share that has dropped.
        self._refresh_disk()

    def note_show(self, show_name: str, take: int, cue: str = "") -> None:
        """Keep the filename examples honest as the show, take and cue change."""
        self._update_example(show_name, take)
        self._update_snapshot_example(show_name, take, cue or "58")


def _size_readout(stats, recording: bool) -> str:
    """The take's size as the recorder last measured it, or why there is none.

    Read, never measured: this runs in the 500 ms timer slot. It used to stat
    the take file from here, through RecordingStats.file_size_mb, which on a
    recordings folder whose network share has dropped is the same kind of
    call _DiskProbe was written to get off this thread -- and it kept doing it
    after the take ended, for as long as the stats line stayed up. The
    recorder now measures the size on its own threads, every part included,
    and this only has to say how old the figure is.
    """
    sampled = stats.size_sampled_at
    if recording:
        if not sampled:
            return "size not measured yet"
        # perf_counter, not monotonic: it has to be the recorder's clock.
        age = time.perf_counter() - sampled
        if age >= _SIZE_STALE_SECONDS:
            return f"size not measured for {age:.0f} s"
    return f"{stats.file_size_mb:.0f} MB"


def _duration(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"
