"""Main application window.

Owns the DataBus, the connections, the compositor and the recorder. Everything
in this module runs on the main thread; the only inbound path from worker
threads is :class:`~wer.ui.bus_bridge.BusBridge`.
"""

from __future__ import annotations

import logging
import os
import queue
import time
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QFontDatabase, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QMainWindow,
    QInputDialog,
    QMessageBox,
    QPlainTextEdit,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wer import APP_DISPLAY_NAME, APP_NAME, __version__
from wer.connections.base import ConnectionState
from wer.connections.builtin import ManualConnection, SystemConnection
from wer.connections.console_finder import ConsoleFinder
from wer.connections.eos import EosConnection, EosSettings
from wer.connections.sacn import new_cid
from wer.connections.sacn_sender import SacnSender, SacnSettings
from wer.connections.eos_parser import CueFired
from wer.core.buslog import BusLogger
from wer.core.databus import DataBus
from dataclasses import dataclass, field, replace

from wer.core.markers import Marker, MarkerKind, MarkerLog
from wer.core.instance import claim_settings
from wer.core.showfile import (
    ConsoleProfile,
    ShowFile,
    autosave_path,
    load_autosave,
    render_filename,
    save_autosave,
)
from wer.core.power import KeepAwake
from wer.logging_setup import log_file_path
from wer.overlay.compositor import Compositor
from wer.overlay.layout import LayoutSet, default_layouts
from wer.paths import (
    app_dir,
    build_id,
    ffmpeg_path,
    is_frozen,
    log_dir,
    resource_dir,
)
from wer.ui.bus_bridge import BusBridge
from wer.ui.clap_dialog import ClapResult, ClapTestDialog, TakeToMeasure
from wer.ui.connection_panel import STATE_COLOURS, ConnectionPanel, EosConnectionPanel
from wer.ui.sacn_panel import SacnPanel
from wer.ui.data_monitor import DataMonitorPanel
from wer.ui.help_window import HelpWindow
from wer.ui.layout_editor import LayoutEditor
from wer.ui.manual_panel import ManualPanel
from wer.ui.preview import PreviewPanel
from wer.ui.record_panel import RecordPanel
from wer.ui.startup import (
    AUDIO_INPUTS,
    CAMERA_FORMATS,
    StartupGate,
)
from wer import licensing
from wer.video.devices import VideoFormat
from wer.video.encoder import AUTO_ENCODER, OutputSettings
from wer.video.recorder import (
    CRITICAL_FREE_BYTES,
    SOUND_SHORT_BELOW,
    DiskWarning,
    Recorder,
    RecorderState,
    check_disk,
)
from wer.video.remux import EmbedControl, embed_chapters
from wer.video.snapshot import SnapshotFormat, save_snapshot

log = logging.getLogger(__name__)

#: How long shutdown waits for a take that is still being written. Long enough
#: for the recorder to flush, the sidecars to be written and a normal take's
#: chapter rewrite to finish; bounded because that rewrite runs an ffmpeg whose
#: stderr readline loop has no timeout of its own, and an ffmpeg that wedges
#: without exiting would otherwise hold the window open forever. Windows
#: force-terminates an app that stops pumping messages during logoff, which is
#: one of the three paths through closeEvent.
FINISH_JOIN_TIMEOUT = 45.0
#: Once that wait has run out and a rewrite still running has been stopped, how
#: long it gets to kill its ffmpeg and delete its temporary copy. Both take
#: milliseconds; the bound is for when they do not, and that case is logged.
FINISH_CANCEL_TIMEOUT = 3.0

#: How often a Record press that arrived mid-close retries. Short enough to be
#: imperceptible, long enough not to spin.
START_RETRY_INTERVAL_MS = 200
#: ...and how long it keeps trying before giving up and saying so.
START_RETRY_LIMIT = 45.0

#: Room a chapter rewrite must leave above the stop floor of a take recording
#: on the same drive. The rewrite writes a whole second copy of the file before
#: it gives any space back, and the take's own disk check -- every 10 s, on its
#: writer thread -- ends the take at the floor with nobody there to restart it,
#: seconds before the space would have come back. The guard that enforces this
#: looks every REWRITE_GUARD_INTERVAL; at the 542 MB/s this machine's NVMe
#: stream-copies a take, that is under 0.3 GB between looks, so 2 GB is caught
#: several looks before the floor, and the take's own few MB/s are lost in it.
REWRITE_HEADROOM_BYTES = 2_000_000_000
REWRITE_GUARD_INTERVAL = 0.5

#: Unacknowledged pinned messages kept for the status bar's tooltip. Enough for
#: a night of trouble; bounded so a desk macro failing on every cue cannot grow
#: it without end.
PINNED_MESSAGE_LIMIT = 10


@dataclass
class FinishedTake:
    """Everything the status bar is told about one take once it is on disk.

    Travels WITH the recording_finished signal rather than in attributes on the
    window, because more than one take can be finishing at once: a long chapter
    rewrite for take 7 is still running while take 8 is started and stopped,
    and a shared attribute would report take 7's trouble against take 8's file.
    """

    #: Every file the take produced that holds video, in order. One, unless
    #: ffmpeg died mid-take and the recording carried on in a new file.
    files: list[Path]
    #: Why the take ended, when it was not the operator's doing.
    failure: str = ""
    #: What could not be written alongside it.
    problem: str = ""
    #: Something true about where the take's files are that is not a fault:
    #: an MP4 take's marker list and bus log staying beside it. Said after
    #: "Saved" and never pinned, because pins are for what went wrong.
    note: str = ""
    #: Take times at which ffmpeg stopped and the take carried on in a new file.
    continued_at: list[float] = field(default_factory=list)
    #: Frames that never reached the file: turned away by the camera's encoder
    #: queue and by the recorder's own, whatever the reason.
    frames_dropped: int = 0
    #: Of frames_dropped, those the recorder lost to ffmpeg failing rather than
    #: to an encoder falling behind. See RecordingStats.
    frames_dropped_ffmpeg_failing: int = 0
    #: Whether ffmpeg failed more than once in a row at some point in the take.
    #: See _why_ffmpeg_lost_them.
    ffmpeg_kept_failing: bool = False
    frames_written: int = 0
    #: The share of its sound the audio input delivered, or None for a take
    #: without sound or too short to judge. See RecordingStats.
    sound_delivered: float | None = None
    #: What the take was recorded with, for the clap test, which measures a
    #: take and applies to the devices it used. The camera is the one in use as
    #: the take was handed over to be finished.
    camera: str = ""
    audio_device: str | None = None
    av_offset_ms: int = 0


def _volume_serial(path: Path) -> int | None:
    """The serial number of the volume holding ``path``, or None if unknowable.

    os.stat's st_dev is the volume serial on Windows (C: and D: on this machine
    read 5478185839323048960 and 6636671590357630424). Walks up to the nearest
    part of the path that exists, as check_disk does, because a take's folder
    may not have been created yet.
    """
    probe = Path(path)
    while True:
        try:
            return os.stat(probe).st_dev or None
        except OSError:
            if probe.parent == probe:
                return None
            probe = probe.parent


def _same_drive(first: Path, second: Path) -> bool:
    """Whether two paths could be competing for the same free space.

    By volume serial rather than drive letter: a folder can be the mount point
    of another disk, and one disk can have two letters. Unsure answers yes. The
    only question ever asked is "could this rewrite run that take out of
    room?", and a wrong no is exactly the failure being guarded against.
    """
    a, b = _volume_serial(first), _volume_serial(second)
    return a is None or b is None or a == b


def _why_ffmpeg_lost_them(kept_failing: bool) -> str:
    """Why frames lost to ffmpeg failing were lost, for a person to read.

    "Because ffmpeg kept failing" only for a take in which ffmpeg failed more
    than once in a row: the recorder's run of failures, which a part that
    records for HEALTHY_PART_SECONDS ends. A take the recorder gave up on has
    had such a run, because it gives up only after more than MAX_RESTARTS
    failures in a row. Otherwise "while ffmpeg was failing or restarting",
    which claims no more than that. It used to be chosen from how many files
    the take was in, so a take whose ffmpeg died twice, hours apart, and
    recorded properly after each, was said to have lost frames because ffmpeg
    kept failing -- what the soak take given up on after seven failures in a
    row was told.
    """
    if kept_failing:
        return "because ffmpeg kept failing"
    return "while ffmpeg was failing or restarting"


def _clock(seconds: float) -> str:
    """A take time for a person to read: 42:10, or 1:05:03."""
    whole = max(0, int(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _dim(label: QLabel, alpha: int = 150) -> None:
    """Render secondary text at reduced contrast.

    Not a stylesheet: `color: palette(mid)` resolves to a dark grey that is
    effectively invisible against a dark theme, and this app will spend its
    life on dark-themed machines in dark rooms. Deriving the colour from the
    widget's own text role with an alpha keeps it legible either way.
    """
    palette = label.palette()
    colour = palette.color(QPalette.ColorRole.WindowText)
    colour.setAlpha(alpha)
    palette.setColor(QPalette.ColorRole.WindowText, colour)
    label.setPalette(palette)


class MainWindow(QMainWindow):
    """Top-level window: preview, recording, data monitor, and connections."""

    #: Emitted from the finishing thread when a recording has been written,
    #: carrying a FinishedTake. Queued automatically, so the handler runs on
    #: the main thread. See FinishedTake for why the report travels with the
    #: signal rather than in attributes on the window.
    recording_finished = Signal(object)
    #: A command from the console arrived: a /wer/ address on the Eos link.
    #: Emitted from the Eos connection's thread and handled on the main thread,
    #: which is the only place any of these actions is safe.
    osc_command = Signal(str, object, float)
    #: Free space is running out. Emitted from the recorder's writer thread.
    disk_warning = Signal(object)
    #: The audio input is delivering too little of its sound. Emitted from the
    #: recorder's thread reading ffmpeg's sound statistics.
    sound_warning = Signal(str)
    #: The recorder gave up mid-take. Emitted from whichever thread noticed;
    #: handled on the main thread, which is the only place the UI can be put
    #: back to the truth.
    recorder_failed = Signal(str)
    #: Progress from the console search, which runs on its own thread.
    console_search_progress = Signal(str)
    #: The search finished: (profile or None, results).
    console_search_finished = Signal(object, list)
    #: How long the sACN output must stay in an error of its own before it is
    #: pinned. A network still coming up at launch or after sleep recovers
    #: inside this, and is not reported as sending having stopped.
    SACN_PIN_AFTER = 10.0

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_DISPLAY_NAME} {__version__}")
        self.resize(1220, 860)

        # Last session's settings. This is what makes the app come back up the
        # way it was left, including reconnecting to the console you used.
        # Whether this copy of Wer is the one allowed to write settings. A
        # second copy open at the same time would otherwise rewrite the whole
        # file from its own older in-memory state on every autosave, silently
        # discarding whatever the first had done -- which is how a layout built
        # during a session went missing.
        self.settings_claim = claim_settings()
        # Read before the autosave is loaded, for the one setting a first
        # launch chooses: the microphone (RecordPanel._choose_first_input). No
        # autosave is what a fresh install looks like. A corrupt one is not a
        # first launch, even though loading it starts from defaults.
        self._first_launch = not autosave_path().is_file()
        self.show_file: ShowFile = load_autosave()

        # One bus for the whole application. Connections publish to it; panels
        # read from it. They never reach each other directly.
        self.bus = DataBus()

        self._ensure_a_console_profile()

        self.finder = ConsoleFinder(
            on_progress=self.console_search_progress.emit,
            on_finished=lambda p, r: self.console_search_finished.emit(p, r),
        )
        self.console_search_progress.connect(self._on_search_progress)
        self.console_search_finished.connect(self._on_search_finished)

        self.system = SystemConnection("system", self.bus)
        # The take counter is loaded with everything else and used to stop
        # there: nothing ever pushed it into the connection that owns it. Every
        # launch went back to take001, so the afternoon session re-used the
        # morning's filenames (saved from overwriting only by the collision
        # suffix), and the next autosave wrote that 1 back over the stored
        # number.
        self.system.take = self.show_file.take
        self.manual = ManualConnection("manual", self.bus)
        self.eos = EosConnection(
            "eos1", self.bus, self._eos_settings_from_show(),
            on_cue_fired=self._on_cue_fired,
        )

        self._register_osc_commands()
        self.osc_command.connect(self._handle_osc_command)

        self.bridge = BusBridge(self.bus, parent=self)
        self.compositor = Compositor(self.bus)
        self._build_layouts()

        self.recorder = Recorder(
            on_state_change=self._on_recorder_state,
            on_disk_warning=self.disk_warning.emit,
            on_sound_warning=self.sound_warning.emit,
        )
        self.disk_warning.connect(self._on_disk_warning)
        self.sound_warning.connect(self._on_sound_warning)
        self.recorder_failed.connect(self._on_recorder_failed)
        #: The source identity a second copy of Wer sends with; see
        #: _sacn_settings_from_show.
        self._sacn_session_cid = ""
        # Wer's recording status on the lighting network. Built here and
        # started at the end of this method, only if it was switched on. It
        # asks _sacn_sees_recording before every packet, from its own thread.
        self.sacn = SacnSender(
            "sacn", self.bus, self._sacn_settings_from_show(),
            is_recording=self._sacn_sees_recording,
        )
        #: Since when the sACN output has been in an error of its own, and how
        #: many of Automatic's moves have been pinned. See _refresh_sacn_status.
        self._sacn_error_since: float | None = None
        self._sacn_moves_seen = 0
        self._pump_thread: threading.Thread | None = None
        self._pump_stop = threading.Event()
        #: Takes still being written. A list, not one thread: the chapter
        #: rewrite for a four-hour take runs for minutes, and the next take can
        #: be started, stopped and finished inside that. One slot meant the
        #: second finisher overwrote the first, and closeEvent then joined
        #: whichever happened to be there.
        self._finishers: list[threading.Thread] = []
        #: Set while a take is being CLOSED -- the seconds between "stop" and
        #: the recorder being free again. A new take genuinely cannot start
        #: inside it: Recorder.stop() is still draining the queue and waiting
        #: on ffmpeg's trailer, and Recorder.start() resets the very state
        #: stop() is working through. Deliberately narrower than "a finisher is
        #: alive": the chapter rewrite is minutes long and blocks nothing.
        self._closing_take = threading.Event()
        #: A Record press that arrived inside that window and is waiting it out.
        self._start_pending = False
        self._start_deadline = 0.0
        #: True from the top of closeEvent, so nothing reschedules itself into
        #: a window that is going away.
        self._shutting_down = False
        #: Holds off idle sleep for as long as a take is in progress. Driven
        #: by the _take_in_progress property, never directly.
        self._keep_awake = KeepAwake()
        #: True from arming a take until it has been handed to the finisher,
        #: however it ended. The recorder's own state cannot answer this: it
        #: goes to ERROR on its own, and something still has to close the take.
        self._take_in_progress = False
        #: Messages pinned on the status bar with no timeout that nobody has
        #: acknowledged yet, oldest first. The newest is on show and all of
        #: them are on the tooltip, so a second failure cannot bury the first.
        #: See _pin_status_message for what takes them down, and what does not.
        self._pinned_messages: list[str] = []
        #: "Closing the last take..." while a Record press is held, so the
        #: start can take it down again. Shown, not pinned: it is progress, and
        #: a pin would outlive the take it was about.
        self._hold_notice = ""
        #: The camera's encoder-drop counter as it stood when this take was
        #: armed. That counter lives as long as the camera, not the take, so
        #: without a starting point every take would be blamed for every frame
        #: the camera had turned away since it was opened.
        self._take_capture_stats = None
        self._take_capture_drops = 0
        self.recording_finished.connect(self._on_recording_finished)
        #: The take the clap test measures: the last one finished. See
        #: _open_clap_test.
        self._last_take: TakeToMeasure | None = None
        self._clap_dialog: ClapTestDialog | None = None

        self.markers = MarkerLog()
        self.buslog = BusLogger(self.bus)

        self._build_menus()
        self._build_central()
        self._build_status_bar()

        self.bridge.batch.connect(self.monitor.apply_batch)
        self.bridge.start()

        self.system.start()
        self.manual.start()
        self._restore_manual_fields()

        # Autosave is debounced: a settings panel can emit on every keystroke,
        # and writing the show file that often would be silly.
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(1500)
        self._autosave_timer.timeout.connect(self._autosave)

        # The panels were built from the show file, but anything that changes
        # the config afterwards must be reflected back into them.
        self.record_panel.load_settings()

        self._help: HelpWindow | None = None

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._refresh_status_bar)
        self._status_timer.start()

        if self.show_file.sacn.enabled:
            self.sacn.start()
        self._maybe_auto_connect()

    # ------------------------------------------------------------- show file

    def _ensure_a_console_profile(self) -> None:
        """A show file must always have at least one console to edit.

        A brand new install has none, and an empty list would leave the panel
        with nothing to show and no way to add anything to.
        """
        show = self.show_file
        if not show.consoles:
            show.consoles.append(
                ConsoleProfile(
                    name="This computer",
                    host=show.eos.host,
                    transport=show.eos.transport,
                    port=show.eos.port,
                    framing=show.eos.framing,
                    user=show.eos.user,
                    subscribe=show.eos.subscribe,
                )
            )
        if not show.active_console or not any(
            p.name == show.active_console for p in show.consoles
        ):
            show.active_console = show.consoles[0].name

    @property
    def active_console(self) -> ConsoleProfile:
        show = self.show_file
        return next(
            (p for p in show.consoles if p.name == show.active_console),
            show.consoles[0],
        )

    def _eos_settings_from_show(self) -> EosSettings:
        profile = self.active_console
        # EosSettings coerces the transport and framing strings back into
        # enums; JSON has no enums, and the dispatch compares with `is`.
        return EosSettings(
            host=profile.host,
            transport=profile.transport,
            port=profile.port,
            framing=profile.framing,
            user=profile.user,
            subscribe=profile.subscribe,
        )

    def select_console(self, name: str) -> None:
        """Switch to a saved console and reconnect to it."""
        if not any(p.name == name for p in self.show_file.consoles):
            return
        previous = self.show_file.active_console
        self.show_file.active_console = name
        was_running = self.eos.is_running
        self.eos.stop()
        # A different desk is about to answer for these keys. Anything the old
        # one published -- its cue label, its show name -- is now wrong, and
        # keys the new desk never sends would otherwise sit there looking
        # current forever. Only on a real change: re-selecting the same console
        # must not blank the overlay.
        if name != previous:
            self.eos.forget_published()
        self.eos.settings = self._eos_settings_from_show()
        # Both halves of the panel have to follow, not just the settings form.
        # A search that switches console behind the user leaves the dropdown
        # naming one desk while the fields below describe another, which is
        # worse than no display at all.
        self.eos_panel.select_profile(name)
        self.eos_panel.load_settings()
        if was_running:
            self.eos.start()
        self.request_autosave()
        log.info("Console switched to %r", name)

    # ------------------------------------------------------------ sACN output

    def _sacn_settings_from_show(self) -> SacnSettings:
        config = self.show_file.sacn
        if not config.cid:
            # Made once and kept with the other settings at the next autosave.
            config.cid = new_cid()
        cid = config.cid
        if not self.settings_claim.may_save:
            # A second copy of Wer, beside the first on the same settings.
            # Receivers tell sources apart by CID alone, so two copies sending
            # with one would look like a single source whose levels and sequence
            # numbers jump between two streams. This copy cannot save one of
            # its own, so it makes one for the session.
            if not self._sacn_session_cid:
                self._sacn_session_cid = new_cid()
            cid = self._sacn_session_cid
        return SacnSettings(
            universe=config.universe,
            address=config.address,
            priority=config.priority,
            per_address_priority=config.per_address_priority,
            adapter=config.adapter,
            adapter_name=config.adapter_name,
            source_name=config.source_name,
            cid=cid,
        )

    def _sacn_sees_recording(self) -> bool:
        """Whether the sACN status should pulse. Asked on the sender's thread.

        A take in progress whose recorder is putting picture into the file right
        now: not while it is starting, stalled, waiting to restart or stopping
        (Recorder.is_writing). Two reads, neither of which touches Qt.
        """
        return self._take_in_progress and self.recorder.is_writing

    def _sacn_settings_changed(self) -> None:
        """Apply the sACN tab: from the next packet where the stream allows,
        otherwise by ending the stream and starting a new one."""
        config = self.show_file.sacn
        settings = self._sacn_settings_from_show()
        if not config.enabled:
            if self.sacn.is_running:
                self.sacn.stop()
            self.sacn.settings = settings
        elif not self.sacn.is_running:
            self.sacn.settings = settings
            self.sacn.start()
        elif not self.sacn.update_settings(settings):
            self.sacn.stop()
            self.sacn.settings = settings
            self.sacn.start()
        self._sacn_error_since = None
        self.request_autosave()

    # -------------------------------------------------------- console search

    def find_console(self) -> None:
        """Try every saved console until one is actually sending."""
        if self.finder.is_running:
            return
        self.eos.stop()
        self.eos_panel.set_searching(True)
        self.finder.start(
            self.show_file.consoles,
            listen_seconds=self.show_file.eos.probe_seconds,
        )

    def _on_search_progress(self, message: str) -> None:
        self.eos_panel.show_search_progress(message)
        self.statusBar().showMessage(message, 6000)

    def _on_search_finished(self, profile, results: list) -> None:
        self.eos_panel.set_searching(False)
        if profile is None:
            summary = "; ".join(result.describe() for result in results)
            self.eos_panel.show_search_progress(
                "No console answered." + (f"  {summary}" if summary else "")
            )
            log.warning("Console search found nothing. %s", summary)
            return

        self.eos_panel.show_search_progress(f"Connected to {profile.name}.")
        self.select_console(profile.name)
        self.eos.start()

    def _maybe_auto_connect(self) -> None:
        """Connect to the console used last, falling through the rest if needed.

        Walking into a tech and finding the app already talking to the desk is
        the point. A laptop that moves between venues finds the right one by
        itself rather than needing its settings changed on arrival.

        Nothing happens until a console has connected at least once, so a fresh
        install does not go dialling addresses nobody chose.
        """
        config = self.show_file.eos
        if not config.auto_connect:
            return
        if not any(p.has_connected for p in self.show_file.consoles):
            log.info("No console has connected before; not searching on startup")
            return

        if config.try_all_on_startup:
            # No console-count condition here, deliberately. It used to require
            # more than one saved console, on the reasoning that searching a
            # list of one is the same as connecting to it. That stopped being
            # true when the search grew a second pass: pass one IS "dial the
            # saved settings", and pass two is the recovery sweep for a desk
            # that has moved. Requiring two consoles meant the one setup that
            # most needs recovering -- a single saved desk whose transport or
            # port changed -- was the one setup that never looked.
            log.info("Looking for a console among %d saved",
                     len(self.show_file.consoles))
            self.find_console()
        else:
            profile = self.active_console
            log.info("Connecting to %s (%s)", profile.name, profile.summary)
            self.eos.start()

    def _restore_manual_fields(self) -> None:
        for name, value in self.show_file.manual_fields.items():
            self.manual.set_field(name, value)

    def request_autosave(self) -> None:
        self._autosave_timer.start()

    def _autosave(self) -> None:
        if not self.settings_claim.may_save:
            # Another copy owns the settings. Everything else about this one
            # works; it simply does not get to overwrite them.
            return
        show = self.show_file
        settings = self.eos.settings
        show.active_console = self.active_console.name
        show.eos.host = settings.host
        show.eos.transport = settings.transport.value
        show.eos.port = settings.port
        show.eos.framing = settings.framing.value
        show.eos.user = settings.user
        show.eos.subscribe = settings.subscribe
        show.manual_fields = self.manual.fields()
        show.show_name = self.manual.fields().get("show", "")
        show.take = self.system.take
        # By name: an index moves when something is replugged, a name does not.
        device_name = self.preview.current_device_name()
        if device_name:
            show.camera.device_name = device_name
            fmt = self.preview.current_format()
            if fmt is not None:
                show.camera.width = fmt.width
                show.camera.height = fmt.height
                show.camera.fps = fmt.fps_max
                show.camera.fourcc = fmt.fourcc
        # Outside the check for a camera: a box ticked while none was found is
        # still the operator's choice, and is kept for when one is.
        show.camera.flip_horizontal = self.preview.flip_horizontal
        show.camera.flip_vertical = self.preview.flip_vertical
        show.layouts = self.layouts.to_list()
        show.active_layout = self.layouts.active_name
        save_autosave(show)

    # ------------------------------------------------------------ osc control

    def _register_osc_commands(self) -> None:
        """Map the commands a console macro sends to actions.

        They arrive on the Eos connection with the console's own traffic
        (connections/osc_control.py), and handlers run on that connection's
        thread, so every one of them does nothing but hand the address to the
        main thread through a queued signal. Starting a recording or touching a
        widget from a socket thread would be exactly the kind of cross-thread
        mistake Qt does not forgive.
        """
        def relay(address: str):
            def handler(message) -> None:
                argument = message.args[0] if message.args else None
                # The moment the packet arrived, taken HERE on the Eos thread
                # rather than once the queued signal reaches the main thread,
                # which can be busy for seconds (a camera reopened mid-take
                # blocks it while the driver negotiates). A marker timed from
                # the far side of that lands seconds after the macro was
                # pressed. The same reasoning as _on_cue_fired.
                #
                # A clock reading, not the take's elapsed time. Elapsed used to
                # be read here, and a marker sent straight after a start was
                # stamped before the start had run: on the previous take's
                # clock, 95 minutes into a take 40 minutes long, and dropped
                # from the chapters without a word. _handle_osc_command turns
                # the arrival into take time once the take it belongs to exists.
                self.osc_command.emit(address, argument, time.perf_counter())

            return handler

        for address in (
            "/wer/record/start",
            "/wer/record/stop",
            "/wer/record/toggle",
            "/wer/record/snapshot",
            "/wer/marker",
            "/wer/layout",
            "/wer/layout/next",
            "/wer/overlay/hide",
            "/wer/overlay/show",
        ):
            self.eos.commands.on(address, relay(address))

    def _handle_osc_command(
        self, address: str, argument, arrived_at: float | None = None
    ) -> None:
        """Act on a command from the console. Main thread, via a queued signal.

        ``arrived_at`` is the perf_counter reading taken when the packet
        arrived, or None for a direct call, which is timed as now. A command
        that does nothing says why in the log: that is where the help sends
        someone whose macro seemed to do nothing.
        """
        log.info("Console command: %s %r", address, argument)

        if address == "/wer/record/start":
            # prompt=False: no dialog. A remote start cannot wait for someone
            # to answer one, exactly as a remote marker cannot wait for someone
            # to type.
            # Same reasoning as _toggle_recording: during the recorder's
            # teardown is_recording is still True, and a macro that fires stop
            # then start back to back lands its start right there. Letting it
            # through to _start_recording is what gets it held rather than
            # silently dropped.
            if not self.recorder.is_recording or self._closing_take.is_set():
                self._start_recording(prompt=False)
            else:
                log.info("Console start ignored: already recording")
        elif address == "/wer/record/stop":
            if self.recorder.is_recording and not self._closing_take.is_set():
                self._stop_recording()
            elif self._closing_take.is_set():
                log.info("Console stop ignored: the take is already stopping")
            else:
                log.info("Console stop ignored: not recording")
        elif address == "/wer/record/toggle":
            self._toggle_recording(prompt=False)
        elif address == "/wer/record/snapshot":
            self.take_snapshot()
        elif address == "/wer/marker":
            # No dialog: a remote marker cannot wait for someone to type.
            if self.recorder.is_recording and not self._closing_take.is_set():
                note = str(argument) if isinstance(argument, str) else ""
                # Take time at the moment the packet arrived: elapsed now, less
                # however long the main thread took to get here. Never before
                # the take. A marker that arrived before this take's first
                # frame -- sent with the start that armed it -- lands where the
                # video begins, as elapsed does for any marker taken then.
                at = self.recorder.stats.elapsed
                if arrived_at is not None:
                    at = max(0.0, at - (time.perf_counter() - arrived_at))
                self.markers.add_manual(at, note)
                self.statusBar().showMessage(
                    f"Marker from the console{f': {note}' if note else ''}", 5000
                )
            else:
                # While a take closes, the recorder still reads as recording,
                # and a marker added then went into the closing take's log
                # after its end.
                why = (
                    "the last take is still closing"
                    if self.recorder.is_recording else "not recording"
                )
                log.info("Console marker ignored: %s", why)
                self.statusBar().showMessage(
                    f"Marker from the console ignored — {why}", 5000
                )
        elif address == "/wer/layout":
            if isinstance(argument, str) and argument.strip():
                self.switch_layout(argument.strip())
            else:
                log.info("Console layout ignored: it needs the layout's name")
        elif address == "/wer/layout/next":
            self._next_layout()
        elif address == "/wer/overlay/hide":
            self.compositor.panic_hide(True)
        elif address == "/wer/overlay/show":
            self.compositor.panic_hide(False)

    # ----------------------------------------------------------------- layout

    def _build_layouts(self) -> None:
        """Load layouts from the show file, or install the defaults.

        Usable before anything has been configured: a show file with no layouts
        in it gets Tech, Minimal and None rather than a blank overlay, and opens
        on Tech.
        """
        self.layouts = LayoutSet.from_list(
            self.show_file.layouts, self.show_file.active_layout
        )
        if not len(self.layouts):
            self.layouts = LayoutSet(default_layouts(), "Tech")
        active = self.layouts.active
        if active is not None:
            self.compositor.apply_layout(active.widgets)
        log.info(
            "Layouts: %s (active: %s)",
            ", ".join(self.layouts.names), self.layouts.active_name,
        )

    def _layout_edited(self) -> None:
        """A widget was added, moved or restyled.

        The compositor is already live -- the editor mutates it directly, which
        is what makes changes visible on the preview immediately. All that is
        left is to fold the result back into the layout so it survives, and
        autosave.
        """
        active = self.layouts.active
        if active is not None:
            active.widgets = self.compositor.widgets
        self.request_autosave()

    def _profiles_changed(self) -> None:
        self._ensure_a_console_profile()
        self.eos_panel.set_profiles(
            self.show_file.consoles, self.show_file.active_console
        )
        self.request_autosave()

    def _set_startup_options(self, auto_connect: bool, try_all: bool) -> None:
        self.show_file.eos.auto_connect = auto_connect
        self.show_file.eos.try_all_on_startup = try_all
        self.request_autosave()

    def _layout_set_changed(self) -> None:
        """Layouts were added, renamed or deleted."""
        self._rebuild_layout_menu()
        self.request_autosave()

    def _rebuild_layout_menu(self) -> None:
        """Rebuild the Layout menu and its Ctrl+1..9 shortcuts.

        Necessary because the menu is built once at startup but layouts can be
        created and deleted at any time; a stale menu would fire a shortcut at
        a layout that no longer exists.
        """
        menu = self._layout_menu
        for action in self._layout_actions:
            menu.removeAction(action)
            # Taken off the window as well, not just out of the menu. Each one
            # is parented to the window, so removeAction alone left it alive
            # and findable -- a deleted layout's action still sitting there,
            # and another set of them on every rebuild after that.
            action.setParent(None)
        self._layout_actions = []

        for index, name in enumerate(self.layouts.names[:9], start=1):
            action = QAction(name, self)
            action.setCheckable(True)
            action.setChecked(name == self.layouts.active_name)
            action.setShortcut(QKeySequence(f"Ctrl+{index}"))
            action.triggered.connect(lambda _=False, n=name: self.switch_layout(n))
            menu.insertAction(self._layout_menu_separator, action)
            self._layout_actions.append(action)

    def switch_layout(self, name: str) -> None:
        """Swap the overlay. Safe mid-recording -- that is the point of it."""
        layout = self.layouts.activate(name)
        if layout is None:
            # The name has gone stale: a console macro naming a layout since
            # deleted, or a menu action from before a rebuild. Put the editor
            # back in step with what is actually live -- left alone, its box
            # went on offering a layout nothing could switch to.
            if hasattr(self, "editor"):
                self.editor.refresh_layouts()
            return
        self.compositor.apply_layout(layout.widgets)
        if hasattr(self, "editor"):
            self.editor.refresh()
            self.editor.refresh_layouts()
        self.show_file.active_layout = name
        self.request_autosave()
        self.statusBar().showMessage(f"Layout: {name}", 4000)
        self._sync_layout_menu()

    def _next_layout(self) -> None:
        layout = self.layouts.next()
        if layout is not None:
            self.switch_layout(layout.name)

    def _sync_layout_menu(self) -> None:
        for action in getattr(self, "_layout_actions", []):
            action.setChecked(action.text().lstrip("&") == self.layouts.active_name)

    # ------------------------------------------------------------------ menus

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_output = QAction("Open &Recordings Folder", self)
        open_output.triggered.connect(self._open_recordings_folder)
        file_menu.addAction(open_output)

        open_snapshots = QAction("Open &Snapshots Folder", self)
        open_snapshots.triggered.connect(self._open_snapshots_folder)
        file_menu.addAction(open_snapshots)
        file_menu.addSeparator()

        quit_action = QAction("E&xit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        layout_menu = self.menuBar().addMenu("&Layout")
        self._layout_menu = layout_menu
        self._layout_actions = []
        # Ctrl+1..9 per layout, plus Ctrl+L to cycle. Layouts have to be
        # switchable by hotkey while recording, which means without going
        # anywhere near a menu.
        for index, name in enumerate(self.layouts.names[:9], start=1):
            action = QAction(name, self)
            action.setCheckable(True)
            action.setChecked(name == self.layouts.active_name)
            action.setShortcut(QKeySequence(f"Ctrl+{index}"))
            action.triggered.connect(lambda _=False, n=name: self.switch_layout(n))
            layout_menu.addAction(action)
            self._layout_actions.append(action)

        self._layout_menu_separator = layout_menu.addSeparator()
        cycle = QAction("&Next Layout", self)
        cycle.setShortcut(QKeySequence("Ctrl+L"))
        cycle.triggered.connect(self._next_layout)
        layout_menu.addAction(cycle)

        record_menu = self.menuBar().addMenu("&Record")
        self._record_action = QAction("Start &Recording", self)
        # These two are the hotkeys worth having under the fingers during a
        # tech.
        self._record_action.setShortcut(QKeySequence("Ctrl+R"))
        self._record_action.triggered.connect(self._toggle_recording)
        record_menu.addAction(self._record_action)

        snapshot = QAction("Take &Snapshot", self)
        # F12 rather than a Ctrl chord: a snapshot is a one-handed action you
        # take without looking, often while the other hand is on the desk.
        snapshot.setShortcut(QKeySequence("F12"))
        snapshot.triggered.connect(self.take_snapshot)
        record_menu.addAction(snapshot)

        marker = QAction("Drop &Marker", self)
        marker.setShortcut(QKeySequence("Ctrl+M"))
        marker.triggered.connect(self._drop_manual_marker)
        record_menu.addAction(marker)

        record_menu.addSeparator()
        panic = QAction("&Panic-Hide Overlay", self)
        panic.setShortcut(QKeySequence("Ctrl+H"))
        panic.setCheckable(True)
        panic.toggled.connect(self.compositor.panic_hide)
        record_menu.addAction(panic)

        help_menu = self.menuBar().addMenu("&Help")

        contents = QAction("&Help Contents", self)
        # F1 is where every Windows user looks for help first.
        contents.setShortcut(QKeySequence("F1"))
        contents.triggered.connect(lambda: self.open_help("start"))
        help_menu.addAction(contents)

        # Context entries, so the answer to "how does recording work" is one
        # click rather than a search through a contents page.
        for label, key in (
            ("Connecting to the &console", "eos"),
            ("&Camera and picture", "camera"),
            ("&Recording", "recording"),
            ("The &widgets", "widgets"),
            ("&Editing the overlay", "editing"),
            ("&Layouts", "layouts"),
            ("&Markers and chapters", "markers"),
            ("&Snapshots", "snapshots"),
            ("&Keyboard shortcuts", "shortcuts"),
            ("Rem&ote control from the console", "osccontrol"),
            ("s&ACN status output", "sacn"),
            ("&Troubleshooting", "trouble"),
        ):
            action = QAction(label, self)
            action.triggered.connect(lambda _=False, k=key: self.open_help(k))
            help_menu.addAction(action)

        help_menu.addSeparator()
        open_logs = QAction("Open &Log Folder", self)
        open_logs.triggered.connect(self._open_log_folder)
        help_menu.addAction(open_logs)

        licences = QAction("&Licences...", self)
        licences.triggered.connect(self._show_licences)
        help_menu.addAction(licences)

        help_menu.addSeparator()
        about = QAction(f"&About {APP_NAME}", self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

    # --------------------------------------------------------------- start-up

    def _camera_formats_probed(self) -> None:
        """The first look for cameras is over, so the audio inputs may be asked.

        Not before. On the Blackmagic rig the audio input is the same
        UltraStudio whose video the format probe is asking about. Started at
        launch alongside encoder detection, the audio check would query one
        side of the unit while ffmpeg was querying the other; it only used to
        avoid that by queueing behind detection on the interface thread. Every
        later look -- Refresh -- comes through here as well, and asks nothing
        again.

        A look that answers after the camera was let go has just opened it:
        PreviewPanel._probe_finished sends this after its automatic start. So
        encoder detection may start from here as well.
        """
        first_look = self._startup.finish(CAMERA_FORMATS)
        if self._shutting_down:
            return
        if self._camera_let_go:
            self._detect_encoders_after_the_camera()
        if not first_look:
            return
        self._startup.begin(AUDIO_INPUTS)
        # A first launch starts on a microphone rather than on video only.
        self.record_panel.start_audio_inputs(choose_an_input=self._first_launch)

    def _audio_inputs_checked(self) -> None:
        self._startup.finish(AUDIO_INPUTS)

    def _camera_in_use_changed(self, running: bool) -> None:
        if running:
            name = self.preview.current_device_name()
            if name:
                self.record_panel.set_camera(name)

    def _open_clap_test(self) -> None:
        """Open the clap test on the last take finished.

        Shown, not exec()'d: a modal loop would sit on top of everything else
        the main thread does, and the booth may start a take meanwhile.
        """
        if self._clap_dialog is not None:
            self._clap_dialog.raise_()
            self._clap_dialog.activateWindow()
            return
        dialog = ClapTestDialog(self._last_take, parent=self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.applied.connect(self._clap_test_applied)
        dialog.finished.connect(self._clap_test_closed)
        self._clap_dialog = dialog
        dialog.show()

    def _clap_test_closed(self, _code: int) -> None:
        self._clap_dialog = None

    def _clap_test_applied(self, result: ClapResult) -> None:
        self.record_panel.apply_clap_test(result)
        message = (
            f"Clap test applied: {result.offset_ms} ms for "
            f"{result.camera or 'the camera'} with {result.audio}. The next take uses it."
        )
        log.info(
            "%s Measured %.1f ms of lag from %d claps, in a take recorded at %d ms.",
            message, result.lag_ms, result.claps, result.recorded_offset_ms,
        )
        self.statusBar().showMessage(message, 15000)

    def _start_camera_after_startup(self, unanswered: list) -> None:
        """Let the camera start: every start-up query answered, or the wait ran out.

        Running out is pinned as well as logged. A camera coming up half a
        minute late is plain to see; what it came up without is not, and would
        otherwise be found in the file the next morning.
        """
        if unanswered:
            self._pin_status_message(self._startup.message_for(unanswered))
        self._camera_let_go = True
        self.preview.release_autostart()
        self._detect_encoders_after_the_camera()

    def _detect_encoders_after_the_camera(self) -> None:
        """Start encoder detection once the camera's automatic start is over.

        The camera used to wait for detection as well. On a freshly installed
        Surface Pro 8 it was the last query to answer, at 8.59 s, and opening a
        camera needs nothing it finds; only a take on Automatic does
        (wer.ui.startup has the timings). So the picture no longer waits for it.

        After the open, though, never alongside it: a test encode must not run
        while the camera is opened and its rate measured, for the reason
        wer.ui.startup gives. release_autostart opens the camera on this thread
        before it returns, so when this runs from _start_camera_after_startup
        the open is over -- unless a look for cameras was still running, when
        the camera opens as that look answers and _camera_formats_probed calls
        this again. With no camera attached there is no open to wait for.

        Once only: RecordPanel.start_encoder_detection ignores a second call.
        """
        if self._shutting_down or self.preview.is_probing:
            return
        self.record_panel.start_encoder_detection()

    def _note_a_take_armed_before_detection(self) -> None:
        """Pin it when a take on Automatic starts before encoder detection answers.

        Detection runs once the camera is up, so for a few seconds after the
        picture appears -- it took 7.5 s on the Surface Pro 8, sharing the
        machine with the other start-up queries -- Automatic has nothing to
        choose from but software. A take started then, most likely from the
        desk, is armed on software and stays on it until it ends
        (RecordPanel.output_settings). On a machine whose processor cannot keep
        up with x264 that is a take dropping frames for as long as it runs,
        with nothing on screen to say why.
        """
        settings = getattr(self.recorder, "settings", None)
        if (
            settings is None
            or not self.record_panel.encoders_pending
            or self.show_file.recording.encoder != AUTO_ENCODER
        ):
            return
        self._take_armed_before_detection = True
        message = (
            f"This take records on {settings.resolve_encoder().label}: it started "
            "before encoder detection had answered, so Automatic had nothing else "
            "to choose, and it stays on it until it ends."
        )
        log.warning("%s", message)
        self._pin_status_message(message)

    def _encoders_detected(self) -> None:
        """Follow up a take that started before encoder detection answered.

        What _note_a_take_armed_before_detection pinned stays until Record is
        pressed at this machine, so on its own it would go on saying Automatic
        means software long after detection had found the GPU. A follow-up is
        pinned rather than the first taken down: the take did go out the way
        the first message said, and whoever reads the bar in the morning needs
        both halves. With no such take there is nothing to say.
        """
        if self._shutting_down or not self._take_armed_before_detection:
            return
        self._take_armed_before_detection = False
        automatic = OutputSettings(encoder=AUTO_ENCODER).resolve_encoder()
        message = (
            "Encoder detection has now answered: a take on Automatic records on "
            f"{automatic.label} from here."
        )
        running = self.recorder.settings if self._take_in_progress else None
        if running is not None:
            # The take's encoder was settled when it was armed (see
            # RecordPanel.output_settings), so it does not follow.
            encoder = running.resolve_encoder()
            if encoder.name != automatic.name:
                message += (
                    f" The take already running stays on {encoder.label} "
                    "until it ends."
                )
        log.info("%s", message)
        self._pin_status_message(message)

    def _startup_query_answered_late(self, name: str, late: float) -> None:
        """Say so when a query the camera stopped waiting for has answered.

        What was pinned when the wait ran out stays until Record is pressed at
        this machine, so on its own it would go on telling the booth that a
        take leaves its audio input on the first format listed long after the
        check had answered. A follow-up is pinned rather than the first taken
        down: a take started in between did go out the way the first message
        said, and whoever reads the bar in the morning needs both halves. The
        camera format probe needs no follow-up; the picture coming up is one.
        """
        if self._shutting_down or name != AUDIO_INPUTS:
            return
        message = (
            f"The audio input check has now answered, {late:.0f} s after the "
            "camera stopped waiting for it: a take started from here asks its "
            "audio input for the best format it offers."
        )
        log.info("%s", message)
        self._pin_status_message(message)

    # ---------------------------------------------------------------- central

    def _build_central(self) -> None:
        self.tabs = QTabWidget()

        # The camera's automatic start waits for the start-up device queries --
        # the camera format probe and the audio input check -- so nothing
        # queries a capture device while the camera is being opened and its
        # rate measured. Encoder detection starts only once that is over, so
        # nothing test-encodes on a GPU during it either. wer.ui.startup has
        # the measurements and the reasons; what follows is the wiring.
        self._startup = StartupGate((CAMERA_FORMATS, AUDIO_INPUTS), parent=self)
        #: The gate has let the camera start; see _detect_encoders_after_the_camera.
        self._camera_let_go = False
        #: A take on Automatic went out before detection answered, and the status
        #: bar said so; see _note_a_take_armed_before_detection.
        self._take_armed_before_detection = False
        self._startup.released.connect(self._start_camera_after_startup)
        self._startup.answered_late.connect(self._startup_query_answered_late)

        # Come back up on the camera the show was left on. A rig whose stage
        # camera is DirectShow index 1 otherwise starts every session on the
        # laptop's own webcam, which is what CameraConfig has always promised
        # not to do.
        camera = self.show_file.camera
        self._startup.begin(CAMERA_FORMATS)
        self.preview = PreviewPanel(
            preferred_device=camera.device_name,
            preferred_format=(
                VideoFormat(
                    camera.width, camera.height, camera.fps, camera.fps, camera.fourcc
                )
                if camera.device_name
                else None
            ),
            hold_autostart=True,
            flip_horizontal=camera.flip_horizontal,
            flip_vertical=camera.flip_vertical,
        )
        self.preview.compositor = self.compositor
        self.preview.flips_changed.connect(self.request_autosave)
        self.preview.record_toggled.connect(self._toggle_recording)
        self.preview.snapshot_requested.connect(self.take_snapshot)
        self.tabs.addTab(self.preview, "Preview")

        # Detection is started once the camera has opened, not with the panel.
        self.record_panel = RecordPanel(
            self.show_file.recording, detect_encoders_now=False
        )
        self.record_panel.attach(self.recorder)
        self.record_panel.record_requested.connect(self._start_recording)
        self.record_panel.stop_requested.connect(self._stop_recording)
        self.record_panel.settings_changed.connect(self.request_autosave)
        self.record_panel.encoders_detected.connect(self._encoders_detected)
        self.record_panel.audio_inputs_checked.connect(self._audio_inputs_checked)
        # The A/V offset is kept for each camera and audio input, so the
        # Recording tab has to know the camera: the saved one until the preview
        # starts one, then whichever it is showing.
        self.record_panel.set_camera(camera.device_name)
        self.preview.capture_changed.connect(self._camera_in_use_changed)
        self.record_panel.clap_test_requested.connect(self._open_clap_test)
        self.tabs.addTab(self.record_panel, "Recording")

        # Only once both panels exist, because the answer starts the Recording
        # panel's audio check. The first look for cameras ran inside the
        # preview's constructor and can already be over -- no camera found, or
        # a probe that answered at once -- so ask as well as listen.
        self.preview.formats_probed.connect(self._camera_formats_probed)
        if not self.preview.is_probing:
            self._camera_formats_probed()

        # This is the single most important debugging tool in the app: it
        # separates "the console isn't talking" from "the widget is wrong",
        # which is nearly every question worth asking.
        self.editor = LayoutEditor(self.compositor)
        self.editor.layout_changed.connect(self._layout_edited)
        self.editor.selection_changed.connect(self.preview.highlight)
        self.editor.edit_mode_changed.connect(self.preview.set_edit_mode)
        # A drag goes out from the preview as the whole movement since the
        # press, the editor decides where the widget lands, and the guides it
        # snapped to come back to be drawn.
        self.preview.drag_started.connect(self.editor.begin_drag)
        self.preview.widget_dragged.connect(self.editor.drag)
        self.preview.drag_finished.connect(self.editor.end_drag)
        self.preview.widget_moved.connect(self.editor.nudge)
        self.preview.widget_selected.connect(self.editor.select)
        self.editor.snap_feedback.connect(self.preview.set_snap_feedback)
        self.editor.grid_changed.connect(self.preview.set_grid)
        self.editor.editor_settings_changed.connect(self.request_autosave)
        self.editor.set_editor_config(self.show_file.editor)
        self.editor.layout_switch_requested.connect(self.switch_layout)
        self.editor.layout_set_changed.connect(self._layout_set_changed)
        self.editor.set_layouts(self.layouts)
        self.tabs.addTab(self.editor, "Overlay")

        self.monitor = DataMonitorPanel(self.bus)
        self.tabs.addTab(self.monitor, "Data Monitor")

        connections = QTabWidget()
        connections.setTabPosition(QTabWidget.TabPosition.West)
        self.eos_panel = EosConnectionPanel(self.eos)
        self.eos_panel.settings_changed.connect(self.request_autosave)
        self.eos_panel.profiles_changed.connect(self._profiles_changed)
        self.eos_panel.profile_selected.connect(self.select_console)
        self.eos_panel.find_requested.connect(self.find_console)
        self.eos_panel.startup_options_changed.connect(self._set_startup_options)
        self.eos_panel.set_profiles(
            self.show_file.consoles, self.show_file.active_console
        )
        self.eos_panel.set_startup_options(
            self.show_file.eos.auto_connect, self.show_file.eos.try_all_on_startup
        )
        connections.addTab(self.eos_panel, "Eos")
        self.manual_panel = ManualPanel(self.manual)
        connections.addTab(self.manual_panel, "Manual")
        connections.addTab(ConnectionPanel(self.system), "System")
        self.sacn_panel = SacnPanel(self.show_file.sacn, self.sacn)
        self.sacn_panel.settings_changed.connect(self._sacn_settings_changed)
        connections.addTab(self.sacn_panel, "sACN output")
        self.tabs.addTab(connections, "Connections")

        self.tabs.addTab(self._build_about_tab(), "Environment")
        self.setCentralWidget(self.tabs)

    def _build_about_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel(APP_DISPLAY_NAME)
        title_font = QFont()
        title_font.setPointSize(20)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        subtitle = QLabel(
            "Live camera feed with lighting-console data composited on top."
        )
        _dim(subtitle)
        layout.addWidget(subtitle)

        layout.addWidget(self._build_selfcheck())
        layout.addStretch(1)
        return root

    def _build_selfcheck(self) -> QWidget:
        """Environment readout. Answers 'did the bundle survive the copy?'."""
        box = QGroupBox("Environment")
        box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        ffmpeg = ffmpeg_path()
        # "Version" says which release this is meant to be; "Built from" says
        # which commit it actually came out of, dirty tree and all. They are
        # not the same question, and the one a bug report needs is the second:
        # two builds of 0.9.2 a day apart are not the same program. build.ps1
        # stamps it in, so a checkout has nothing to read and says so.
        stamp = build_id()
        built_from = stamp or ("unknown" if is_frozen() else "source checkout")
        rows: list[tuple[str, str, bool]] = [
            ("Version", __version__, True),
            ("Python", sys.version.split()[0], sys.version_info[:2] == (3, 12)),
            ("Build", "frozen exe" if is_frozen() else "source checkout", True),
            ("Built from", built_from, True),
            ("App folder", str(app_dir()), True),
            ("Log file", str(log_file_path() or log_dir()), True),
            ("Show file", str(self.show_file.schema_version), True),
            (
                "ffmpeg",
                str(ffmpeg) if ffmpeg else "not found - recording unavailable",
                ffmpeg is not None,
            ),
        ]
        for label, value, ok in rows:
            field = QLabel(value)
            field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            field.setWordWrap(True)
            if not ok:
                field.setStyleSheet("color: #c0392b; font-weight: bold;")
            form.addRow(f"{label}:", field)
        return box

    # ------------------------------------------------------------- recording

    def _toggle_recording(self, *, prompt: bool = True) -> None:
        # `and not _closing_take` matters, and is easy to read as belt and
        # braces. A take being torn down still reports is_recording True for
        # the ~214 ms the recorder takes to stop, so without this clause a
        # press landing in that window is read as "stop the take that is
        # already stopping" and thrown away. That is the exact press
        # _start_when_the_last_take_is_closed exists to hold onto -- a console
        # macro firing stop then start back to back -- and gating on
        # is_recording alone meant nothing could ever reach it.
        if self.recorder.is_recording and not self._closing_take.is_set():
            self._stop_recording()
        else:
            self._start_recording(prompt=prompt)

    @property
    def _take_in_progress(self) -> bool:
        """True from arming a take until it has been handed to the finisher."""
        return getattr(self, "_take_live", False)

    @_take_in_progress.setter
    def _take_in_progress(self, value: bool) -> None:
        """Keep Windows awake for exactly as long as this is true.

        A take is armed or ended in four places -- Record, Stop, a recorder
        that dies on its own, and closing the window -- and all four already
        set this flag. A keep-awake call written beside each would be one
        forgotten path away from a machine that sleeps mid-take, or one that
        never sleeps again. Riding on the flag they all set rules out both.
        Every one of them runs on the main thread, which is the thread Windows
        ties the request to; see wer.core.power.
        """
        self._take_live = bool(value)
        if self._take_live:
            self._keep_awake.hold()
        else:
            self._keep_awake.release()

    def _start_recording(self, *, prompt: bool = True) -> None:
        """Arm a take.

        ``prompt`` is False when the start came from OSC. A remote start has
        nobody sitting in front of it, so a modal dialog does not ask a
        question -- it parks the take and waits forever, and a console macro
        pressed twice stacks a second dialog behind the first. Refusals are
        reported in the status bar and the log instead, and the low-disk
        question is answered "yes": whoever wired the desk to start a recording
        wanted a recording. This file already reasons exactly this way about
        remote markers.
        """
        def refuse(title: str, message: str, *, critical: bool = False) -> None:
            log.error("Not recording: %s -- %s", title, message.replace("\n", " "))
            if prompt:
                box = QMessageBox.critical if critical else QMessageBox.warning
                box(self, title, message)
            else:
                self.statusBar().showMessage(f"{title}: {message}", 15000)

        if self._shutting_down:
            return
        if self._take_in_progress:
            # Already rolling. Nothing reaches here that way today -- both
            # toggles ask the recorder first -- but arming twice would call
            # Recorder.start() on a running recorder, which refuses, and the
            # refusal would put a modal over a take that was perfectly healthy.
            return

        # Only while the recorder is actually being torn down -- seconds, not
        # the minutes the chapter rewrite takes. This guard used to be "is a
        # finishing thread alive?", which made Record, Ctrl+R and an OSC
        # /wer/record/start all dead for the whole finish. The button did not
        # look dead, and the remote start was dropped with nothing but a status
        # line nobody was in the room to read.
        #
        # And the press is not thrown away: it is held and retried, because
        # someone who pressed Record wanted a take, not an explanation. The
        # races the old guard covered are closed at the source instead -- see
        # _finish_take, which hands the finisher its own marker log and its own
        # parts list.
        if self._closing_take.is_set():
            self._start_when_the_last_take_is_closed(prompt=prompt)
            return
        self._start_pending = False

        capture = self.preview.capture
        if capture is None or not capture.is_running:
            refuse(
                "No camera",
                "Start the camera on the Preview tab before recording.",
            )
            return

        config = self.show_file.recording
        directory = config.resolved_directory()

        # A folder that has stopped existing is not a folder to create quietly.
        # An external drive left at home, or a path that was only ever
        # temporary, would otherwise send a whole tech somewhere nobody chose
        # and nobody looks until afterwards.
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            refuse(
                "Cannot record there",
                f"{directory}\n\n{exc}\n\nPick another folder on the Recording tab.",
                critical=True,
            )
            return

        space = check_disk(directory)
        if space is not None:
            # A chapter rewrite still running for an earlier take is holding
            # the size of its copy on this drive, and all of it comes back
            # whether the rewrite finishes or is stopped -- and if this take
            # needs the room, it is stopped (see _guard_rewrite). Refusing a
            # take over space that will be free in seconds is a take that never
            # happens, and from the desk nobody is there to press Record again.
            held = self._space_held_by_rewrites(directory)
            free_gb = (space.free_bytes + held) / 1_000_000_000
            if held:
                log.info(
                    "%.1f GB of this drive is held by a chapter rewrite in "
                    "progress; counted as free, because it comes back.",
                    held / 1_000_000_000,
                )
            if free_gb <= config.low_disk_stop_gb:
                # Starting here would record for a few seconds and then stop
                # itself, which is worse than plainly refusing.
                refuse(
                    "Not enough disk space",
                    f"Only {free_gb:.1f} GB free on this drive, which is "
                    f"at or below the {config.low_disk_stop_gb:.0f} GB floor "
                    f"where recording stops.\n\n"
                    "Free up some space, or choose another folder on the "
                    "Recording tab.",
                )
                return
            if free_gb < config.low_disk_warning_gb:
                warning = (
                    f"Only {free_gb:.1f} GB free on this drive. "
                    f"A four-hour tech at 1080p is typically 12-20 GB, and "
                    f"recording stops automatically at "
                    f"{config.low_disk_stop_gb:.0f} GB."
                )
                if prompt:
                    answer = QMessageBox.question(
                        self,
                        "Low disk space",
                        f"{warning} Record anyway?",
                        QMessageBox.StandardButton.Yes
                        | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if answer != QMessageBox.StandardButton.Yes:
                        return
                else:
                    # Recording something is better than recording nothing, and
                    # the recorder stops itself at the floor anyway.
                    log.warning("Remote start on low disk: %s", warning)
                    self.statusBar().showMessage(f"Low disk space. {warning}", 30000)

        show_name = self.manual.fields().get("show", "")
        name = render_filename(
            config.filename_template, show_name=show_name, take=self.system.take
        )
        output = directory / f"{name}.{config.container}"
        # Never silently overwrite a take. Losing a recording to a filename
        # collision would be unforgivable.
        counter = 1
        while output.exists():
            output = directory / f"{name}-{counter}.{config.container}"
            counter += 1

        width, height = capture.actual_resolution
        # The NEGOTIATED rate, not the measured one. measured_fps is a rolling
        # average over the last ~60 frames, so it reflects whatever the machine
        # happened to be doing a second ago -- and arming a recording is exactly
        # when the machine is busiest. Baking a transient 15.4 into the output
        # timebase makes a 30 fps recording claim to be 15 fps, and every tool
        # downstream believes it. ffmpeg is given
        # -use_wallclock_as_timestamps anyway, so it times frames by arrival;
        # -r only needs to be the honest nominal rate.
        fps = capture.settings.fps or capture.stats.measured_fps or 30.0

        self.recorder.low_disk_bytes = int(config.low_disk_warning_gb * 1_000_000_000)
        self.recorder.stop_below_bytes = int(config.low_disk_stop_gb * 1_000_000_000)
        self.recorder.restart_on_failure = config.continue_after_failure

        # Armed BEFORE the recorder starts, and the order is load-bearing.
        # ffmpeg opens the audio input and then reads the frame already waiting
        # in its pipe, and sound and picture are each timed from their own first
        # arrival, so a frame that reaches it late puts the sound early by
        # however late it was -- the very error, 280-790 ms on every take, that
        # putting the audio input first was measured to remove (12-13 Sep 2026;
        # see build_ffmpeg_command). The recorder refuses frames until it has
        # asked DirectShow which audio inputs there are, so none pile up
        # meanwhile, and a take with sound launches ffmpeg only once one has
        # arrived.
        #
        # The drop count first, before feed_encoder is set, so no frame this
        # take turns away can land on the wrong side of the line.
        self._take_capture_stats = capture.stats
        self._take_capture_drops = capture.stats.frames_dropped_encoder
        # A NEW log, not a cleared one. The previous take's finisher may still
        # be reading its markers -- writing the CSV, building each part's
        # chapters -- and clearing the object underneath it emptied the only
        # record of that take mid-write. Rebinding leaves the finisher holding
        # the log it was given (see _finish_take) and costs nothing: the panel
        # and the status bar read whatever self.markers is at the time. Made
        # before the recorder starts, because it counts as recording from then:
        # a cue fired while it starts belongs to this take, and used to be
        # marked in the last one's log.
        last_takes_markers = self.markers
        self.markers = MarkerLog()
        self.markers.add(Marker(timestamp=0.0, kind=MarkerKind.START))
        capture.feed_encoder.set()
        self._start_pump()

        started = self.recorder.start(
            self.record_panel.output_settings(),
            output,
            capture_width=width,
            capture_height=height,
            capture_fps=fps,
        )
        if not started:
            # Disarmed before the refusal is shown, which for a press at this
            # machine is a modal that waits for someone: the camera would go on
            # queueing frames, and the pump feeding them, for a take that is not
            # happening. The marker log goes back to the last take's, as though
            # Record had not been pressed; this one has no take to belong to.
            capture.feed_encoder.clear()
            self._stop_pump()
            self.markers = last_takes_markers
            refuse(
                "Could not start recording",
                self.recorder.detail
                or "ffmpeg would not start. See the log for details.",
                critical=True,
            )
            return

        self._take_in_progress = True
        if prompt:
            # Record was pressed at this machine, so whatever is pinned has
            # been in front of whoever pressed it. A start from the desk is no
            # such evidence: a macro starting Act 2 used to clear Act 1's
            # failure with nobody in the room, and the one notice left for the
            # morning went with it.
            self._unpin_status_message()
        # After the unpin, which would otherwise take it straight down again.
        self._note_a_take_armed_before_detection()
        if config.write_bus_log:
            # Timed from the video's first frame, the clock the markers and
            # chapters are on. From start() instead, every line sat behind the
            # picture by ffmpeg's launch and its audio input's open, which differ
            # take to take. The first frame has normally landed by now; if
            # start() stopped waiting before it had, there is none to time from
            # yet, and the log keeps to when the take was started.
            stats = self.recorder.stats
            self.buslog.start(
                output.with_suffix(".bus.jsonl"),
                stats.first_frame_at or stats.started_at,
            )
        self.preview.set_recording(True)
        self.system.start_recording()
        self._record_action.setText("Stop &Recording")
        log.info("Recording to %s", output)

    def _start_when_the_last_take_is_closed(self, *, prompt: bool) -> None:
        """Hold a Record press that landed while the last take was closing.

        A dropped press is the failure this exists to avoid: a console macro
        that fires /wer/record/stop and /wer/record/start back to back would
        otherwise lose the second cue's take entirely, and nobody would know
        until the edit. The window being waited on is the recorder's own
        teardown -- measured at 214 ms with nothing slowed down -- so in
        practice this fires once and the take starts.
        """
        if self._shutting_down or self._start_pending:
            # One held press, one retry chain. A second press must not start a
            # second one: both would fire when the gate opens, the first would
            # arm the take and the second would find the recorder already
            # running and raise "Could not start recording" over a take that
            # was starting perfectly well.
            return
        self._start_pending = True
        self._start_deadline = time.monotonic() + START_RETRY_LIMIT
        # Shown, not pinned. It is progress rather than a problem, and a pin
        # would outlive the take it was about: a start from the desk no longer
        # takes pins down, so it would have sat over a healthy take all night.
        self._hold_notice = (
            "Closing the last take — the next one starts as soon as it is safe"
        )
        self.statusBar().showMessage(
            self._hold_notice, int(START_RETRY_LIMIT * 1000) + 5000
        )
        log.info("Record pressed while the last take was closing; holding it")
        self._retry_held_start(prompt=prompt)

    def _retry_held_start(self, *, prompt: bool) -> None:
        """One tick of the held press above."""
        if self._shutting_down or not self._start_pending:
            return
        if not self._closing_take.is_set():
            self._start_pending = False
            self._clear_hold_notice()
            self._start_recording(prompt=prompt)
            return
        if time.monotonic() >= self._start_deadline:
            self._start_pending = False
            self._hold_notice = ""
            log.error(
                "Gave up starting a take: the last one was still closing after %gs",
                START_RETRY_LIMIT,
            )
            self._pin_status_message(
                "Could not start: the last take is still being closed"
            )
            return
        QTimer.singleShot(
            START_RETRY_INTERVAL_MS, lambda: self._retry_held_start(prompt=prompt)
        )

    def _clear_hold_notice(self) -> None:
        """Take down the held-start notice, if it is still the one showing."""
        notice, self._hold_notice = self._hold_notice, ""
        if notice and self.statusBar().currentMessage() == notice:
            self.statusBar().clearMessage()

    def _pin_status_message(self, message: str) -> None:
        """Show a message that stays until somebody has seen it.

        Used for anything an operator has to see after walking back into the
        booth. The status-bar labels are permanent widgets (see
        _build_status_bar), so pinning a message no longer costs the REC
        indicator.

        Staying is enforced, not hoped for. QStatusBar holds one temporary
        message, so any later showMessage -- a layout switch, an OSC marker
        while idle, another take's "Saved" -- used to replace a pin, and
        nothing put it back once that message's own timeout ran out: a failure
        pinned at 1:10 was gone at the desk's next GO. A passing message is
        still shown over a pin, and the pin now comes back after it (see
        _on_status_message_changed). What takes pins down is Record pressed at
        this machine, and nothing else; see _start_recording.

        A second pin does not bury the first: the newest is shown, and every
        one not yet acknowledged is on the tooltip.
        """
        self._pinned_messages.append(message)
        del self._pinned_messages[:-PINNED_MESSAGE_LIMIT]
        self._show_pinned_message()

    def _show_pinned_message(self) -> None:
        if not self._pinned_messages:
            return
        self.statusBar().showMessage(self._pinned_messages[-1], 0)
        # The bar is one line and shares it with four permanent labels, so a
        # long message is elided. Nothing may be lost that way: the whole text
        # of every unacknowledged pin is here to be hovered, newest first, and
        # it is all in the log.
        self.statusBar().setToolTip("\n\n".join(reversed(self._pinned_messages)))

    def _on_status_message_changed(self, text: str) -> None:
        """Put a pin back once whatever was shown over it has gone.

        Deferred rather than done inside the signal: this can be emitted from
        inside QStatusBar's own clearMessage, and calling showMessage back into
        it from there is not something Qt documents as safe.
        """
        if not text and self._pinned_messages:
            QTimer.singleShot(0, self._restore_pinned_message)

    def _restore_pinned_message(self) -> None:
        # Asked again: something newer may have been shown in the meantime. It
        # is left to run its course, and the pin comes back after that.
        if self._pinned_messages and not self.statusBar().currentMessage():
            self._show_pinned_message()

    def _unpin_status_message(self) -> None:
        """Acknowledge every pinned message, taking down the one on show.

        Called only when Record is pressed at this machine; see
        _start_recording. Anything newer shown over the pin -- a low-disk
        warning from this very start -- is left alone.
        """
        pinned, self._pinned_messages = self._pinned_messages, []
        if pinned and self.statusBar().currentMessage() in pinned:
            self.statusBar().clearMessage()
        self.statusBar().setToolTip("")

    def _space_held_by_rewrites(self, directory: Path) -> int:
        """Bytes that chapter rewrites still running hold on ``directory``'s drive.

        That is each rewrite's temporary copy, and it all comes back: a rewrite
        that finishes deletes an original about the size of its copy, and one
        that is stopped deletes the copy. Main thread, at a Record press: one
        stat per copy, only while a rewrite is actually running, on the drive
        check_disk has just measured.
        """
        held = 0
        for finisher in self._finishers:
            control = getattr(finisher, "embed", None)
            if control is None or control.temporary is None:
                continue
            if not finisher.is_alive():
                continue
            size = control.temporary_bytes()
            if size and _same_drive(control.temporary, directory):
                held += size
        return held

    def _stop_recording(self) -> None:
        """Finish the take, without freezing the interface.

        Everything here happens on a worker thread. Finishing a recording means
        draining the encoder queue and waiting for ffmpeg to flush and write its
        trailer, and that is genuinely slow -- seconds on a long take. Doing it
        on the main thread froze the whole window for as long as it took, which
        is both awful to use and a blocked main thread, which nothing here
        may do.

        The window stays responsive and shows "Finishing"; the file is reported
        when it is actually on disk.

        Note what this does NOT do: it never touches the camera. The preview
        keeps running, because losing the picture the moment you stop a take is
        exactly wrong -- you almost always want to keep watching, and often to
        start another take straight afterwards.
        """
        if not self._take_in_progress:
            # Nothing to end. Asked of the take rather than of the finishing
            # thread, because by the time a SECOND take is finishing the first
            # one's rewrite may still be running, and that is not a reason to
            # ignore a Stop press.
            return

        capture = self.preview.capture
        if capture is not None:
            capture.feed_encoder.clear()

        self._take_in_progress = False
        self.system.stop_recording()
        self.preview.set_recording(False)
        self._record_action.setText("Start &Recording")

        self._finish_take(
            self.recorder.stats.elapsed,
            capture.settings.fps if capture is not None else 30.0,
            self.show_file.recording,
        )
        log.info("Finishing recording on a worker thread; camera left running")

    def _finish_take(
        self, duration: float, fps: float, config, *, failure: str = ""
    ) -> None:
        """Close the file and write everything that goes with it, off the main
        thread. Every path that ends a take goes through here, so none of them
        can quietly skip a piece of it.

        The finisher works from its OWN copies of everything the next take
        would move: the marker log it is handed here, and the parts list it
        snapshots the instant the recorder is stopped. Both used to be read
        live off the window, which is why starting a take had to be forbidden
        for the whole finish -- Recorder.start() assigns a NEW parts list and
        arming a take cleared the marker log, so a Record press during the
        chapter rewrite could empty the record of the take being written. Own
        copies mean the next take is free to start the moment the recorder is,
        which is the difference between a Record button that works and one that
        is dead for minutes after every Stop.
        """
        markers = self.markers
        markers.add(Marker(timestamp=duration, kind=MarkerKind.STOP))
        # Read here, where the take is handed over. Every caller has already
        # stopped the camera feeding the encoder, so the count is final; by the
        # time the finisher could read it, the next take may be adding to the
        # same counter.
        capture_dropped = self._capture_drops_this_take()
        parts_at_handover = list(getattr(self.recorder, "parts", None) or [])
        take_path = (
            parts_at_handover[0].path if parts_at_handover
            else self.recorder.stats.output_path
        )
        # What the take was recorded with, read here for the same reason: the
        # next take may be armed with other settings before this one is done.
        take_settings = getattr(self.recorder, "settings", None)
        take_camera = self.preview.current_device_name()
        take_audio = getattr(take_settings, "audio_device", None)
        take_offset = int(getattr(take_settings, "av_offset_ms", 0) or 0)
        self._closing_take.set()

        def finish() -> None:
            output = None
            parts: list = []
            problem = ""
            note = ""
            recorder_dropped = 0
            ffmpeg_failing = 0
            kept_failing = False
            written = 0
            sound: float | None = None
            try:
                try:
                    self._stop_pump()
                    output = self.recorder.stop()
                    # Snapshot before anything can rebind it, and before the
                    # gate below lets the next take call Recorder.start() --
                    # which replaces the stats the drop count is read from too.
                    parts = list(self.recorder.parts)
                    stats = self.recorder.stats
                    recorder_dropped = int(getattr(stats, "frames_dropped", 0) or 0)
                    ffmpeg_failing = min(recorder_dropped, int(
                        getattr(stats, "frames_dropped_ffmpeg_failing", 0) or 0
                    ))
                    kept_failing = int(
                        getattr(stats, "most_ffmpeg_failures_in_a_row", 0) or 0
                    ) > 1
                    written = int(getattr(stats, "frames_written", 0) or 0)
                    sound = getattr(stats, "sound_delivered_share", None)
                    self.buslog.stop()
                finally:
                    self._closing_take.clear()
                thread.stage = "writing its cue times and chapters"
                if output is not None and output.is_file():
                    problem, note = self._write_sidecars(
                        output, parts, markers, fps, config, embed=thread.embed
                    )
            except Exception:  # noqa: BLE001
                # Whatever went wrong, this take still has to be reported. A
                # finishing thread that dies before the emit leaves no "Saved",
                # no failure message and a window that believes a take is still
                # being written -- the silent failure this whole file is about.
                log.exception("Finishing the take failed")
                problem = (
                    "finishing the take failed; see the log for what happened"
                )
            finally:
                dropped = capture_dropped + recorder_dropped
                behind = dropped - ffmpeg_failing
                if dropped:
                    # The recorder logs drops at 1, 10, 100 and 1000 and then
                    # says nothing more, so without this line the log of a
                    # four-hour take cannot tell 1,001 from 300,000. The cause
                    # is the one the recorder saw: this line put the soak's
                    # 2,638 frames lost to a failing ffmpeg down to the encoder
                    # not keeping up. None of the camera's queue's count can be
                    # ffmpeg failing: the pump empties that queue into
                    # submit(), which never waits on ffmpeg, so a failing
                    # ffmpeg cannot back it up. What does back it up is not
                    # known here, so that count keeps the wording it had.
                    lost_to_ffmpeg = _why_ffmpeg_lost_them(kept_failing)
                    if not ffmpeg_failing:
                        log.warning(
                            "%s lost %d frame(s) because the encoder could not "
                            "keep up: %d turned away by the camera's encoder "
                            "queue and %d by the recorder's, with %d written.",
                            thread.take_name, dropped, capture_dropped,
                            recorder_dropped, written,
                        )
                    elif not behind:
                        log.warning(
                            "%s lost %d frame(s), with %d written, all turned "
                            "away by the recorder %s.",
                            thread.take_name, dropped, written, lost_to_ffmpeg,
                        )
                    else:
                        log.warning(
                            "%s lost %d frame(s), with %d written: %d turned "
                            "away by the recorder %s, and %d because the "
                            "encoder could not keep up (%d by the camera's "
                            "encoder queue and %d by the recorder's).",
                            thread.take_name, dropped, written, ffmpeg_failing,
                            lost_to_ffmpeg, behind, capture_dropped,
                            recorder_dropped - ffmpeg_failing,
                        )
                # Marked, not removed, and not left to is_alive(): the thread
                # is still running at this point -- it is inside its own
                # finally -- so pruning the list on liveness would have kept
                # this entry forever whenever the main thread got here first.
                # The mark means "this take has been reported", which is
                # exactly what the list is asked.
                thread.reported = True
                self.recording_finished.emit(
                    FinishedTake(
                        files=(
                            [p.path for p in parts if p.usable and p.exists]
                            or ([output] if output is not None else [])
                        ),
                        failure=failure,
                        problem=problem,
                        note=note,
                        continued_at=[p.session_offset for p in parts[1:]],
                        frames_dropped=dropped,
                        frames_dropped_ffmpeg_failing=ffmpeg_failing,
                        ffmpeg_kept_failing=kept_failing,
                        frames_written=written,
                        sound_delivered=sound,
                        camera=take_camera,
                        audio_device=take_audio,
                        av_offset_ms=take_offset,
                    )
                )

        thread = threading.Thread(target=finish, name="finish-recording", daemon=True)
        thread.reported = False
        #: The way into this take's chapter rewrite, for the disk guard and for
        #: shutdown. See _write_sidecars and _abandon_unfinished_takes.
        thread.embed = EmbedControl()
        #: What to call the take, and what it is doing, in the one log line
        #: written if shutdown has to leave it unfinished.
        thread.take_name = take_path.name if take_path is not None else "the take"
        thread.stage = "closing its video file"
        self._finishers.append(thread)
        thread.start()

    def _capture_drops_this_take(self) -> int:
        """Frames the camera's encoder queue turned away since the take was armed.

        Main thread. The counter lives as long as the camera, so the take's
        share is what it has gained since _start_recording noted it. A camera
        replaced mid-take started a new counter after the take began, so all of
        that one counts; whatever the old camera turned away is lost to this
        figure, which after a camera change can be too low but never too high.
        """
        capture = self.preview.capture
        if capture is None:
            return 0
        stats = capture.stats
        count = int(getattr(stats, "frames_dropped_encoder", 0) or 0)
        if stats is self._take_capture_stats:
            return max(0, count - self._take_capture_drops)
        return count

    def _live_take_floor(self, path: Path) -> int | None:
        """The free space at which a take recording on ``path``'s drive stops.

        None when no take is recording there. Runs on finishing threads: it
        reads attributes the main thread owns, and a plain read needs no lock
        (see _start_pump). The floor is the recorder's own rule,
        max(stop_below_bytes, CRITICAL_FREE_BYTES), because that is the number
        the take's disk check will actually stop it at.
        """
        if not self._take_in_progress:
            return None
        output = getattr(self.recorder.stats, "output_path", None)
        if output is None or not _same_drive(path, Path(output).parent):
            return None
        return max(int(self.recorder.stop_below_bytes), CRITICAL_FREE_BYTES)

    def _guard_rewrite(self, control: EmbedControl, video: Path):
        """Watch one chapter rewrite, and stop it before it can stop a take.

        The check in _write_sidecars is made once, before the rewrite starts.
        A take armed after that -- at any point in a rewrite that runs for
        minutes -- was never weighed against it: its own start check saw the
        space before the copy had grown into it, and the copy then took the
        drive under its floor. So for as long as the rewrite runs this looks
        every REWRITE_GUARD_INTERVAL, and once a take is recording on the same
        drive with free space inside REWRITE_HEADROOM_BYTES of its floor, the
        rewrite is stopped. Its copy is deleted and the space is back well
        before the take's own check, which runs every 10 s.

        Returns a function that ends the watch; call it once the rewrite is
        over, whatever the outcome.
        """
        done = threading.Event()

        def watch() -> None:
            while not done.wait(REWRITE_GUARD_INTERVAL):
                floor = self._live_take_floor(video)
                if floor is None:
                    continue
                space = check_disk(video.parent)
                if space is None or space.free_bytes > floor + REWRITE_HEADROOM_BYTES:
                    continue
                if done.is_set():
                    return
                log.warning(
                    "Stopping the chapter rewrite of %s: %.1f GB free, and the "
                    "take recording on the same drive stops at %.1f GB.",
                    video.name, space.free_gb, floor / 1_000_000_000,
                )
                control.cancel("the take recording on the same drive needed the room")
                return

        watcher = threading.Thread(target=watch, name="rewrite-guard", daemon=True)
        watcher.start()

        def end() -> None:
            done.set()
            # Bounded: check_disk on a volume that has gone away can take its
            # time, and the finish must not wait on that.
            watcher.join(timeout=2.0)

        return end

    def _write_sidecars(
        self, output, all_parts, markers, fps: float, config,
        *, embed: EmbedControl | None = None,
    ) -> str:
        """Write the marker files and, if asked, fold them into the video.

        Runs on the finishing thread. Nothing here may touch Qt.

        ``embed`` is the finisher's way into the chapter rewrite, so shutdown
        can stop it; see _abandon_unfinished_takes.

        ``all_parts`` and ``markers`` belong to THIS take and are passed in
        rather than read off the window, so the next take can be armed while
        this is still running -- see _finish_take. Returns two plain-language
        strings for the caller to carry to the status bar: what could not be
        written, "" when the take was saved whole; and a note on anything else
        true about where its files are, "" when there is nothing to say.

        Markers are timed against the SESSION, but chapters have to be timed
        against the file they are embedded in. Normally those are the same
        thing; they differ only when ffmpeg failed and the session continued
        into a second file, and then each part gets the markers that fall
        inside it, shifted to that part's own timeline.
        """
        parts = [p for p in all_parts if p.usable and p.exists]
        if not parts:
            log.error("No usable recording to write sidecars for")
            return "no usable recording was found to write the cue times against", ""

        # A part that ended up empty holds nothing and only raises the question
        # of what happened to it. The failure is already in the log and in the
        # status message; the zero-byte file adds nothing.
        for part in all_parts:
            if not part.usable and part.path.is_file():
                try:
                    if part.path.stat().st_size == 0:
                        part.path.unlink()
                        log.info("Removed the empty %s", part.path.name)
                except OSError:
                    log.exception("Could not remove %s", part.path.name)

        # Both of these are named after the FIRST part, because that is what
        # the session was called when it started. After a continuation `output`
        # is the LAST part, so deriving them from it would look for files that
        # never existed -- which is exactly what happened.
        csv_path = parts[0].path.with_suffix(".markers.csv")
        bus_path = parts[0].path.with_suffix(".bus.jsonl")
        try:
            markers.write_csv(csv_path, frames_per_second=fps)
        except OSError:
            log.exception("Could not write the marker CSV")
            return f"the cue times could not be written to {csv_path.name}", ""

        # What the rewrites really put where. The tidy-up after the loop may
        # delete only what the take's files now hold, and what a container
        # could not hold goes into the note.
        chapters_inside: list[Path] = []
        attached_per_part: list[set[Path]] = []
        left_out: list[Path] = []
        for part in parts:
            chapters_path = part.path.with_suffix(".chapters.txt")
            try:
                self._write_part_chapters(markers, part, chapters_path)
            except OSError:
                log.exception("Could not write chapters for %s", part.path.name)
                continue

            if not config.embed_markers:
                continue

            # Embedding writes a complete second copy of the recording and
            # only then replaces the original: measured at the instant of the
            # swap, peak usage is exactly 2.00x the file. Finding that out by
            # running out of space costs minutes on a long take and leaves a
            # half-written temp file; a stat call costs nothing.
            space = check_disk(part.path.parent)
            try:
                needed = part.path.stat().st_size * 1.05
            except OSError:
                # The parts list was filtered by p.exists when it was built;
                # this stat happens later. A USB or network volume -- which is
                # exactly where takes get written -- can go away in between,
                # and letting the OSError out killed the finishing thread
                # before anything was ever reported. embed_chapters checks the
                # file itself and fails politely, so fall through to it rather
                # than inventing a second answer here.
                log.exception("Could not size %s before embedding", part.path.name)
                needed = 0.0
            if space is not None and needed and space.free_bytes < needed:
                log.error(
                    "Not embedding chapters in %s: it needs %.1f GB free to "
                    "rewrite the file and there is %.1f GB.",
                    part.path.name, needed / 1_000_000_000, space.free_gb,
                )
                return (
                    f"there was not enough room to embed the chapters, so the "
                    f"cue times stayed in {csv_path.name} beside the video"
                ), ""

            # A take recording on the same drive outranks these chapters. The
            # check above asks only whether the drive can hold a second copy of
            # the file, never whether writing it would take a live take under
            # its stop floor -- and a take stopped for space at three in the
            # morning stays stopped. Skipped rather than put off until that
            # take ends: it may run for hours, and a rewrite waiting behind it
            # dies with the app if the window is closed first.
            live_floor = self._live_take_floor(part.path)
            if (
                space is not None and needed and live_floor is not None
                and space.free_bytes - needed <= live_floor + REWRITE_HEADROOM_BYTES
            ):
                log.error(
                    "Not embedding chapters in %s: rewriting it would leave "
                    "%.1f GB free, and the take recording on the same drive "
                    "stops at %.1f GB.",
                    part.path.name, (space.free_bytes - needed) / 1_000_000_000,
                    live_floor / 1_000_000_000,
                )
                return (
                    f"the chapters were not embedded, because rewriting the "
                    f"file would have run the take recording on the same drive "
                    f"out of room; the cue times stayed in {csv_path.name} "
                    f"beside the video"
                ), ""

            control = embed if embed is not None else EmbedControl()
            attachments = [p for p in (csv_path, bus_path) if p.is_file()]
            end_guard = self._guard_rewrite(control, part.path)
            try:
                result = embed_chapters(
                    part.path, chapters_path,
                    attachments=attachments,
                    tags={
                        "title": self.manual.fields().get("show", "") or part.path.stem,
                        "comment": f"Recorded with Wer. {len(markers)} markers."
                        + (f" Part {parts.index(part) + 1} of {len(parts)}."
                           if len(parts) > 1 else ""),
                    },
                    control=control,
                )
            finally:
                end_guard()
            if not result.ok and result.cancelled:
                # Stopped from outside: by the guard, for a live take's sake, or
                # by shutdown. Degraded rather than lost, as a failure is -- the
                # original is untouched and the sidecars are still on disk.
                log.error(
                    "Chapters were not embedded in %s, because %s. The cue "
                    "times are in %s beside it.",
                    part.path.name, result.detail, csv_path.name,
                )
                return (
                    f"the chapters were not embedded, because {result.detail}; "
                    f"the cue times stayed in {csv_path.name} beside the video"
                ), ""
            if not result.ok:
                # The recording itself is untouched and the sidecars are on
                # disk (returning here skips the deletion below), so this is a
                # degraded result rather than a lost one. But it was reported
                # only in the log, while the status bar said "Saved take.mkv
                # (48 MB)" over a file with no chapters in it -- and with
                # keep_sidecar_files off by default, nobody would think to go
                # looking beside the video for the cue times.
                log.error("Could not embed markers: %s", result.detail)
                return (
                    f"the chapters could not be embedded, so the cue times "
                    f"stayed in {csv_path.name} beside the video"
                ), ""
            chapters_inside.append(chapters_path)
            attached_per_part.append(set(result.attached))
            left_out += [path for path in result.left_out if path not in left_out]

        note = ""
        if left_out:
            # Not a fault: the chapters and tags are in the file, and these
            # were never going to fit. Said anyway, at INFO and after "Saved",
            # because the Recording tab promises one file, and whoever finds
            # three beside an MP4 should be told why.
            kind = parts[0].path.suffix.lstrip(".").upper()
            kept = [path for path in (csv_path, bus_path) if path in left_out]
            stay = "stays" if len(kept) == 1 else "stay"
            log.info(
                "%s %s beside %s, not inside it: %s cannot hold attachments",
                " and ".join(path.name for path in kept), stay,
                parts[0].path.name, kind,
            )
            named = {csv_path: "the marker list", bus_path: "the bus log"}
            note = (
                " and ".join(f"{named[path]} ({path.name})" for path in kept)
                + f" {stay} beside the video, not inside it, because {kind} "
                "cannot hold attachments"
            )

        if config.embed_markers and not config.keep_sidecar_files:
            # Only what the take's files now hold. Deleting every sidecar once
            # the rewrites succeeded was safe only while no MP4 rewrite could
            # succeed. An MP4 cannot hold attachments, so its marker list and
            # bus log exist only beside it -- the bus log nowhere else at all --
            # and deleting them would turn embedded chapters into console data
            # silently lost. A chapters file is an intermediate: once its
            # part's rewrite succeeded, what it held is in that part. The marker
            # list and the bus log cover the whole take, so they go only when
            # every part holds them, and a part whose chapters could not be
            # written was never rewritten and holds neither.
            if len(attached_per_part) == len(parts):
                inside_every_part = set.intersection(*attached_per_part)
            else:
                inside_every_part = set()
            removable = [
                *chapters_inside,
                *(path for path in (csv_path, bus_path) if path in inside_every_part),
            ]
            for path in removable:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.exception("Could not remove sidecar %s", path.name)
        return "", note

    def _write_part_chapters(self, markers, part, chapters_path) -> None:
        """Chapters for one part, shifted onto that part's own timeline."""
        start = part.session_offset
        end = start + part.duration

        subset = MarkerLog()
        for marker in markers.markers:
            if start <= marker.timestamp <= end:
                subset.add(
                    replace(marker, timestamp=marker.timestamp - start)
                )
        subset.write_chapters(chapters_path, part.duration)

    def _on_recording_finished(self, report: FinishedTake) -> None:
        """Main thread, via a queued signal.

        The report arrives with the take it belongs to rather than being read
        off the window, because a second take can be started and finished
        while the first one's chapter rewrite is still running.
        """
        self._finishers = [
            finisher for finisher in self._finishers if not finisher.reported
        ]
        self.request_autosave()
        failure, problem = report.failure, report.problem
        if report.files:
            self._last_take = TakeToMeasure(
                path=report.files[0],
                camera=report.camera,
                audio=report.audio_device,
                av_offset_ms=report.av_offset_ms,
            )

        # The size is a nicety; being told the take ended is not. A volume that
        # has gone away -- a pulled USB disk, a dropped network share, which is
        # exactly where takes get written -- makes stat() raise, and this runs
        # in a Qt slot on the main thread where the exception would take the
        # whole report with it and leave the status bar on the last thing it
        # happened to be saying.
        message = "Recording stopped"
        files = list(report.files)
        if files:
            try:
                present = [path for path in files if path.is_file()]
                total = sum(path.stat().st_size for path in present) / 1_000_000
            except OSError:
                log.exception("Could not read back the finished take %s", files[-1])
                message = (
                    f"Recording stopped: {files[-1].name} could not be read back"
                )
            else:
                for path in present:
                    log.info("Recording saved: %s", path)
                if len(present) == 1:
                    message = f"Saved {present[0].name} ({total:.0f} MB)"
                elif present:
                    # Every file, first to last. Recorder.stop() hands back the
                    # LAST part, and naming only that -- with only its size --
                    # announced the tail of a take as the whole of it, while
                    # the first forty minutes sat in a file nobody mentioned.
                    message = (
                        f"Saved in {len(present)} files: "
                        f"{', '.join(path.name for path in present)} "
                        f"({total:.0f} MB in all)"
                    )
        if report.note:
            # After "Saved", not in front of it with the warnings: it is not
            # one, and a status bar that elides the message should lose this
            # before it loses the file's name. The log has all of it anyway.
            message = f"{message} — {report.note}"
        # The warning goes FIRST, before the reassuring half. The status bar is
        # one line and it now shares its width with four permanent labels, so a
        # long message is elided on the right -- and what must never be the
        # part that survives the cut is "Saved take012.mkv (4...". Measured at
        # this window's default 1220 px: the labels take 984 and the message
        # area 236, about nineteen characters; maximised on a 1920 booth
        # monitor it is 936, which reads the whole warning. The full text is on
        # the tooltip and in the log either way.
        if report.frames_dropped:
            message = f"{self._describe_drops(report)} — {message}"
        sound_short = (
            report.sound_delivered is not None
            and report.sound_delivered < SOUND_SHORT_BELOW
        )
        if sound_short:
            message = (
                f"Only {report.sound_delivered:.0%} of the sound arrived, so it "
                f"has gaps — {message}"
            )
        if report.continued_at:
            message = f"{self._describe_continuation(report)} — {message}"
        if failure:
            message = f"Recording FAILED: {failure} — {message}"
        if problem:
            message = f"Not saved whole: {problem} — {message}"

        # "Saved" on its own has to mean saved whole. A take that ended because
        # the encoder gave up, or one whose chapters never made it into the
        # file, is PINNED -- no timeout -- so the message is still on screen
        # when whoever left the booth comes back. That is safe only because the
        # status-bar labels are permanent widgets: QStatusBar hides ordinary
        # ones for as long as a message is showing, and pinning this over
        # addWidget labels took the REC indicator off the screen for the rest
        # of the session. See _build_status_bar; do not move them back.
        #
        # A take split across files by an ffmpeg that died, and a take that
        # lost frames, are pinned for the same reason. Neither ever reached
        # the window as a failure: the recorder stays RECORDING through a
        # restart and through every drop, so the ten-second "Saved" was the
        # only thing ever said about either. The same goes for a take whose
        # sound arrived with gaps.
        if (
            failure or problem or report.continued_at or report.frames_dropped
            or sound_short
        ):
            self._pin_status_message(message)
        else:
            self.statusBar().showMessage(message, 10000)

    @staticmethod
    def _describe_continuation(report: FinishedTake) -> str:
        """Where ffmpeg stopped mid-take and the recording carried on anew."""
        times = [_clock(at) for at in report.continued_at]
        if len(times) == 1:
            return (
                f"ffmpeg stopped at {times[0]} and the take carried on in a new "
                f"file, with a gap where it restarted"
            )
        return (
            f"ffmpeg stopped {len(times)} times (at {', '.join(times)}) and the "
            f"take carried on in new files, with a gap at each restart"
        )

    @staticmethod
    def _describe_drops(report: FinishedTake) -> str:
        """How many frames never reached the file, what share, and why.

        Why as the recorder saw it. Every dropped frame used to be put down to
        the encoder, so the overnight soak take whose h264_mf ffmpeg failed at
        every start finished on "2,638 frames dropped (99.5%): the encoder
        could not keep up". That points whoever reads it at an encoder too
        slow for the picture, when no ffmpeg was taking frames at all.
        """
        dropped = report.frames_dropped
        share = ""
        if report.frames_written:
            percent = dropped / (report.frames_written + dropped) * 100
            share = " (under 0.1%)" if percent < 0.1 else f" ({percent:.1f}%)"
        noun = "frame" if dropped == 1 else "frames"
        failing = min(report.frames_dropped_ffmpeg_failing, dropped)
        behind = dropped - failing
        lost_to_ffmpeg = _why_ffmpeg_lost_them(report.ffmpeg_kept_failing)
        if not failing:
            why = "the encoder could not keep up"
        elif not behind:
            why = f"not recorded {lost_to_ffmpeg}"
        else:
            why = (
                f"{failing:,} not recorded {lost_to_ffmpeg}, {behind:,} because "
                f"the encoder could not keep up"
            )
        return f"{dropped:,} {noun} dropped{share}: {why}"

    def _start_pump(self) -> None:
        """Move frames from the live capture queue into the recorder.

        A thread rather than a Qt timer: this must not be affected by how busy
        the UI is, and it must not run on the main thread where a slow write
        would stall the interface.

        It reads ``self.preview.capture`` on every pass rather than holding the
        object it was handed. That is the difference between a take that
        survives its camera being replaced and one that ends without saying so:
        the pump used to close over the CameraCapture that existed when
        recording started, so anything that built a new one -- pressing Refresh
        was enough -- left it draining a queue nothing would ever fill again,
        for the rest of the session, while the preview showed a perfect picture
        and the clock, the markers and the chapters carried on as if all were
        well. Measured: a 17.6 s take produced a 5.1 s file.

        A plain attribute read needs no lock; the panel only ever rebinds it,
        on the main thread.
        """
        self._pump_stop.clear()

        def pump() -> None:
            while not self._pump_stop.is_set():
                capture = self.preview.capture
                if capture is None:
                    # Between a stop and a start. Wait rather than spin.
                    self._pump_stop.wait(0.1)
                    continue
                try:
                    frame = capture.encoder_queue.get(timeout=0.25)
                except queue.Empty:
                    continue
                self.recorder.submit(frame)

        self._pump_thread = threading.Thread(
            target=pump, name="encoder-pump", daemon=True
        )
        self._pump_thread.start()

    def _stop_pump(self) -> None:
        self._pump_stop.set()
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=3.0)
            self._pump_thread = None
        # Whatever camera is live now, it is not feeding an encoder any more.
        # The one at the start of the take was disarmed by the caller; a
        # replacement that arrived mid-take would otherwise go on filling a
        # queue nobody drains and counting the overflow as encoder drops.
        capture = self.preview.capture
        if capture is not None:
            capture.feed_encoder.clear()

    def _on_sound_warning(self, message: str) -> None:
        """The audio input is delivering too little of its sound. Main thread,
        via a queued signal.

        Pinned. The take carries on and the gaps with it, so the message has
        to still be there when somebody looks, and the Recording tab, where
        the recorder also says it, is not the tab anyone watches mid-take.
        """
        self._pin_status_message(message)

    def _on_disk_warning(self, warning: DiskWarning) -> None:
        """Free space is running low. Main thread, via a queued signal."""
        message = warning.describe()
        log.warning("Disk: %s", message)
        self.statusBar().showMessage(message, 30000)
        if warning.critical:
            # The recorder closes the file itself; the window still has to stop
            # feeding it and run the normal finish, or the encoder pump would
            # keep going against a dead process.
            if self.recorder.is_recording:
                QTimer.singleShot(0, self._stop_recording)
            # A modal while the file is still being written would be wrong, so
            # tell them once it is safely on disk.
            QTimer.singleShot(
                2500,
                lambda: QMessageBox.warning(
                    self, "Recording stopped: not enough disk space", message
                ),
            )

    def _on_recorder_state(self, state: RecorderState, detail: str) -> None:
        """Recorder callback. Arrives on a worker thread; touch no widgets."""
        if state is RecorderState.ERROR:
            log.error("Recorder error: %s", detail)
            self.recorder_failed.emit(detail)

    def _on_recorder_failed(self, detail: str) -> None:
        """The recorder gave up mid-take. Main thread, via a queued signal.

        This used to be a log line and nothing else, and the log is not where an
        operator looks. `is_recording` went False on its own while the window
        still believed a take was running: the Preview tab's big button and
        Ctrl+R both still read "Stop Recording" beside a status bar reading
        "Not recording", and pressing either called `_start_recording`, which
        clears the marker log and resets the parts list -- destroying every
        marker and every trace of the take that had just failed, before
        anything had been written for it.

        So the take is finished here, properly and once: the footage already on
        disk gets its markers, its chapters and its bus log, and the controls go
        back to telling the truth. No modal: this can fire at three in the
        morning with nobody in the booth, and a dialog would only sit there
        blocking the next take.
        """
        if not self._take_in_progress:
            return
        self._take_in_progress = False

        capture = self.preview.capture
        if capture is not None:
            capture.feed_encoder.clear()
        self.system.stop_recording()
        self.preview.set_recording(False)
        self._record_action.setText("Start &Recording")

        # Carried to the finisher and reported once the file is actually
        # closed, not now: the "Saved ..." message that lands a second later
        # would otherwise wipe this one off the status bar and take the only
        # visible trace of the failure with it.
        self._finish_take(
            self.recorder.stats.elapsed,
            capture.settings.fps if capture is not None else 30.0,
            self.show_file.recording,
            failure=detail,
        )

    # ---------------------------------------------------------------- markers

    def _on_cue_fired(self, event: CueFired) -> None:
        """Auto-marker on every cue fire.

        Called on the Eos connection thread, so it must not touch Qt. MarkerLog
        is thread-safe; the status bar picks the count up on its own timer.

        Markers are recorded only during a take. A cue fired while idle has no
        timestamp to be relative to.
        """
        if not self.recorder.is_recording:
            return
        if not self.show_file.recording.auto_markers:
            return
        self.markers.add_cue(
            self.recorder.stats.elapsed,
            f"{event.cue_list}/{event.number}".strip("/"),
            event.label,
        )

    def take_snapshot(self) -> None:
        """Save a still of the current picture, overlay included.

        Independent of recording, because the camera is. Taking one during a
        take also drops a marker, so the still and the moment in the video can
        be found from each other afterwards.
        """
        image = self.preview.current_frame()
        if image is None:
            log.info("Snapshot not taken: the camera is not running")
            self.statusBar().showMessage(
                "No picture to snapshot — the camera is not running", 5000
            )
            return

        config = self.show_file.recording
        cue = self.bus.value("eos.cue.active.number") or ""
        name = render_filename(
            config.snapshot_template,
            show_name=self.manual.fields().get("show", ""),
            take=self.system.take,
            cue=str(cue),
        )
        try:
            image_format = SnapshotFormat(config.snapshot_format)
        except ValueError:
            image_format = SnapshotFormat.PNG

        directory = config.resolved_snapshot_directory()
        path = directory / f"{name}.{image_format.value}"
        # Two snapshots inside the same second must not overwrite each other,
        # and during a tech that happens.
        counter = 2
        while path.exists():
            path = directory / f"{name}-{counter}.{image_format.value}"
            counter += 1

        result = save_snapshot(
            image, path,
            image_format=image_format,
            jpeg_quality=config.snapshot_jpeg_quality,
        )
        if not result.ok:
            log.error("Snapshot failed: %s", result.detail)
            QMessageBox.warning(
                self, "Could not save the snapshot", result.detail
            )
            return

        if self.recorder.is_recording and config.snapshot_marks_recording:
            self.markers.add_manual(
                self.recorder.stats.elapsed, f"Snapshot: {path.name}"
            )

        self.statusBar().showMessage(
            f"Snapshot saved: {path.name} ({result.size_bytes / 1024:.0f} KB)",
            8000,
        )

    def _drop_manual_marker(self) -> None:
        if not self.recorder.is_recording:
            self.statusBar().showMessage(
                "Markers are only dropped while recording", 4000
            )
            return
        elapsed = self.recorder.stats.elapsed
        note, accepted = QInputDialog.getText(
            self, "Drop marker",
            f"Note for the marker at {int(elapsed // 60)}:{int(elapsed % 60):02d} "
            "(optional):",
        )
        if not accepted:
            return
        self.markers.add_manual(elapsed, note.strip())
        self.statusBar().showMessage(
            f"Marker at {int(elapsed // 60)}:{int(elapsed % 60):02d}"
            + (f" — {note}" if note.strip() else ""),
            5000,
        )

    # ------------------------------------------------------------- status bar

    def _build_status_bar(self) -> None:
        self._eos_status = QLabel()
        self._record_status = QLabel()
        self._bus_status = QLabel()
        self._marker_status = QLabel()
        self._sacn_status = QLabel()
        # addPermanentWidget, not addWidget. QStatusBar HIDES every ordinary
        # widget for as long as a message is showing, and this window pins
        # messages with no timeout for a failed take or a failed chapter embed
        # -- deliberately, so they survive until someone walks back into the
        # booth. With addWidget that cost the REC indicator: one degraded take
        # blanked all four labels for the rest of the session, including
        # through every later take, while the REC text went on updating behind
        # a hidden widget. "Is it rolling?" has exactly one at-a-glance answer
        # and it must never be the thing a message pushes off the screen.
        for widget in (self._eos_status, self._sacn_status, self._record_status,
                       self._bus_status, self._marker_status):
            self.statusBar().addPermanentWidget(widget)
        # How a pinned message outlives the passing ones shown over it; see
        # _pin_status_message.
        self.statusBar().messageChanged.connect(self._on_status_message_changed)
        self._refresh_status_bar()

    def _refresh_status_bar(self) -> None:
        self.eos.refresh_staleness()
        state = self.eos.status.state

        # Remember a console only once it has actually delivered data. A host
        # that was typed and never worked should not be dialled automatically
        # on every future launch -- and an open socket is not evidence, since
        # Eos accepts connections with OSC output switched off.
        if state is ConnectionState.LIVE:
            profile = self.active_console
            now = time.time()
            # Once a minute is plenty; this runs twice a second.
            if now - profile.last_connected > 60:
                profile.last_connected = now
                self.show_file.eos.enabled = True
                log.info("Console %r is live; remembered as most recent",
                         profile.name)
                self.request_autosave()

        colour = STATE_COLOURS.get(state, "#7f8c8d")
        self._eos_status.setText(f"● Eos: {state.value}")
        self._eos_status.setStyleSheet(f"color: {colour};")
        self._refresh_sacn_status()

        if self.recorder.is_recording:
            stats = self.recorder.stats
            self._record_status.setText(
                f"   ● REC {int(stats.elapsed // 60)}:{int(stats.elapsed % 60):02d}"
            )
            self._record_status.setStyleSheet("color: #c0392b; font-weight: bold;")
        else:
            self._record_status.setText("   ○ Not recording")
            self._record_status.setStyleSheet("")

        self._bus_status.setText(
            f"   Layout: {self.layouts.active_name}   Bus: {len(self.bus)} keys"
            + ("" if self.settings_claim.may_save else "   settings read-only")
        )
        count = len(self.markers)
        self._marker_status.setText(
            f"   Markers: {count}" if count else "   Markers: none"
        )
        self.record_panel.note_show(
            self.manual.fields().get("show", ""),
            self.system.take,
            str(self.bus.value("eos.cue.active.number") or ""),
        )
        self.record_panel.show_markers(self.markers.markers)

    def _refresh_sacn_status(self) -> None:
        """The sACN label, and a pin for what happened without anyone choosing it.

        Pinned: sending stopped for a reason of its own -- the network gone, a
        send failing -- once that has lasted SACN_PIN_AFTER; and Automatic
        moving a running stream to another network, which is no error but loses
        every receiver on the first. Each message is pinned once however often
        it recurs, so a flaky cable cannot push a failed take's pin off the list.
        """
        if not self.show_file.sacn.enabled:
            self._sacn_status.setText("   sACN: off")
            self._sacn_status.setStyleSheet("color: #7f8c8d;")
            self._sacn_status.setToolTip("")
            self._sacn_error_since = None
            return
        sender = self.sacn
        sender.refresh_staleness()
        status = sender.status
        words = {
            ConnectionState.LIVE: "sending",
            ConnectionState.ERROR: "not sending",
            ConnectionState.STALE: "stalled",
        }
        self._sacn_status.setText(
            f"   ● sACN: {words.get(status.state, status.state.value.lower())}"
        )
        self._sacn_status.setStyleSheet(
            f"color: {STATE_COLOURS.get(status.state, '#7f8c8d')};"
        )
        warning = sender.choice.warning if sender.choice is not None else ""
        self._sacn_status.setToolTip(
            "\n\n".join(text for text in (status.detail, warning) if text)
        )
        if status.state is ConnectionState.ERROR and not sender.settings.problem():
            now = time.monotonic()
            if self._sacn_error_since is None:
                self._sacn_error_since = now
            if now - self._sacn_error_since >= self.SACN_PIN_AFTER and status.detail:
                self._pin_once(f"sACN status output stopped sending: {status.detail}")
        else:
            self._sacn_error_since = None
        if sender.moves > self._sacn_moves_seen:
            self._sacn_moves_seen = sender.moves
            self._pin_once(f"sACN status output: {sender.last_move}")

    def _pin_once(self, message: str) -> None:
        """Pin a message unless it is pinned already."""
        if message not in self._pinned_messages:
            self._pin_status_message(message)

    # --------------------------------------------------------------- actions

    def open_help(self, topic: str = "start") -> None:
        """Open the Help window at a topic, reusing it if already open."""
        if getattr(self, "_help", None) is None:
            self._help = HelpWindow(self)
        self._help.show_topic(topic)

    def _open_recordings_folder(self) -> None:
        directory = self.show_file.recording.resolved_directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._reveal(directory)
        except OSError as exc:
            QMessageBox.warning(self, "Could not open folder", f"{directory}\n\n{exc}")

    def _open_snapshots_folder(self) -> None:
        directory = self.show_file.recording.resolved_snapshot_directory()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._reveal(directory)
        except OSError as exc:
            QMessageBox.warning(self, "Could not open folder", f"{directory}\n\n{exc}")

    def _open_log_folder(self) -> None:
        directory = log_dir()
        log.info("Opening log folder: %s", directory)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._reveal(directory)
        except OSError as exc:
            log.exception("Could not open log folder")
            QMessageBox.warning(
                self, "Could not open log folder", f"{directory}\n\n{exc}"
            )

    @staticmethod
    def _reveal(path: Path) -> None:
        """Show a folder in Explorer.

        os.startfile is the right call and exists only on Windows, which is
        fine - Wer is Windows-only - but guard it so this
        stays importable for tests on any machine.
        """
        startfile = getattr(os, "startfile", None)
        if startfile is not None:
            startfile(str(path))
        else:  # pragma: no cover - non-Windows safety net
            subprocess.run(["xdg-open", str(path)], check=False)

    @staticmethod
    def _bundled_text(*parts: str) -> str | None:
        """Read a text file shipped inside the build, or None if it is absent.

        None rather than an exception: a missing licence file is a packaging
        fault worth shouting about in the log, but it must not be the reason
        somebody cannot open the dialog that tells them their rights.
        """
        path = resource_dir().joinpath(*parts)
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
        log.error("Bundled licence text not found: %s", path)
        return None

    def _licence_text(self) -> str:
        """Wer's notice, who else's work is in here, then the full licence texts.

        Not decoration, and not only about Wer. Three obligations meet in this
        one dialog:

        - GPL v3 section 5, for Wer itself and for the GPL ffmpeg it bundles:
          whoever holds a copy is told the terms and how to get the source.
        - LGPL v3 section 4(a), for Qt: prominent notice that the library is
          used and is covered by that licence.
        - LGPL v3 section 4(c): because this program displays copyright notices
          while it runs, Qt's copyright notice belongs among them, together
          with a pointer to where both licence texts can be read.

        Every text is read from the bundle rather than embedded here, so it
        cannot drift out of sync with the binaries actually shipped. The
        component list lives in THIRD-PARTY-NOTICES.md for the same reason: one
        file, not one copy per place that needs to say it.
        """
        notices = self._bundled_text("THIRD-PARTY-NOTICES.md")
        gpl = self._bundled_text("vendor", "ffmpeg", "LICENSE")
        lgpl = self._bundled_text("licences", "qt", "LGPL-3.0.txt")

        rule = "-" * 72
        out = [
            licensing.NOTICE,
            "",
            "Qt is used under the GNU Lesser General Public License version 3.",
            "Copyright (C) The Qt Company Ltd. and other contributors.",
            "Qt is not modified. This build keeps its libraries replaceable,",
            "which is the condition that carries. The GNU GPL and the GNU LGPL",
            "are both reproduced in full below, and ship as files too: the GPL",
            "as LICENSE beside the application, and again at _internal\\LICENSE;",
            "the LGPL at _internal\\licences\\qt\\LGPL-3.0.txt.",
            "",
            rule,
            "",
        ]

        if notices:
            out.append(notices)
        else:
            # Say what is in here even when the file listing it is not.
            out += [
                "The third-party notices file is missing from this build.",
                "That is a packaging fault, not a licensing one. Wer bundles",
                "Qt (LGPL v3), FFmpeg (GPL v3), OpenCV (Apache 2.0),",
                "CPython (PSF), OpenSSL (Apache 2.0), NumPy and OpenBLAS",
                "(BSD), pygrabber and comtypes (MIT).",
                f"Write to {licensing.CONTACT_EMAIL} for the full notices.",
            ]

        for title, text, fallback in (
            (
                "GNU GENERAL PUBLIC LICENSE v3 - Wer, and the bundled FFmpeg 8.1.2",
                gpl,
                "https://www.gnu.org/licenses/gpl-3.0.txt",
            ),
            (
                "GNU LESSER GENERAL PUBLIC LICENSE v3 - Qt, via PySide6",
                lgpl,
                "https://www.gnu.org/licenses/lgpl-3.0.txt",
            ),
        ):
            out += ["", rule, "", title, ""]
            out.append(
                text
                if text
                else f"Missing from this build. Read it at {fallback}"
            )

        return "\n".join(out)

    def _show_licences(self) -> None:
        """Put the terms in front of whoever asks, as section 5 requires."""
        text = self._licence_text()

        dialog = QDialog(self)
        dialog.setWindowTitle("Licences")
        dialog.resize(760, 620)
        layout = QVBoxLayout(dialog)

        heading = QLabel(self._licence_heading_html())
        heading.setOpenExternalLinks(True)
        heading.setWordWrap(True)
        layout.addWidget(heading)

        body = QPlainTextEdit()
        body.setPlainText(text)
        body.setReadOnly(True)
        body.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        body.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(body)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def _licence_heading_html(self) -> str:
        """The line above the licence texts: the terms in one breath, then
        where the source is. A function for the same reason as _about_html.

        The Wer link is section 6(d) -- the public repository the build was
        made from -- and for anyone who reads no further it is the whole of
        Wer's section 6 compliance. A contact address here instead would
        not be a route section 6 allows; wer.licensing says why. Both links
        are shown as bare host and path so the address can be read off the
        screen and typed elsewhere, since the machine Wer runs on is not
        always one that can open a browser to it.
        """
        repository = licensing.SOURCE_REPOSITORY
        shown = repository.split("://", 1)[-1]
        return (
            "<b>Wer</b> is free software under the <b>GNU General Public "
            "License v3</b>, and so is the <b>FFmpeg 8.1.2</b> it bundles, "
            "which is included unmodified and run as a separate program. "
            "<b>Qt</b> is used under the <b>LGPL v3</b>, unmodified, with its "
            "libraries left replaceable. Everything else bundled is listed "
            "below.<br>"
            f"Source for Wer: <a href='{repository}'>{shown}</a>. "
            "Source for ffmpeg: "
            "<a href='https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz'>"
            "ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz</a>"
        )

    def _about_html(self) -> str:
        """What the About box says. A function, so a test can read it without
        putting a modal dialog on the screen and waiting for somebody."""
        ffmpeg = ffmpeg_path()
        return (
            f"<h3>{APP_DISPLAY_NAME} {__version__}</h3>"
            "<p>A Windows video-overlay recorder for lighting.</p>"
            f"<p><b>Python</b> {sys.version.split()[0]}<br>"
            f"<b>ffmpeg</b> {'bundled (GPL v3)' if ffmpeg else 'not found'}</p>"
            + licensing.summary_html()
        )

    def _show_about(self) -> None:
        QMessageBox.about(self, f"About {APP_NAME}", self._about_html())

    # -------------------------------------------------------------- shutdown

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Stop every thread before the window goes away.

        A recording in progress is finished properly rather than abandoned: the
        ffmpeg process needs its stdin closed and time to write the trailer, or
        the file will not seek.
        """
        log.info("Shutting down")
        self._shutting_down = True
        # A start-up query answering from here on must not open the camera on
        # a window that is going away, and nor may the fallback timer.
        self._startup.cancel()
        if self._help is not None:
            self._help.close()
        # Asked of the TAKE, not of the recorder. recorder.is_recording goes
        # False inside Recorder.stop(), which the finishing thread does not
        # reach until after _stop_pump()'s join -- so a close landing in that
        # gap (measured at 214 ms, longer whenever the pump is slow to join)
        # saw a take still "recording" that was already being finished, and ran
        # a second, concurrent finish here on the main thread: two sets of
        # sidecars for one take, two STOP markers, two chapter rewrites of the
        # same file, and one run deleting the sidecars the other was reading.
        # _take_in_progress is cleared by every path that hands a take to the
        # finisher, before it does so, and only on this thread. It also covers
        # the opposite gap: a take whose recorder has already died still has
        # footage on disk that needs its markers.
        if self._take_in_progress:
            # "Finished properly" means everything _stop_recording does, not
            # just closing the video. This path used to stop the recorder and
            # nothing else: no STOP marker, no markers CSV, no chapters, no
            # embedding, and the bus log left open at zero bytes -- so quitting
            # mid-take delivered a perfectly good video with every cue time in
            # it thrown away. The X button, File > Quit and a Windows logoff
            # all come through here.
            #
            # It runs on the finishing thread, joined below with a bound,
            # rather than inline. Inline it was UNBOUNDED: _write_sidecars
            # reaches embed_chapters, a stream-copy rewrite of the whole
            # recording (~28 s for a 15 GB four-hour take on this machine's
            # NVMe at 542 MB/s, minutes on a USB or spinning disk), and
            # embed_chapters' own timeout only guards process.wait() -- an
            # ffmpeg that wedges without exiting blocks the stderr readline
            # loop before that, forever, and the window never closes. Windows
            # force-terminates an app that stops pumping messages during
            # logoff, so "forever" here means the take is killed anyway, with
            # no say in what survives. Bounded, the worst case is a rewrite
            # that did not finish -- and because the sidecar cleanup only runs
            # after a successful embed, the cue times are still on disk beside
            # the video.
            log.info("Finishing the recording in progress before exit")
            capture = self.preview.capture
            if capture is not None:
                capture.feed_encoder.clear()
            self._take_in_progress = False
            self.system.stop_recording()
            self._finish_take(
                self.recorder.stats.elapsed,
                capture.settings.fps if capture is not None else 30.0,
                self.show_file.recording,
            )
        # Before the wait for finishing takes, not with the other connections
        # after it: Windows can end the process during that wait at logoff, and
        # nothing after it runs. Stopping sends the address at 0 and then ends
        # the stream (SacnSender._end_stream), so a receiver that holds its last
        # look holds 0, not wherever the pulse had got to.
        try:
            self.sacn.stop()
        except Exception:  # noqa: BLE001
            log.exception("Error stopping the sACN output")
        self._name_takes_shutdown_waits_for()
        # One deadline for all of them, not one each: FINISH_JOIN_TIMEOUT is
        # how long shutdown may take in total, and a second take finishing
        # behind the first must not double it.
        deadline = time.monotonic() + FINISH_JOIN_TIMEOUT
        for finisher in self._finishers:
            if finisher.is_alive():
                finisher.join(timeout=max(0.0, deadline - time.monotonic()))
        self._abandon_unfinished_takes()

        self._autosave()
        self.settings_claim.release()
        self.finder.stop()
        self.bridge.stop()
        self.preview.stop_capture()
        for connection in (self.eos, self.manual, self.system):
            try:
                connection.stop()
            except Exception:  # noqa: BLE001
                log.exception("Error stopping connection %s", connection.id)
        super().closeEvent(event)

    def _name_takes_shutdown_waits_for(self) -> None:
        """Say in the log which takes are still finishing, before waiting on them.

        Before the wait, not after it. A Windows logoff comes through
        closeEvent, Windows force-terminates an app that stops pumping messages
        during logoff, and this wait pumps none -- for up to its whole
        FINISH_JOIN_TIMEOUT when a long take is rewriting on a USB disk. Ended
        inside the wait, nothing after it runs: not the stop, not the delete,
        not the line _abandon_unfinished_takes writes. The log's last word on
        the take stayed "Embedding chapters into take.mkv", which reads as a
        rewrite going well, and the full-size .chapters-tmp copy beside the
        video had nothing to say what it was.

        That copy is safe to delete whenever it is left: embed_chapters swaps
        it in with a single replace, so while it exists the original has not
        been touched.
        """
        for finisher in self._finishers:
            if not finisher.is_alive():
                continue
            control = getattr(finisher, "embed", None)
            temporary = control.temporary if control is not None else None
            log.warning(
                "Waiting up to %.0f s for %s, which is still %s, before Wer "
                "closes.%s",
                FINISH_JOIN_TIMEOUT,
                getattr(finisher, "take_name", "a take"),
                getattr(finisher, "stage", "being finished"),
                f" If Wer is ended first, {temporary.name} may be left beside "
                f"it, and is safe to delete." if temporary is not None else "",
            )

    def _abandon_unfinished_takes(self) -> None:
        """Deal with whatever is still finishing once shutdown has waited enough.

        This used to be nothing at all. closeEvent carried on, the app exited,
        and the daemon finishing thread died with the interpreter partway
        through its rewrite: the ffmpeg it had launched went on with nobody
        reading it, the full-size .chapters-tmp copy was never swapped in and
        never deleted, and the log's last word on the take was "Embedding
        chapters into take.mkv" -- no error, nothing to say the rewrite had
        been abandoned. Closing the window a minute after stopping a long take
        on a USB disk was enough.

        Now each one still running is named in the log with what it was left
        doing, and its rewrite is stopped. The stop reaches ffmpeg through the
        take's EmbedControl; the finishing thread, which owns the process,
        deletes the copy, and is given FINISH_CANCEL_TIMEOUT to do so before
        the app goes.
        """
        unfinished = [finisher for finisher in self._finishers if finisher.is_alive()]
        if not unfinished:
            return
        for finisher in unfinished:
            control = getattr(finisher, "embed", None)
            name = getattr(finisher, "take_name", "a take")
            if control is not None and control.running and control.video is not None:
                log.error(
                    "Shutdown could not wait any longer for %s: stopping the "
                    "chapter rewrite of %s. That file is untouched but has no "
                    "chapters, and the cue times stay in the sidecar files "
                    "beside it.",
                    name, control.video.name,
                )
            else:
                log.error(
                    "Shutdown could not wait any longer for %s, which was still "
                    "%s; that did not finish.",
                    name, getattr(finisher, "stage", "being finished"),
                )
            if control is not None:
                control.cancel("Wer was closed before the rewrite finished")

        deadline = time.monotonic() + FINISH_CANCEL_TIMEOUT
        for finisher in unfinished:
            finisher.join(timeout=max(0.0, deadline - time.monotonic()))
            if finisher.is_alive():
                control = getattr(finisher, "embed", None)
                leftover = control.temporary if control is not None else None
                log.error(
                    "%s was still finishing when Wer closed%s",
                    getattr(finisher, "take_name", "A take"),
                    f"; {leftover.name} may be left beside it, and is safe to delete"
                    if leftover is not None else "",
                )
