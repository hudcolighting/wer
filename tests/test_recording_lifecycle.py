"""What happens to a take when something changes underneath it.

Every test in here is about the same class of failure: the app carries on
saying "Recording" while nothing is reaching the file. That is the failure that
costs a whole session, because nobody is in the booth to see it and the status
bar keeps counting up regardless.

No real camera is opened anywhere in this file. The camera is replaced with a
synthetic frame source that behaves like ``CameraCapture`` -- same queues, same
stats, same ``feed_encoder`` gate -- because these bugs are all in the wiring
around capture, not in capture itself, and a test that needs a webcam is a test
that does not run on the machine where it matters.

A few tests do run the real bundled ffmpeg, because the thing they prove is
whether markers and chapters actually landed in the file on disk, and which
sidecars are left beside it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QObject, Signal

from wer.video import capture as capture_module
from wer.video.capture import CameraCapture, CaptureSettings, CaptureStats, Frame
from wer.video.devices import CaptureDevice, VideoFormat
from wer.video.recorder import RecorderState, RecordingPart

#: Video never goes on C:. A generated take is small, but the rule exists
#: because an unbounded ffmpeg graph filled a system disk once already.
SCRATCH_VIDEO = Path("D:/wer-scratch/video")


# --------------------------------------------------------------- fake devices

FORMATS = [
    VideoFormat(1280, 720, 15.0, 30.0, "MJPG"),
    VideoFormat(640, 360, 15.0, 30.0, "MJPG"),
    VideoFormat(640, 360, 15.0, 30.0, "NV12"),
]

DEVICES = [CaptureDevice(0, "Fake Cam A"), CaptureDevice(1, "Fake Cam B")]


class SyntheticCapture(CameraCapture):
    """A CameraCapture that makes its own frames instead of opening a device.

    Subclassed rather than mocked so the queues, the stats and the
    ``feed_encoder`` gate are the real ones -- those are exactly the parts the
    bugs here run through.
    """

    #: Every instance ever created, in order, so a test can see a restart.
    instances: list[SyntheticCapture] = []

    def __init__(self, settings: CaptureSettings, *, on_error=None) -> None:
        super().__init__(settings, on_error=on_error)
        SyntheticCapture.instances.append(self)

    def open(self) -> bool:
        self._backend_name = "FAKE"
        self._actual = (self.settings.width, self.settings.height, self.settings.fps)
        return True

    def start(self) -> None:
        self._stop.clear()
        self.stats = CaptureStats(started_at=time.perf_counter())
        self._thread = threading.Thread(
            target=self._produce, name="synthetic-capture", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _produce(self) -> None:
        blank = np.zeros(
            (self.settings.height, self.settings.width, 3), dtype=np.uint8
        )
        index = 0
        while not self._stop.is_set():
            now = time.perf_counter()
            frame = Frame(image=blank.copy(), timestamp=now, index=index)
            index += 1
            self.stats.note_frame(now)
            self._offer_preview(frame)
            if self.feed_encoder.is_set():
                self._offer_encoder(frame)
            self._stop.wait(1.0 / 30.0)


class SyncProbe(QObject):
    """FormatProbe without the thread, so a test does not have to pump events."""

    finished = Signal(object)

    def probe(self, devices) -> None:
        self.finished.emit({device.index: list(FORMATS) for device in devices})


@pytest.fixture
def fake_camera(monkeypatch):
    """Replace enumeration, probing and the camera itself. Opens nothing."""
    from wer.ui import preview as preview_module

    SyntheticCapture.instances = []
    monkeypatch.setattr(
        preview_module, "enumerate_video_devices", lambda: list(DEVICES)
    )
    monkeypatch.setattr(preview_module, "probe_formats", lambda device: list(FORMATS))
    monkeypatch.setattr(preview_module, "FormatProbe", SyncProbe)
    monkeypatch.setattr(preview_module, "CameraCapture", SyntheticCapture)
    yield SyntheticCapture
    for instance in SyntheticCapture.instances:
        instance.stop()
    SyntheticCapture.instances = []


@pytest.fixture
def panel(qt_app, fake_camera):
    """A real PreviewPanel with a synthetic camera already live."""
    from wer.ui.preview import PreviewPanel

    widget = PreviewPanel()
    yield widget
    widget.stop_capture()
    widget.deleteLater()


# ------------------------------------------------------------- a dead camera


class _Clock:
    """Stands in for the time module inside wer.video.capture."""

    def __init__(self) -> None:
        self.t = 1000.0

    def perf_counter(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:  # pragma: no cover - never called
        pass


def test_measured_fps_stops_claiming_a_rate_once_frames_stop(monkeypatch) -> None:
    """A camera that dies must not keep reporting the rate it had when alive.

    Reproduced against the real thing: unplug the camera 3 s in and the stats
    line still said "29.65 fps measured" fifteen seconds later, because the
    sample window only ever advances on a successful read.
    """
    clock = _Clock()
    monkeypatch.setattr(capture_module, "time", clock)

    stats = CaptureStats()
    for _ in range(30):
        clock.t += 1 / 30
        stats.note_frame(clock.t)

    assert stats.is_live
    assert stats.measured_fps == pytest.approx(30.0, abs=0.5)

    clock.t += 10.0  # the camera has been gone for ten seconds
    assert not stats.is_live
    assert stats.measured_fps == 0.0, (
        "the one number that reveals a sick camera is reporting health on a "
        "dead one"
    )


def test_the_stats_line_says_no_signal_rather_than_a_healthy_rate(
    qt_app, monkeypatch, fake_camera
) -> None:
    """The 500 ms readout must lead with the camera being gone."""
    from wer.ui import preview as preview_module
    from wer.ui.preview import PreviewPanel

    monkeypatch.setattr(preview_module, "enumerate_video_devices", lambda: [])
    widget = PreviewPanel()
    try:
        camera = SyntheticCapture(CaptureSettings(width=640, height=360))
        camera.open()
        now = time.perf_counter()
        for _ in range(30):
            now += 1 / 30
            camera.stats.note_frame(now)
        # Frames stopped a while ago; last_frame_at is far in the past.
        camera.stats.last_frame_at = time.perf_counter() - 30.0
        widget.capture = camera

        widget._refresh_stats()
        text = widget._stats.text()
        assert "NO SIGNAL" in text, text
        assert "30.0 fps measured" not in text, text
    finally:
        widget.deleteLater()


def test_a_capture_failure_reaches_the_screen_from_the_capture_thread(
    qt_app, monkeypatch
) -> None:
    """QTimer.singleShot on a thread with no event loop never fires.

    That is where the "the camera stopped delivering frames" message went: the
    capture thread raised it, the timer was created on that thread, and the
    preview went on showing the last good frame with no banner at all.
    """
    from wer.ui import preview as preview_module
    from wer.ui.preview import PreviewPanel

    monkeypatch.setattr(preview_module, "enumerate_video_devices", lambda: [])
    widget = PreviewPanel()
    try:
        message = "The camera stopped delivering frames."
        worker = threading.Thread(target=widget._on_capture_error, args=(message,))
        worker.start()
        worker.join(5.0)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and widget._surface.text() != message:
            qt_app.processEvents()
            time.sleep(0.01)
        assert widget._surface.text() == message
    finally:
        widget.deleteLater()


# ------------------------------------------------- the controls during a take


def test_the_camera_controls_are_locked_while_recording(panel) -> None:
    """Refresh and the Device box end the take's video. They must be dead
    while a take is rolling."""
    assert panel._device_box.isEnabled()
    panel.set_recording(True)
    assert not panel._device_box.isEnabled()
    assert not panel._format_box.isEnabled()
    assert not panel._refresh_button.isEnabled()

    panel.set_recording(False)
    assert panel._device_box.isEnabled()
    assert panel._format_box.isEnabled()
    assert panel._refresh_button.isEnabled()


def test_refresh_during_a_take_leaves_the_camera_alone(panel) -> None:
    """Belt and braces: even called directly, a refresh mid-take must not
    swap the object the encoder pump is reading."""
    live = panel.capture
    panel.set_recording(True)
    panel.refresh_devices()
    assert panel.capture is live


# ------------------------------------------------- device and format pickers


def test_choosing_another_camera_actually_opens_it(panel) -> None:
    """Picking Camera B used to stop Camera A and start nothing at all: the
    picture died and the only way back was Refresh, which reverted to A."""
    assert panel.capture is not None
    panel._device_box.setCurrentIndex(1)

    assert panel.capture is not None and panel.capture.is_running
    assert panel.capture.settings.device_index == 1


def test_refresh_comes_back_on_the_camera_you_chose(panel) -> None:
    """Re-enumeration must not silently revert to device 0."""
    panel._device_box.setCurrentIndex(1)
    panel.refresh_devices()
    assert panel._device_box.currentIndex() == 1
    assert panel.capture is not None
    assert panel.capture.settings.device_index == 1


def test_choosing_a_format_reaches_the_camera(panel) -> None:
    """The Format box was wired to nothing: it changed its own text and the
    camera went on capturing whatever best_format had picked."""
    wanted = FORMATS.index(VideoFormat(640, 360, 15.0, 30.0, "NV12"))
    panel._format_box.setCurrentIndex(wanted)

    settings = panel.capture.settings
    assert (settings.width, settings.height, settings.fourcc) == (640, 360, "NV12")


def test_refresh_keeps_the_format_you_chose(panel) -> None:
    panel._format_box.setCurrentIndex(2)
    panel.refresh_devices()
    assert panel._format_box.currentData() == FORMATS[2]
    assert panel.capture.settings.fourcc == "NV12"


# ------------------------------------------------------- the whole main window


class StubRecorder:
    """Everything MainWindow asks of a Recorder, without an ffmpeg.

    The bugs below are in the window's bookkeeping, not in the encoder, and a
    stub keeps these tests at a second apiece. The one test that has to prove
    bytes landed in a file runs the real recorder.
    """

    def __init__(self) -> None:
        self.state = RecorderState.IDLE
        self.detail = ""
        self.parts: list[RecordingPart] = []
        self.submitted = 0
        self.stopped = 0
        self.output: Path | None = None
        self.low_disk_bytes = 0
        self.stop_below_bytes = 0
        self.restart_on_failure = True

        class _Stats:
            started_at = 0.0
            # Never set: the stub writes no frames, so a bus log keeps to
            # started_at, as a real take does when start() stops waiting first.
            first_frame_at = 0.0
            output_path: Path | None = None

            @property
            def elapsed(self) -> float:
                return time.perf_counter() - self.started_at if self.started_at else 0.0

        self.stats = _Stats()

    @property
    def is_recording(self) -> bool:
        return self.state in (RecorderState.RECORDING, RecorderState.STARTING)

    def start(self, settings, output, **geometry) -> bool:
        # Recorder.start() assigns a NEW parts list, and anything still holding
        # the old one is holding the previous take's. Faithful here on purpose.
        self.parts = []
        self.output = output
        self.stats.started_at = time.perf_counter()
        self.stats.output_path = output
        self.state = RecorderState.RECORDING
        return True

    def submit(self, frame) -> bool:
        self.submitted += 1
        return True

    def stop(self) -> Path | None:
        self.stopped += 1
        if self.state is not RecorderState.ERROR:
            self.state = RecorderState.IDLE
        return self.parts[-1].path if self.parts else None


def wait_for(qt_app, predicate, timeout: float = 15.0) -> bool:
    """Pump the event loop until something becomes true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        qt_app.processEvents()
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def dialogs(monkeypatch):
    """Catch every modal instead of letting one park forever.

    A test that opens a real QMessageBox never finishes, and neither does an
    unattended tech.
    """
    from PySide6.QtWidgets import QMessageBox

    raised: list[str] = []

    def spy(parent, title, text, *args, **kwargs):
        raised.append(title)
        return QMessageBox.StandardButton.No

    for name in ("warning", "critical", "question", "information"):
        monkeypatch.setattr(QMessageBox, name, spy)
    return raised


@pytest.fixture
def window(qt_app, fake_camera, dialogs, tmp_path):
    """A real MainWindow on a synthetic camera and a stubbed recorder."""
    from wer.ui.main_window import MainWindow

    win = MainWindow()
    win.recorder = StubRecorder()
    config = win.show_file.recording
    config.directory = str(tmp_path / "takes")
    # Whatever the machine running this has free is not the subject.
    config.low_disk_stop_gb = 0.0
    config.low_disk_warning_gb = 0.0
    # The camera starts once the camera format probe and the audio check have
    # answered, and they answer by queued signal, so the loop has to run first.
    assert wait_for(qt_app, lambda: win.preview.capture is not None, 10.0), (
        "the synthetic camera never started"
    )
    yield win
    win.close()


def test_the_encoder_pump_follows_the_camera_it_is_given(window, qt_app) -> None:
    """The pump closed over the CameraCapture that existed when the take
    started. Anything that replaced that object -- Refresh, a device change --
    left the pump draining a dead queue for the rest of the session while the
    preview showed a live picture and the recorder kept timing.

    The panel's controls are locked during a take now, so this reaches past
    them and does what a restart does directly: the pump must survive it.
    """
    win = window
    win._start_recording()
    assert win.recorder.is_recording
    assert wait_for(qt_app, lambda: win.recorder.submitted > 0)

    first = win.preview.capture
    win.preview.stop_capture()
    win.preview.start_capture()
    assert win.preview.capture is not first

    submitted = win.recorder.submitted
    assert wait_for(qt_app, lambda: win.recorder.submitted > submitted + 5), (
        "the recorder stopped receiving frames when the camera was replaced, "
        "and said nothing"
    )
    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)


def test_a_recorder_error_ends_the_take_instead_of_pretending(
    window, qt_app, tmp_path
) -> None:
    """When ffmpeg dies for good the window used to do nothing but log it.

    The controls an operator actually uses -- the Preview tab's big button and
    Ctrl+R -- went on saying "Stop Recording" while the status bar said "Not
    recording", and pressing either one started a NEW take, clearing the failed
    take's markers before anything had been written for them.
    """
    win = window
    config = win.show_file.recording
    config.embed_markers = False        # the sidecars are the point here
    config.keep_sidecar_files = True

    win._start_recording()
    win.markers.add_manual(1.0, "the moment that matters")

    part_path = tmp_path / "failed_take.mkv"
    part_path.write_bytes(b"not really a video, but a file that exists")
    win.recorder.parts = [
        RecordingPart(path=part_path, session_offset=0.0, duration=5.0, frames=150)
    ]

    # Exactly what Recorder does when it gives up: state first, then the
    # listener, on the writer thread.
    win.recorder.state = RecorderState.ERROR
    win._on_recorder_state(
        RecorderState.ERROR, "ffmpeg failed 5 times; giving up rather than restarting."
    )
    assert wait_for(qt_app, lambda: not win._finishers)

    assert win._record_action.text() == "Start &Recording"
    assert not win.preview._recording, "the preview REC indicator is still lit"

    csv_path = part_path.with_suffix(".markers.csv")
    assert csv_path.is_file(), "the failed take got no markers at all"
    assert "the moment that matters" in csv_path.read_text(encoding="utf-8")


def test_the_controls_an_operator_uses_still_start_and_stop_a_take(
    window, qt_app
) -> None:
    """Ctrl+R, the Preview tab's button and the Recording tab's button are the
    three ways in. They go through a start that now takes a keyword argument;
    Qt hands slots whatever the signal carries, so this checks all three still
    connect to something that works."""
    win = window

    win._record_action.trigger()
    assert win.recorder.is_recording
    assert win._record_action.text() == "Stop &Recording"

    win.preview._record_button.click()
    assert wait_for(qt_app, lambda: not win._finishers)
    assert not win.recorder.is_recording

    win.record_panel.record_requested.emit()
    assert win.recorder.is_recording
    win.record_panel.stop_requested.emit()
    assert wait_for(qt_app, lambda: not win._finishers)
    assert not win.recorder.is_recording


def test_an_osc_start_never_waits_on_a_dialog(window, dialogs, qt_app) -> None:
    """A remote start parks a modal on a laptop nobody is sitting at, and the
    take that was asked for never happens. The same file already reasons this
    way about remote markers."""
    win = window
    # Force the low-disk question, whatever the machine actually has free.
    win.show_file.recording.low_disk_warning_gb = 100_000.0

    win._handle_osc_command("/wer/record/start", None)

    assert dialogs == [], f"a remote start raised {dialogs}"
    assert win.recorder.is_recording, "the take asked for over OSC never started"
    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)


def test_an_osc_start_with_no_camera_says_so_without_a_modal(
    window, dialogs
) -> None:
    """A refusal still has to be reported -- in the status bar and the log,
    where a remote caller's failure belongs, not in a dialog that stacks up one
    copy per attempt."""
    win = window
    win.preview.stop_capture()

    win._handle_osc_command("/wer/record/start", None)
    win._handle_osc_command("/wer/record/start", None)

    assert dialogs == []
    assert not win.recorder.is_recording
    assert "camera" in win.statusBar().currentMessage().lower()


def test_the_camera_feeds_the_take_before_the_recorder_starts(window, qt_app) -> None:
    """ffmpeg opens the audio input and then reads the frame waiting in its
    pipe, and the take's sync rests on one being there. The feed was armed only
    once the recorder had started, so the first frame reached ffmpeg after its
    input had opened, and every take's sound was 280-790 ms early.

    So by the time Recorder.start() is called the camera is feeding the encoder
    queue, the pump is moving frames into the recorder, the camera's drop count
    has been noted, and the take has its own marker log: a cue fired while the
    recorder starts belongs to this take, not to the last take's log."""
    win = window
    capture = win.preview.capture
    last_takes_markers = win.markers
    seen: dict[str, object] = {}

    class Watching(StubRecorder):
        def start(self, settings, output, **geometry) -> bool:
            pump = win._pump_thread
            seen["feeding"] = capture.feed_encoder.is_set()
            seen["pumping"] = pump is not None and pump.is_alive()
            seen["drops noted"] = win._take_capture_stats is capture.stats
            seen["own log"] = win.markers is not last_takes_markers
            seen["markers"] = len(win.markers)
            deadline = time.monotonic() + 3.0
            while self.submitted == 0 and time.monotonic() < deadline:
                time.sleep(0.02)
            seen["frames"] = self.submitted
            return super().start(settings, output, **geometry)

    win.recorder = Watching()
    win._start_recording()
    try:
        assert seen["feeding"], "the camera was not feeding the encoder queue"
        assert seen["pumping"], "nothing was moving frames into the recorder"
        assert seen["frames"], "no frame reached the recorder while it started"
        assert seen["drops noted"], "the camera's drop count was not noted first"
        assert seen["own log"], "a cue fired now would go into the last take's log"
        assert seen["markers"] == 1, "the new log should hold its START and no more"
        assert win.recorder.is_recording
    finally:
        win._stop_recording()
        assert wait_for(qt_app, lambda: not win._finishers)


def test_a_refused_start_leaves_nothing_armed(window, monkeypatch) -> None:
    """With the feed armed before the recorder starts, a start the recorder
    refuses -- an audio input ffmpeg cannot open is refused there now, with
    ffmpeg's own words -- has to be taken down again before it is reported.
    Left armed, the camera would go on queueing frames and the pump feeding
    them for a take that never happened, behind a modal waiting for someone to
    read it, and the status bar would count the markers of a log with no take."""
    from PySide6.QtWidgets import QMessageBox

    win = window
    capture = win.preview.capture
    last_takes_markers = win.markers
    armed_when_started: list[bool] = []
    armed_when_told: list[tuple[bool, bool, bool]] = []

    class Refusing(StubRecorder):
        def start(self, settings, output, **geometry) -> bool:
            armed_when_started.append(capture.feed_encoder.is_set())
            self.state = RecorderState.ERROR
            self.detail = (
                "ffmpeg could not open the audio device. Check the Audio "
                "setting, or set it to None to record video only."
            )
            return False

    def told(parent, title, text, *args, **kwargs):
        armed_when_told.append((
            capture.feed_encoder.is_set(),
            win._pump_thread is not None,
            win.markers is not last_takes_markers,
        ))
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "critical", told)
    win.recorder = Refusing()
    win._start_recording()

    assert armed_when_started == [True], "the feed was never armed; nothing was tested"
    assert armed_when_told == [(False, False, False)], (
        f"(feeding, pumping, a new log) when the refusal was shown: {armed_when_told}"
    )
    assert not capture.feed_encoder.is_set()
    assert win._pump_thread is None
    assert win.markers is last_takes_markers
    assert not win._take_in_progress
    assert win._record_action.text() == "Start &Recording"


def test_the_take_number_survives_a_restart(qt_app, fake_camera, monkeypatch) -> None:
    """ShowFile.take was loaded and then never applied, so every launch
    recorded take001 again -- and the first autosave wrote the 1 back over the
    stored number."""
    from wer.core.showfile import ShowFile
    from wer.ui import main_window as main_window_module

    show = ShowFile()
    show.take = 7
    monkeypatch.setattr(main_window_module, "load_autosave", lambda: show)

    win = main_window_module.MainWindow()
    try:
        assert win.system.take == 7
        win._autosave()
        assert show.take == 7, "the stored take number was overwritten with 1"
    finally:
        win.close()


def test_a_marker_sent_with_a_start_lands_in_the_new_take(window, qt_app) -> None:
    """A macro that starts a take and marks it at once.

    Both commands arrive on the Eos thread before the main thread has run
    either. The marker used to be stamped there with the recorder's elapsed
    time, which still counted from the previous take's start: 95 minutes into
    a take that had just begun, past its end, and dropped from the chapters
    without a word.
    """
    import threading

    from wer.connections.osc import OscMessage

    win = window
    # The last take started 95 minutes ago; the stats object outlives it, as
    # a real recorder's does.
    win.recorder.stats.started_at = time.perf_counter() - 95 * 60

    def the_desk() -> None:
        win.eos.commands.dispatch(OscMessage("/wer/record/start"))
        win.eos.commands.dispatch(OscMessage("/wer/marker", ("Act 2",), "s"))

    sender = threading.Thread(target=the_desk, name="test-desk")
    sender.start()
    sender.join(timeout=5.0)

    def act_two():
        return [m for m in win.markers.markers if m.note == "Act 2"]

    assert wait_for(qt_app, lambda: bool(act_two()), 5.0), "the marker never arrived"
    assert win.recorder.is_recording, "precondition: the desk's take did not start"
    assert act_two()[0].timestamp < 60.0, (
        f"the marker landed at {act_two()[0].timestamp:.0f} s, on the last take's clock"
    )
    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)


def _armed_take_with_a_part(win, tmp_path) -> Path:
    """Roll a take whose one part is a real file on disk."""
    win._start_recording()
    win.markers.add_manual(1.0, "cue 12")
    part_path = tmp_path / "embedtake.mkv"
    part_path.write_bytes(b"x" * 4096)
    win.recorder.parts = [
        RecordingPart(path=part_path, session_offset=0.0, duration=30.0, frames=900)
    ]
    return part_path


def test_a_failed_embed_is_not_announced_as_saved(
    window, qt_app, tmp_path, monkeypatch
) -> None:
    """Embedding rewrites the file and can fail -- most likely on the disk
    that just filled up. It was logged and nothing else, while the status bar
    said "Saved take.mkv (48 MB)" over a file with no chapters in it. The cue
    times are still recoverable from the sidecars, but only if anyone is told
    they are there."""
    from wer.ui import main_window as main_window_module
    from wer.video.remux import RemuxResult

    win = window
    win.show_file.recording.embed_markers = True
    monkeypatch.setattr(
        main_window_module,
        "embed_chapters",
        lambda video, chapters, **kwargs: RemuxResult(
            False, video, "No space left on device"
        ),
    )
    part_path = _armed_take_with_a_part(win, tmp_path)

    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)

    message = win.statusBar().currentMessage()
    assert "chapters" in message.lower(), message
    assert part_path.with_suffix(".markers.csv").is_file(), (
        "the sidecars are the only surviving copy of the cue times"
    )


def test_embedding_is_not_attempted_without_room_for_it(
    window, qt_app, tmp_path, monkeypatch
) -> None:
    """The rewrite writes a second copy before replacing the original: peak
    usage is 2.00x the recording, measured at the swap. Starting it on a disk
    that cannot hold one spends minutes to fail and leaves a half-written temp
    file behind."""
    from wer.ui import main_window as main_window_module
    from wer.video.recorder import DiskSpace
    from wer.video.remux import RemuxResult

    win = window
    win.show_file.recording.embed_markers = True
    part_path = _armed_take_with_a_part(win, tmp_path)

    attempts: list[Path] = []

    def record_attempt(video, chapters, **kwargs):
        attempts.append(video)
        return RemuxResult(True, video)

    monkeypatch.setattr(main_window_module, "embed_chapters", record_attempt)
    monkeypatch.setattr(
        main_window_module,
        "check_disk",
        lambda path: DiskSpace(free_bytes=1024, total_bytes=1_000_000),
    )

    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)

    assert attempts == [], "started a rewrite that could not possibly finish"
    assert part_path.with_suffix(".markers.csv").is_file()
    assert "chapters" in win.statusBar().currentMessage().lower()


def test_a_take_whose_chapters_could_not_be_written_keeps_its_marker_list_and_bus_log(
    window, qt_app, tmp_path, monkeypatch
) -> None:
    """Nothing was rewritten, so the video holds no sidecar to tidy away.

    A chapters file that could not be written is logged and passed over, and
    the tidy-up after the loop deleted the marker list and the bus log anyway
    with sidecars off: the only copies of both, from a file that held neither.
    """
    win = window
    config = win.show_file.recording
    config.embed_markers = True
    config.keep_sidecar_files = False
    part_path = _armed_take_with_a_part(win, tmp_path)
    bus_log = part_path.with_suffix(".bus.jsonl")
    bus_log.write_text(
        '{"t":1.0,"key":"eos.cue.active.number","value":"12"}\n', encoding="utf-8"
    )

    def unwritable(markers, part, chapters_path):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(win, "_write_part_chapters", unwritable)

    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)

    assert part_path.with_suffix(".markers.csv").is_file(), "the marker list was deleted"
    assert bus_log.is_file(), "the bus log was deleted"


# ------------------------------------------------------- with the real ffmpeg


@pytest.fixture
def recording_dir(tmp_path):
    """Somewhere to put a take.

    Video goes on D: on the machine this was written on and never on the
    system disk. Anywhere else, the pytest temp directory will do.
    """
    base = SCRATCH_VIDEO
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = tmp_path
    directory = base / f"lifecycle-{os.getpid()}-{int(time.time() * 1000)}"
    directory.mkdir(parents=True)
    yield directory
    # The assertions quote ffmpeg's own report of the file, so there is nothing
    # left to learn from the take itself.
    shutil.rmtree(directory, ignore_errors=True)


def ffmpeg_report(path: Path) -> str:
    """What ffmpeg says about a file. There is no ffprobe in the bundle."""
    from wer.paths import ffmpeg_path

    result = subprocess.run(
        [str(ffmpeg_path()), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return result.stderr


def test_closing_the_window_mid_take_keeps_the_markers(
    qt_app, fake_camera, dialogs, recording_dir
) -> None:
    """Quitting during a take wrote the video and threw away everything else.

    No STOP marker, no markers CSV, no chapters, no embedding and a bus log
    left open at zero bytes -- while the log said "Finishing the recording in
    progress before exit" and the Recording tab's own tooltip promised "one
    self-contained file: open it in VLC and every cue is a chapter". The video
    was fine; every cue time in it was gone.

    This one runs the real recorder and the bundled ffmpeg, because the claim
    is about what is inside the file on disk.
    """
    from wer.paths import ffmpeg_path
    from wer.ui.main_window import MainWindow

    if ffmpeg_path() is None:
        pytest.skip("no bundled ffmpeg")

    win = MainWindow()
    # The camera starts once the start-up queries have answered by queued
    # signal; see the window fixture.
    assert wait_for(qt_app, lambda: win.preview.capture is not None, 10.0), (
        "the synthetic camera never started"
    )
    config = win.show_file.recording
    config.directory = str(recording_dir)
    config.filename_template = "closetake"
    config.low_disk_stop_gb = 0.0
    config.low_disk_warning_gb = 0.0

    win._start_recording()
    assert dialogs == [], f"could not start a take: {dialogs}"
    assert wait_for(qt_app, lambda: win.recorder.stats.frames_written > 30, 20.0)
    # Half a second in: comfortably inside the file however slowly this
    # machine happens to be encoding, rather than on its last frame.
    win.markers.add_manual(0.5, "the moment that matters")
    assert wait_for(qt_app, lambda: win.recorder.stats.frames_written > 90, 20.0)

    win.close()

    video = recording_dir / "closetake.mkv"
    assert video.is_file(), sorted(p.name for p in recording_dir.iterdir())
    report = ffmpeg_report(video)
    assert "Chapters" in report, report
    assert "the moment that matters" in report, report
    # keep_sidecar_files is off by default: the marker log and the bus log
    # belong INSIDE the file, as attachments, not beside it as leftovers.
    assert "Attachment" in report, report
    empty = [p.name for p in recording_dir.iterdir() if p.stat().st_size == 0]
    assert not empty, f"left behind empty: {empty}"


# ----------------------------------- which sidecars an MP4 and an MKV keep


@pytest.fixture(scope="module")
def recorded_clips(tmp_path_factory):
    """A two-second MKV and a two-second MP4, each written by Wer's Recorder.

    Recorded once and copied into each test, which rewrites its copy with the
    real embed_chapters. Paced the way a camera delivers frames: ffmpeg times
    this pipe by arrival, so an unpaced clip lasts milliseconds and every
    chapter lands past its end. The software encoder is named rather than left
    to "auto", so nothing here can reach a GPU.
    """
    from wer.paths import ffmpeg_path
    from wer.video.encoder import SOFTWARE_ENCODER, Container, OutputSettings
    from wer.video.recorder import Recorder

    if ffmpeg_path() is None:
        pytest.skip("no bundled ffmpeg")
    base = SCRATCH_VIDEO
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = tmp_path_factory.mktemp("clips")
    directory = base / f"clips-{os.getpid()}-{int(time.time() * 1000)}"
    directory.mkdir(parents=True)
    width, height, fps = 320, 180, 30
    clips: dict[str, Path] = {}
    try:
        for container in (Container.MKV, Container.MP4):
            path = directory / f"clip.{container.value}"
            recorder = Recorder()
            assert recorder.start(
                OutputSettings(
                    quality_preset="tiny", encoder=SOFTWARE_ENCODER.name,
                    container=container,
                ),
                path, capture_width=width, capture_height=height, capture_fps=fps,
            ), recorder.detail
            started = time.perf_counter()
            for index in range(fps * 2):
                image = np.zeros((height, width, 3), np.uint8)
                image[:, index * 3:index * 3 + 20] = (30, 180, 240)
                recorder.submit(
                    Frame(image=image, timestamp=time.perf_counter(), index=index)
                )
                remaining = started + (index + 1) / fps - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)
            recorder.stop()
            assert path.is_file(), f"the Recorder wrote no {path.name}"
            clips[container.value] = path
        yield clips
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _armed_take_on_a_real_recording(win, clip: Path, directory: Path) -> Path:
    """Roll a take whose one part is a copy of a real recording.

    The recorder is still the stub, so the bus log the window starts goes
    beside the stub's output. One is written here where the finisher looks for
    the take's own: beside its first part.
    """
    win._start_recording()
    win.markers.add_manual(0.5, "cue 12")
    win.markers.add_manual(1.0, "cue 13")
    part_path = directory / f"realtake{clip.suffix}"
    shutil.copyfile(clip, part_path)
    part_path.with_suffix(".bus.jsonl").write_text(
        '{"t":0.5,"key":"eos.cue.active.number","value":"12"}\n', encoding="utf-8"
    )
    win.recorder.parts = [
        RecordingPart(path=part_path, session_offset=0.0, duration=2.0, frames=60)
    ]
    return part_path


def test_an_mp4_take_keeps_its_marker_list_and_bus_log_with_sidecars_off(
    window, qt_app, recorded_clips, recording_dir
) -> None:
    """An MP4 cannot hold attachments, so what is beside it is the only copy.

    Every sidecar was deleted once the rewrites succeeded. That was safe only
    because an MP4's rewrite never did: ffmpeg refused the attachments and
    failed it. With the chapters now going in, the same deletion would throw
    away the marker list and the bus log, which exists nowhere else, behind a
    take reported as saved. The chapters file can go: what it held is in the
    video now.
    """
    win = window
    config = win.show_file.recording
    config.embed_markers = True
    config.keep_sidecar_files = False
    part_path = _armed_take_on_a_real_recording(
        win, recorded_clips["mp4"], recording_dir
    )

    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)

    report = ffmpeg_report(part_path)
    assert "cue 12" in report and "cue 13" in report, report
    assert part_path.with_suffix(".markers.csv").is_file(), "the marker list was deleted"
    assert part_path.with_suffix(".bus.jsonl").is_file(), "the bus log was deleted"
    assert not part_path.with_suffix(".chapters.txt").exists()


def test_a_saved_mp4_take_reports_no_problem_and_says_its_sidecars_stay_beside_it(
    window, qt_app, recorded_clips, recording_dir
) -> None:
    """Chapters in, marker list and bus log beside: saved whole, and said so.

    Every MP4 take read "Not saved whole: the chapters could not be embedded",
    pinned for the morning. Nothing has gone wrong now, so nothing is pinned.
    What follows "Saved" tells whoever finds three files, where the Recording
    tab promised one, why they are there.
    """
    win = window
    config = win.show_file.recording
    config.embed_markers = True
    config.keep_sidecar_files = False
    part_path = _armed_take_on_a_real_recording(
        win, recorded_clips["mp4"], recording_dir
    )

    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)

    message = win.statusBar().currentMessage()
    assert "Not saved whole" not in message, message
    assert f"Saved {part_path.name}" in message, message
    assert "MP4 cannot hold attachments" in message, message
    assert message.index("Saved") < message.index("cannot hold"), (
        "the note went in front of Saved, which is where warnings go"
    )
    assert message not in win._pinned_messages, "a take saved whole was pinned"


def test_an_mkv_take_still_carries_both_attachments_and_leaves_no_sidecars(
    window, qt_app, recorded_clips, recording_dir
) -> None:
    """The MKV half: still one file, with sidecars off.

    Keeping what an MP4 cannot hold must not keep it beside an MKV that holds
    it, and an MKV take is not told its sidecars stay beside it.
    """
    from wer.video.remux import list_attachments

    win = window
    config = win.show_file.recording
    config.embed_markers = True
    config.keep_sidecar_files = False
    part_path = _armed_take_on_a_real_recording(
        win, recorded_clips["mkv"], recording_dir
    )
    markers_csv = part_path.with_suffix(".markers.csv")
    bus_log = part_path.with_suffix(".bus.jsonl")

    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)

    assert set(list_attachments(part_path)) == {markers_csv.name, bus_log.name}
    left = sorted(path.name for path in recording_dir.iterdir())
    assert left == [part_path.name], f"left beside the MKV: {left}"
    message = win.statusBar().currentMessage()
    assert "Not saved whole" not in message, message
    assert "cannot hold" not in message, message


# ------------------------------------------------- what the fixes broke

# Everything below is a regression test in the strict sense: each one failed
# because a fix above went slightly too far. They are kept next to the tests
# for the original bugs on purpose -- the pair is the whole story, and a future
# reader tempted to undo one needs to see the other.


def test_a_degraded_take_still_shows_the_rec_indicator(
    window, qt_app, tmp_path, monkeypatch
) -> None:
    """A permanent status message must not blank the status bar.

    QStatusBar hides every normal widget for as long as a message is showing.
    Reporting a failed embed with a zero timeout therefore took the REC
    indicator, the Eos state, the bus line and the marker count off the screen
    for the REST OF THE SESSION -- including through every later take, where
    the label's text went on updating behind a hidden widget. The one
    at-a-glance answer to "is it rolling?" was the price of the message.
    """
    from wer.ui import main_window as main_window_module
    from wer.video.remux import RemuxResult

    win = window
    win.show()          # QStatusBar only hides anything once it is visible
    qt_app.processEvents()
    try:
        assert not win._record_status.isHidden()

        win.show_file.recording.embed_markers = True
        monkeypatch.setattr(
            main_window_module,
            "embed_chapters",
            lambda video, chapters, **kwargs: RemuxResult(
                False, video, "No space left on device"
            ),
        )
        _armed_take_with_a_part(win, tmp_path)
        win._stop_recording()
        assert wait_for(qt_app, lambda: not win._finishers)

        message = win.statusBar().currentMessage()
        assert "chapters" in message.lower(), message
        qt_app.processEvents()
        assert not win._record_status.isHidden(), (
            "one degraded take hid the REC indicator for the rest of the session"
        )
        assert not win._eos_status.isHidden()
        assert not win._marker_status.isHidden()
    finally:
        win.hide()


def test_record_works_again_while_the_last_take_is_still_being_written(
    window, qt_app, tmp_path
) -> None:
    """The next take must not wait on the chapter rewrite.

    Guarding _start_recording with "is the finishing thread alive?" made the
    Record button, Ctrl+R and an OSC /wer/record/start all dead for the whole
    of the finish -- and that window is dominated by embed_chapters, which
    rewrites the entire recording. Measured with the bundled ffmpeg on this
    machine's NVMe: 542 MB/s, so ~28 s for a 15 GB four-hour take and minutes
    on the USB disk a booth is as likely to be recording to.

    The race the guard existed for is real and must stay closed: starting a
    take used to clear the very marker log the finishing thread was writing.
    The second assertion here is that half, so removing the guard cannot be
    "fixed" by putting it back.
    """
    win = window
    config = win.show_file.recording
    config.embed_markers = False
    config.keep_sidecar_files = True

    part_path = _armed_take_with_a_part(win, tmp_path)
    win.markers.add_manual(2.0, "the cue from the FIRST take")

    # Hold the finishing thread inside the CSV write, which is where the old
    # race bit: a new take starting here wiped the log mid-write.
    entered = threading.Event()
    release = threading.Event()
    original_write = win.markers.write_csv

    def gated_write(path, **kwargs):
        entered.set()
        release.wait(20.0)
        return original_write(path, **kwargs)

    win.markers.write_csv = gated_write

    win._stop_recording()
    assert entered.wait(10.0), "the finisher never reached the marker CSV"

    try:
        win._start_recording()
        assert wait_for(qt_app, lambda: win.recorder.is_recording, 10.0), (
            "Record did nothing while the last take was still being written"
        )
    finally:
        release.set()

    assert wait_for(qt_app, lambda: not win._finishers)
    csv_text = part_path.with_suffix(".markers.csv").read_text(encoding="utf-8")
    assert "the cue from the FIRST take" in csv_text, (
        "starting the next take wiped the marker log the finisher was writing"
    )
    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)


def test_closing_during_the_finish_does_not_finish_the_take_twice(
    window, qt_app, tmp_path
) -> None:
    """recorder.is_recording is not the question closeEvent has to ask.

    The finishing thread does not clear that flag until it reaches
    recorder.stop(), which is after _stop_pump()'s join. Closing the window in
    that gap -- measured at 214 ms with nothing slowed down -- ran a second,
    concurrent finish on the main thread: two sets of sidecars for one take,
    two STOP markers, two chapter rewrites of the same file, and one run
    deleting the sidecars the other was still reading.
    """
    win = window
    win.show_file.recording.embed_markers = False
    win.show_file.recording.keep_sidecar_files = True
    _armed_take_with_a_part(win, tmp_path)

    finishes: list[str] = []
    original_sidecars = win._write_sidecars

    def counted(*args, **kwargs):
        finishes.append(threading.current_thread().name)
        return original_sidecars(*args, **kwargs)

    win._write_sidecars = counted

    # Stand in for the pump join, so the close lands inside the real window.
    original_stop_pump = win._stop_pump

    def slow_stop_pump():
        time.sleep(1.0)
        original_stop_pump()

    win._stop_pump = slow_stop_pump

    win._stop_recording()
    time.sleep(0.1)
    win.close()

    assert wait_for(qt_app, lambda: not win._finishers)
    assert len(finishes) == 1, f"the take was finished twice: {finishes}"
    assert win.recorder.stopped == 1, (
        f"recorder.stop() was called {win.recorder.stopped} times for one take"
    )


def test_closing_mid_take_does_not_block_forever_on_the_rewrite(
    window, qt_app, tmp_path, monkeypatch
) -> None:
    """The X button must come back even when ffmpeg wedges.

    embed_chapters' own timeout only guards process.wait(), which is reached
    after the stderr readline loop has hit EOF; an ffmpeg that wedges without
    exiting blocks that readline forever. Running the rewrite synchronously on
    the main thread therefore meant the window never closed -- and Windows
    force-terminates an app that stops pumping messages during logoff, which is
    one of the three paths through here.
    """
    from wer.ui import main_window as main_window_module
    from wer.video.remux import RemuxResult

    win = window
    win.show_file.recording.embed_markers = True
    win.show_file.recording.keep_sidecar_files = True
    monkeypatch.setattr(main_window_module, "FINISH_JOIN_TIMEOUT", 1.0)
    # This rewrite ignores being stopped, and what is under test is that the
    # window comes back regardless, not how long a stop is given to work. Left
    # at its 3 s, that wait took this test from 1 s to 4 s of its 8.
    monkeypatch.setattr(main_window_module, "FINISH_CANCEL_TIMEOUT", 0.5)

    wedged = threading.Event()

    def wedged_embed(video, chapters, **kwargs):
        wedged.wait(20.0)
        return RemuxResult(True, video)

    monkeypatch.setattr(main_window_module, "embed_chapters", wedged_embed)
    part_path = _armed_take_with_a_part(win, tmp_path)

    try:
        started = time.monotonic()
        win.close()
        took = time.monotonic() - started
        assert took < 8.0, f"closing took {took:.1f}s with a wedged rewrite"
        # Degraded, not lost: the rewrite never finished, so the sidecar
        # deletion never ran and the cue times are still beside the video.
        assert part_path.with_suffix(".markers.csv").is_file()
    finally:
        wedged.set()


def test_a_volume_that_vanishes_before_the_headroom_check_is_still_reported(
    window, qt_app, tmp_path, monkeypatch
) -> None:
    """The pre-embed stat must not kill the finishing thread.

    The parts list is filtered by p.exists when it is built; the stat happens
    later. A USB or network volume that goes away in between -- exactly where
    takes get written -- raised OSError out of finish(), so recording_finished
    never fired: no "Saved", no failure message, and the window left believing
    a take was still being finished. embed_chapters itself handles a vanished
    file gracefully, so this narrowed an exception path that used to be
    covered.
    """
    from wer.ui import main_window as main_window_module
    from wer.video.recorder import DiskSpace, RecordingPart
    from wer.video.remux import RemuxResult

    class VanishedVolume:
        """A path whose stat() fails the way a pulled USB disk's does."""

        def __init__(self, real: Path) -> None:
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __fspath__(self) -> str:
            return str(self._real)

        def stat(self, *args, **kwargs):
            raise OSError(64, "The specified network name is no longer available")

    win = window
    win.show_file.recording.embed_markers = True
    win.show_file.recording.keep_sidecar_files = True
    monkeypatch.setattr(
        main_window_module,
        "check_disk",
        lambda path: DiskSpace(free_bytes=10 ** 12, total_bytes=10 ** 12),
    )
    monkeypatch.setattr(
        main_window_module,
        "embed_chapters",
        lambda video, chapters, **kwargs: RemuxResult(True, tmp_path / "x.mkv"),
    )

    win._start_recording()
    win.markers.add_manual(1.0, "cue 12")
    real_part = tmp_path / "vanishing.mkv"
    real_part.write_bytes(b"x" * 4096)
    win.recorder.parts = [
        RecordingPart(
            path=VanishedVolume(real_part), session_offset=0.0,
            duration=30.0, frames=900,
        )
    ]

    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers), (
        "the finishing thread died on the disk check and reported nothing"
    )
    assert win.statusBar().currentMessage(), "nothing was ever reported"


def test_a_camera_that_has_only_just_opened_does_not_cry_no_signal(
    qt_app, monkeypatch, fake_camera
) -> None:
    """NO SIGNAL has to mean no signal.

    CaptureStats.is_live is False until the first frame lands, so a camera that
    has only just been opened -- every app launch, and every restart after a
    device or format change -- read "NO SIGNAL (no frames yet)". The stats
    timer is 500 ms, so at least one refresh always showed it. An alarm that
    cries wolf on every launch is an alarm the operator learns to ignore, and
    this one is the tell for a stage camera that has been unplugged.

    Measured on the real Integrated Camera: open() takes 3.6-4.1 s (it probes
    both backends for the delivered rate) and start() to the first frame is
    32-35 ms, i.e. one frame period. The wait for frame one is short; the
    window in which the alarm is a lie is the one that has to close.
    """
    from wer.ui import preview as preview_module
    from wer.ui.preview import PreviewPanel
    from wer.video.capture import LIVE_WINDOW

    monkeypatch.setattr(preview_module, "enumerate_video_devices", lambda: [])
    widget = PreviewPanel()
    try:
        camera = SyntheticCapture(CaptureSettings(width=640, height=360))
        camera.open()
        camera.stats = CaptureStats(started_at=time.perf_counter())
        widget.capture = camera

        widget._refresh_stats()
        text = widget._stats.text()
        assert "NO SIGNAL" not in text, text
        assert "Starting" in text, text

        # ...and the alarm still fires when the camera really has not come up.
        camera.stats.started_at = time.perf_counter() - (LIVE_WINDOW + 5.0)
        widget._refresh_stats()
        assert "NO SIGNAL" in widget._stats.text(), widget._stats.text()
    finally:
        widget.deleteLater()


def test_a_record_press_during_the_close_is_held_not_dropped(
    window, qt_app, tmp_path, dialogs
) -> None:
    """The one window in which a take genuinely cannot start.

    Recorder.stop() is still draining the queue and waiting on ffmpeg's
    trailer, and Recorder.start() resets the very state it is working through,
    so a start here would be worse than a wait. The press is therefore held and
    retried rather than refused: a console macro that fires /wer/record/stop
    and /wer/record/start back to back would otherwise lose the next scene's
    take entirely, and nobody would find out until the edit.
    """
    win = window
    win.show_file.recording.embed_markers = False
    win.show_file.recording.keep_sidecar_files = True
    _armed_take_with_a_part(win, tmp_path)

    release = threading.Event()
    original_stop = win.recorder.stop

    def held_stop():
        release.wait(20.0)
        return original_stop()

    win.recorder.stop = held_stop

    win._stop_recording()
    assert wait_for(qt_app, lambda: win._closing_take.is_set(), 10.0)

    try:
        win._start_recording()
        # is_recording is still True here -- the stub does not go idle until
        # the held stop() returns -- so ask whether a NEW take was armed.
        assert not win._take_in_progress, (
            "a take was armed while the recorder was still being torn down"
        )
        assert win._start_pending, "the Record press was dropped on the floor"
        assert "closing" in win.statusBar().currentMessage().lower()
        # An impatient second press must not queue a second retry: both would
        # fire when the gate opens, and the loser would find the recorder
        # already running and put "Could not start recording" over a take that
        # had started perfectly well.
        win._start_recording()
    finally:
        release.set()

    assert wait_for(qt_app, lambda: win._take_in_progress, 15.0), (
        "the held Record press never turned into a take"
    )
    assert win.recorder.is_recording
    qt_app.processEvents()
    assert dialogs == [], f"the held press raised {dialogs}"
    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)


def test_the_controls_people_use_can_reach_the_held_start(
    window, qt_app, tmp_path, dialogs
) -> None:
    """The holding machinery above was unreachable from anything real.

    _start_recording holds a press that lands while the last take is closing,
    and the test above proves it works -- by calling _start_recording directly.
    Nothing else did. _toggle_recording and the OSC handler both asked
    ``recorder.is_recording`` first, and during the recorder's teardown that is
    still True, so the Record button, Ctrl+R and /wer/record/start were all
    routed to "stop the take that is already stopping" and the press was
    dropped. Sixty lines of correct, tested code that no operator and no desk
    macro could get to.

    This drives the two paths a person or a console actually uses.
    """
    win = window
    win.show_file.recording.embed_markers = False
    win.show_file.recording.keep_sidecar_files = True
    _armed_take_with_a_part(win, tmp_path)

    release = threading.Event()
    original_stop = win.recorder.stop
    win.recorder.stop = lambda: (release.wait(20.0), original_stop())[1]

    win._stop_recording()
    assert wait_for(qt_app, lambda: win._closing_take.is_set(), 10.0)

    try:
        assert win.recorder.is_recording, (
            "precondition: the recorder still reports recording during teardown, "
            "which is what made is_recording alone the wrong question"
        )
        win._toggle_recording()          # the Record button and Ctrl+R
        assert win._start_pending, "the toggle path dropped the press"

        win._start_pending = False       # now the console macro's half
        win._handle_osc_command("/wer/record/start", None)
        assert win._start_pending, "the OSC path dropped the press"
    finally:
        release.set()

    assert wait_for(qt_app, lambda: win._take_in_progress, 15.0)
    assert dialogs == []
    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)


# ------------------------------------------------- finishing a long take


class FakeDisk:
    """check_disk with a free-space figure the test moves as it goes."""

    def __init__(self, free_bytes: int) -> None:
        self.free_bytes = free_bytes

    def __call__(self, path):
        from wer.video.recorder import DiskSpace

        return DiskSpace(free_bytes=self.free_bytes, total_bytes=10 ** 13)


#: What a stand-in rewrite runs in place of ffmpeg: a process that does
#: nothing until it is killed.
IDLE_PROCESS = [sys.executable, "-c", "import time; time.sleep(60)"]


class RewriteUntilStopped:
    """Stands in for embed_chapters: a rewrite that runs until it is stopped.

    What the window is tested on here is whether it stops a rewrite, and when.
    Deleting the copy is embed_chapters' half, covered in test_remux_control.
    Like the real thing, this names the file and its temporary copy on the
    control, can write that copy so the window has something to measure, and
    runs a real process through the control that only a stop can end. It used
    to run none, which left control.running False for good: shutdown's branch
    for a rewrite still running -- the one every real ffmpeg takes -- was never
    reached, and a mutation that made that branch raise passed every test.
    """

    def __init__(self, copy_bytes: int = 0, limit: float = 20.0) -> None:
        self.started = threading.Event()
        self.stopped_because = ""
        self.copy_bytes = copy_bytes
        self.limit = limit
        self.process: subprocess.Popen | None = None

    def __call__(self, video, chapters, *, control=None, **kwargs):
        from wer.video.remux import RemuxResult

        temporary = video.with_name(f"{video.stem}.chapters-tmp{video.suffix}")
        if control is not None:
            control.video = video
            control.temporary = temporary
        if self.copy_bytes:
            temporary.write_bytes(b"\0" * self.copy_bytes)
        process = subprocess.Popen(
            IDLE_PROCESS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.process = process
        if control is not None and not control._attach(process):
            process.kill()          # a stop got there first, as embed_chapters does
        self.started.set()
        try:
            deadline = time.monotonic() + self.limit
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    return RemuxResult(True, video)
                time.sleep(0.02)
            if control is not None and control.cancelled:
                self.stopped_because = control.reason
                return RemuxResult(False, video, control.reason, cancelled=True)
            return RemuxResult(False, video, "the stand-in ffmpeg exited by itself")
        finally:
            if control is not None:
                control._detach()
            if process.poll() is None:
                process.kill()
            process.wait(10.0)
            temporary.unlink(missing_ok=True)


def test_a_take_armed_before_the_last_takes_rewrite_is_not_stopped_by_it(
    window, qt_app, tmp_path, monkeypatch
) -> None:
    """A live take outranks chapters.

    The finisher checked only that the drive could hold a second copy of the
    file, never that a take was recording on that drive. A console macro that
    fires /wer/record/stop and then /wer/record/start arms the next take before
    the rewrite has written a byte, and that take's start check passes on the
    space as it stands. The rewrite's copy then took the drive under the new
    take's stop floor, the take's own 10 s disk check ended it with a modal
    nobody was in the room to see, and seconds later the swap gave the space
    back. With the shipped 10 GB floor: 18 GB free, a 12 GB take, and the next
    take lost.

    Here the drive has 11 GB free and the new take stops at 10: rewriting even
    a small file would leave it inside REWRITE_HEADROOM_BYTES of that floor.
    """
    from wer.ui import main_window as main_window_module
    from wer.video.remux import RemuxResult

    win = window
    config = win.show_file.recording
    config.embed_markers = True
    config.keep_sidecar_files = True
    part_path = _armed_take_with_a_part(win, tmp_path)

    attempts: list[Path] = []

    def record_attempt(video, chapters, **kwargs):
        attempts.append(video)
        return RemuxResult(True, video)

    monkeypatch.setattr(main_window_module, "embed_chapters", record_attempt)
    monkeypatch.setattr(main_window_module, "check_disk", FakeDisk(11_000_000_000))

    # Hold the finisher before its embed check, which is where a back-to-back
    # start lands: the recorder is free, and the rewrite has not begun.
    entered = threading.Event()
    release = threading.Event()
    original_write = win.markers.write_csv

    def gated_write(path, **kwargs):
        entered.set()
        release.wait(20.0)
        return original_write(path, **kwargs)

    win.markers.write_csv = gated_write
    win._stop_recording()
    assert entered.wait(10.0), "the finisher never reached the marker CSV"

    try:
        config.low_disk_stop_gb = 10.0
        win._start_recording()
        assert win._take_in_progress, "precondition: the next take did not arm"
    finally:
        release.set()

    assert wait_for(qt_app, lambda: not win._finishers)
    assert attempts == [], "the rewrite went ahead into the live take's floor"
    assert win._take_in_progress and win.recorder.is_recording
    assert part_path.with_suffix(".markers.csv").is_file()
    message = win.statusBar().currentMessage()
    assert "chapters" in message.lower(), message
    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)


def test_a_rewrite_that_would_run_a_live_take_out_of_room_is_stopped_not_the_take(
    window, qt_app, tmp_path, monkeypatch
) -> None:
    """The rewrite was already under way when the next take was armed.

    That take's start check saw the space before the rewrite's copy had grown
    into it. A 16 GB take stopped with 24 GB free left 8 GB once its copy was
    written -- under the next take's 10 GB floor -- and that take was ended for
    lack of space on a drive with hours of room once the swap had finished.
    The rewrite has to give way.
    """
    from wer.ui import main_window as main_window_module

    win = window
    config = win.show_file.recording
    config.embed_markers = True
    config.keep_sidecar_files = True
    disk = FakeDisk(100_000_000_000)
    rewrite = RewriteUntilStopped()
    monkeypatch.setattr(main_window_module, "check_disk", disk)
    monkeypatch.setattr(main_window_module, "embed_chapters", rewrite)
    part_path = _armed_take_with_a_part(win, tmp_path)

    win._stop_recording()
    assert rewrite.started.wait(10.0), "the rewrite never began"

    config.low_disk_stop_gb = 10.0
    win._start_recording()
    assert win._take_in_progress, "precondition: the next take did not arm"
    disk.free_bytes = 11_000_000_000        # the copy has grown into the room

    assert wait_for(qt_app, lambda: not win._finishers, 10.0), (
        "the rewrite carried on towards the live take's floor"
    )
    assert rewrite.stopped_because, "the rewrite finished instead of giving way"
    assert win._take_in_progress and win.recorder.is_recording
    assert part_path.with_suffix(".markers.csv").is_file()
    assert "chapters" in win.statusBar().currentMessage().lower()
    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)


def test_a_take_armed_while_a_rewrite_holds_the_room_it_needs_still_starts(
    window, qt_app, tmp_path, monkeypatch, dialogs
) -> None:
    """Space a rewrite is holding is space the next take gets back.

    Free space dips by the size of the file while its chapters are embedded,
    and returns the moment the copy is swapped in or deleted. A Record press
    inside that dip was refused as "Not enough disk space" -- and a start from
    the desk refused that way is a take that never happens, reported on a
    status line nobody is there to read.
    """
    from wer.ui import main_window as main_window_module

    win = window
    config = win.show_file.recording
    config.embed_markers = True
    config.keep_sidecar_files = True
    disk = FakeDisk(100_000_000_000)
    rewrite = RewriteUntilStopped(copy_bytes=64_000)
    monkeypatch.setattr(main_window_module, "check_disk", disk)
    monkeypatch.setattr(main_window_module, "embed_chapters", rewrite)
    _armed_take_with_a_part(win, tmp_path)

    win._stop_recording()
    assert rewrite.started.wait(10.0), "the rewrite never began"

    # Scaled to what a test can write: the drive is 16 KB short of a 50 KB
    # floor, and the rewrite's copy is holding 64 KB of it.
    config.low_disk_stop_gb = 0.000_050
    disk.free_bytes = 34_000
    win._start_recording()

    assert dialogs == [], f"the take was refused: {dialogs}"
    assert win._take_in_progress and win.recorder.is_recording
    # ...and the rewrite then gives the room back rather than keeping it.
    assert wait_for(qt_app, lambda: not win._finishers, 10.0)
    assert rewrite.stopped_because
    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)


def test_a_take_on_another_drive_leaves_the_rewrite_alone(
    window, qt_app, tmp_path, monkeypatch
) -> None:
    """A rewrite gives way only to a take on the same drive.

    Same drive is judged by volume serial, and nothing tested the answer no. A
    slip that answered yes for every pair of paths would skip or stop the
    chapters of every take finished while the next was already recording -- a
    desk macro's stop-then-start does that at every take -- on a machine
    recording to a second disk with room to spare, and each take would be
    reported with its cue times beside it rather than in it, for nothing.

    The drive being rewritten is as tight as in the first test above, where the
    rewrite is rightly skipped; the live take is on another one.
    """
    from wer.ui import main_window as main_window_module

    win = window
    config = win.show_file.recording
    config.embed_markers = True
    config.keep_sidecar_files = True
    recordings = Path(config.directory)
    monkeypatch.setattr(
        main_window_module, "_volume_serial",
        lambda path: 2 if Path(path).is_relative_to(recordings) else 1,
    )
    monkeypatch.setattr(main_window_module, "check_disk", FakeDisk(11_000_000_000))
    # The guard looks often enough, for long enough, that one answering yes
    # would certainly have stopped the rewrite.
    monkeypatch.setattr(main_window_module, "REWRITE_GUARD_INTERVAL", 0.05)
    rewrite = RewriteUntilStopped(limit=1.0)
    monkeypatch.setattr(main_window_module, "embed_chapters", rewrite)
    part_path = _armed_take_with_a_part(win, tmp_path)

    # Arm the next take before the finisher reaches its embed check, as a
    # back-to-back start from the desk does.
    entered = threading.Event()
    release = threading.Event()
    original_write = win.markers.write_csv

    def gated_write(path, **kwargs):
        entered.set()
        release.wait(20.0)
        return original_write(path, **kwargs)

    win.markers.write_csv = gated_write
    win._stop_recording()
    assert entered.wait(10.0), "the finisher never reached the marker CSV"
    try:
        config.low_disk_stop_gb = 10.0
        win._start_recording()
        assert win._take_in_progress, "precondition: the next take did not arm"
    finally:
        release.set()

    assert wait_for(qt_app, lambda: not win._finishers)
    assert rewrite.started.is_set(), (
        "the chapters were skipped for a take on another drive"
    )
    assert not rewrite.stopped_because, (
        f"the rewrite was stopped: {rewrite.stopped_because}"
    )
    assert win._take_in_progress and win.recorder.is_recording
    assert not any("not embedded" in message for message in win._pinned_messages), (
        win._pinned_messages
    )
    assert part_path.with_suffix(".markers.csv").is_file()
    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)


def test_closing_during_a_long_rewrite_stops_it_and_says_so(
    window, qt_app, tmp_path, monkeypatch, caplog
) -> None:
    """Shutdown used to give up on a rewrite without a word.

    Once its wait ran out it carried on and exited: the finishing thread died
    with the interpreter, the ffmpeg it had launched went on unsupervised, the
    full-size .chapters-tmp copy stayed beside the video with nothing ever
    going to delete it, and the log's last word on the take was "Embedding
    chapters into take.mkv" -- no error, nothing to say it had been abandoned.
    A four-hour take on a USB disk rewrites for minutes, so Stop and then close
    is an ordinary end to a night.
    """
    import logging

    from wer.ui import main_window as main_window_module

    win = window
    win.show_file.recording.embed_markers = True
    win.show_file.recording.keep_sidecar_files = True
    monkeypatch.setattr(main_window_module, "FINISH_JOIN_TIMEOUT", 1.0)
    rewrite = RewriteUntilStopped()
    monkeypatch.setattr(main_window_module, "embed_chapters", rewrite)
    part_path = _armed_take_with_a_part(win, tmp_path)

    win._stop_recording()
    assert rewrite.started.wait(10.0), "the rewrite never began"

    with caplog.at_level(logging.ERROR, logger="wer.ui.main_window"):
        win.close()
        abandoned = [
            record.getMessage() for record in caplog.records
            if record.name == "wer.ui.main_window"
            and "shutdown" in record.getMessage().lower()
        ]

    assert rewrite.stopped_because, "shutdown left the rewrite running"
    assert rewrite.process is not None and rewrite.process.poll() is not None, (
        "the rewrite's process outlived the window"
    )
    assert not any(finisher.is_alive() for finisher in win._finishers), (
        "the finisher was not given the moment it needs to clean up"
    )
    assert abandoned, "the log never said the rewrite was abandoned"
    # Named as a rewrite still running, which is what a real ffmpeg is at this
    # point -- not the fallback line for a take stuck at some other stage.
    expected = f"stopping the chapter rewrite of {part_path.name}"
    assert expected in abandoned[0], abandoned
    assert part_path.with_suffix(".markers.csv").is_file()


def test_a_close_cut_short_inside_its_wait_has_already_named_what_it_leaves(
    window, qt_app, tmp_path, monkeypatch, caplog
) -> None:
    """Everything shutdown said about an unfinished take, it said after waiting.

    A Windows logoff comes through closeEvent, and Windows force-terminates an
    app that stops pumping messages during logoff -- which a 45 s wait on a
    rewrite does. Ended inside that wait, the stop, the delete and the line
    naming what was abandoned never ran, and the log's last word on the take
    stayed "Embedding chapters into take.mkv": no error, and a full-size
    .chapters-tmp copy beside the video with nothing to say what it was.

    A test cannot end the process, so this reads the log at the moment the
    wait begins, the last moment anything is sure to be written.
    """
    import logging

    from wer.ui import main_window as main_window_module

    win = window
    win.show_file.recording.embed_markers = True
    win.show_file.recording.keep_sidecar_files = True
    monkeypatch.setattr(main_window_module, "FINISH_JOIN_TIMEOUT", 1.0)
    rewrite = RewriteUntilStopped()
    monkeypatch.setattr(main_window_module, "embed_chapters", rewrite)
    part_path = _armed_take_with_a_part(win, tmp_path)

    win._stop_recording()
    assert rewrite.started.wait(10.0), "the rewrite never began"

    finisher = next(f for f in win._finishers if f.is_alive())
    join = finisher.join
    said_before_waiting: list[list[str]] = []

    def join_after_reading_the_log(timeout=None):
        if not said_before_waiting:
            said_before_waiting.append([
                record.getMessage() for record in caplog.records
                if record.name == "wer.ui.main_window"
                and record.levelno >= logging.WARNING
            ])
        join(timeout)

    finisher.join = join_after_reading_the_log
    with caplog.at_level(logging.WARNING, logger="wer.ui.main_window"):
        win.close()

    assert said_before_waiting, "precondition: shutdown never waited for the take"
    before = said_before_waiting[0]
    copy = part_path.with_name(f"{part_path.stem}.chapters-tmp{part_path.suffix}")
    assert any(part_path.name in line and copy.name in line for line in before), (
        f"nothing named the take and its copy before the wait began: {before}"
    )


def test_a_take_that_carried_on_into_a_second_file_says_so_and_names_both(
    window, qt_app, tmp_path
) -> None:
    """"Saved take-part2.mkv (1200 MB)" used to be the whole report.

    When ffmpeg dies mid-take the recorder carries on into a new file and stays
    RECORDING, so no failure reaches the window. At Stop, Recorder.stop()
    returns the LAST part, and the status bar said "Saved" -- unpinned, gone in
    ten seconds -- over the name and size of the tail alone, while the first
    forty minutes sat in a file it never mentioned. The Help promises the
    status message names every file produced.
    """
    win = window
    config = win.show_file.recording
    config.embed_markers = False
    config.keep_sidecar_files = True

    win._start_recording()
    first = tmp_path / "splittake.mkv"
    second = tmp_path / "splittake-part2.mkv"
    first.write_bytes(b"x" * 3_000_000)
    second.write_bytes(b"x" * 1_000_000)
    win.recorder.parts = [
        RecordingPart(path=first, session_offset=0.0, duration=2530.0, frames=75_900),
        RecordingPart(
            path=second, session_offset=2530.0, duration=1070.0, frames=32_100
        ),
    ]

    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)

    message = win.statusBar().currentMessage()
    assert first.name in message and second.name in message, message
    assert "42:10" in message, message
    assert "4 MB" in message, message
    assert message.index("42:10") < message.index("Saved"), (
        "the warning has to survive the status bar cutting the message short"
    )
    assert message in win._pinned_messages, (
        "a split take was announced and then forgotten"
    )


def test_sound_arriving_with_gaps_is_pinned_while_the_take_runs(
    window, qt_app
) -> None:
    """The recorder says this on the Recording tab's detail line, and that is
    not the tab anyone is watching mid-take. Pinned, because the gaps go on for
    as long as the take does."""
    win = window
    win._start_recording()
    warning = (
        'Only 51% of the sound from "Microphone (USB Color Camera)" is reaching '
        "the recording, so this take's sound has gaps in it."
    )

    win.sound_warning.emit(warning)
    qt_app.processEvents()

    assert win.statusBar().currentMessage() == warning
    assert warning in win._pinned_messages


def test_the_recorders_sound_warning_reaches_the_status_bar(
    qt_app, fake_camera, dialogs
) -> None:
    """The recorder's callback, the signal and the pin, joined up.

    Each half was covered on its own: the recorder tests pass their own
    callback, and the pin test emits the signal itself. Taking
    on_sound_warning off the Recorder the window builds left every one of them
    green, and a take on a half-delivering input said nothing at all.
    """
    import threading

    from wer.ui.main_window import MainWindow

    win = MainWindow()
    try:
        warning = 'Only 51% of the sound from "Line In" is reaching take012.mkv.'
        # From a worker, as the thread reading ffmpeg's statistics does.
        threading.Thread(
            target=lambda: win.recorder._on_sound_warning(warning), daemon=True
        ).start()

        assert wait_for(qt_app, lambda: warning in win._pinned_messages), (
            "the recorder's warning never reached the status bar"
        )
    finally:
        win.close()
        win.deleteLater()


def test_a_take_whose_sound_had_gaps_says_so_when_it_is_saved(
    window, qt_app, tmp_path
) -> None:
    """"Saved take.mkv (4 MB)" over a take missing half its sound is the silent
    failure this check exists for."""
    win = window
    win.show_file.recording.embed_markers = False
    win.show_file.recording.keep_sidecar_files = True
    _armed_take_with_a_part(win, tmp_path)
    win.recorder.stats.sound_delivered_share = 0.5

    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)

    message = win.statusBar().currentMessage()
    assert "50% of the sound" in message, message
    assert message.index("50%") < message.index("Saved"), (
        "the warning has to survive the status bar cutting the message short"
    )
    assert message in win._pinned_messages, "a take with gappy sound was forgotten"


def test_a_pinned_failure_comes_back_after_a_passing_message(
    window, qt_app, tmp_path
) -> None:
    """A pin was only as permanent as the next status message.

    QStatusBar holds one temporary message. Any later showMessage replaced a
    pinned failure, and nothing put the failure back when that message's own
    timeout ran out -- so "Recording FAILED" lasted until the desk's next GO
    ("OSC marker ignored -- not recording", five seconds), and the bar was then
    empty for the rest of the night.
    """
    win = window
    win.show_file.recording.embed_markers = False
    win.show_file.recording.keep_sidecar_files = True
    _armed_take_with_a_part(win, tmp_path)
    win.recorder.state = RecorderState.ERROR
    win._on_recorder_state(RecorderState.ERROR, "ffmpeg failed 5 times; giving up.")
    assert wait_for(qt_app, lambda: not win._finishers)
    pinned = win.statusBar().currentMessage()
    assert "FAILED" in pinned, pinned

    win._handle_osc_command("/wer/marker", None)
    assert "ignored" in win.statusBar().currentMessage(), (
        "precondition: the OSC message did not replace the pin"
    )
    # Another passing message with its timeout shortened: the five seconds are
    # Qt's to count, not this test's.
    win.statusBar().showMessage("Layout: Tech", 100)

    assert wait_for(
        qt_app, lambda: win.statusBar().currentMessage() == pinned, 3.0
    ), f"the failure was wiped: the bar reads {win.statusBar().currentMessage()!r}"


def test_a_start_from_the_desk_does_not_take_down_the_last_takes_failure(
    window, qt_app, tmp_path
) -> None:
    """Arming a take cleared the pinned failure, wherever the start came from.

    The reasoning was that arming a take means somebody is in the booth and
    has seen it. A desk macro starting Act 2 is no such evidence: it cleared
    Act 1's "Recording FAILED" with nobody in the room, and the one notice left
    for the morning went with it. Record pressed at the machine still does.
    """
    win = window
    win.show_file.recording.embed_markers = False
    win.show_file.recording.keep_sidecar_files = True
    _armed_take_with_a_part(win, tmp_path)
    win.recorder.state = RecorderState.ERROR
    win._on_recorder_state(RecorderState.ERROR, "ffmpeg failed 5 times; giving up.")
    assert wait_for(qt_app, lambda: not win._finishers)
    assert "FAILED" in win.statusBar().currentMessage()

    win._handle_osc_command("/wer/record/start", None)
    assert win.recorder.is_recording, "precondition: the desk's take did not start"
    assert "FAILED" in win.statusBar().currentMessage(), (
        "a start from the desk took down the last take's failure"
    )

    win._handle_osc_command("/wer/record/stop", None)
    assert wait_for(qt_app, lambda: not win._finishers)
    win._start_recording()              # Record, pressed at the machine
    assert win._take_in_progress
    assert win._pinned_messages == []
    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)


def _add_encoder_drops(capture, count: int) -> None:
    """Count frames as turned away by a camera's encoder queue, without racing it.

    The camera's own thread adds to the same counter whenever its encoder queue
    is full, and a += from here landing on top of one there can lose either. So
    the feed to the encoder is paused until the camera has counted one more
    frame: no frame counted after the pause can reach the queue, and any offer
    already under way when it began has finished by then. The feed is put back
    as it was.
    """
    feeding = capture.feed_encoder.is_set()
    capture.feed_encoder.clear()
    try:
        seen = capture.stats.frames_captured
        deadline = time.monotonic() + 5.0
        while capture.stats.frames_captured <= seen and time.monotonic() < deadline:
            time.sleep(0.005)
        assert capture.stats.frames_captured > seen, (
            "the camera is not producing frames"
        )
        capture.stats.frames_dropped_encoder += count
    finally:
        if feeding:
            capture.feed_encoder.set()


def test_frames_lost_during_a_take_are_counted_in_its_report(
    window, qt_app, tmp_path
) -> None:
    """A take that dropped frames was announced as plain "Saved".

    wer.log records drops at 1, 10, 100 and 1000 and then goes quiet, the stop
    summary carries no drop figure, and the Recording tab's red count is gone
    the moment the next take starts -- so the morning after a four-hour tech
    nobody could tell 1,001 drops from 300,000. Both queues count: the
    camera's encoder queue, whose counter lives as long as the camera, and the
    recorder's, which starts again with every take.
    """
    import re

    win = window
    config = win.show_file.recording
    config.embed_markers = False
    config.keep_sidecar_files = True
    capture = win.preview.capture
    # Turned away before this take started: not this take's to report.
    _add_encoder_drops(capture, 5000)

    _armed_take_with_a_part(win, tmp_path)
    _add_encoder_drops(capture, 25)
    win.recorder.stats.frames_dropped = 40
    win.recorder.stats.frames_written = 900

    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)

    message = win.statusBar().currentMessage()
    found = re.search(r"([\d,]+) frames? dropped", message)
    assert found, message
    dropped = int(found.group(1).replace(",", ""))
    assert 65 <= dropped < 5000, f"{dropped} reported: {message}"
    assert message.index("dropped") < message.index("Saved"), message
    assert message in win._pinned_messages, "the loss was announced and then forgotten"


def test_a_camera_restarted_mid_take_has_its_own_drops_counted_whole(
    window, qt_app, tmp_path
) -> None:
    """A camera started inside a take began counting inside it.

    The take's share of a camera's drops is what its counter has gained since
    the take was armed, and that holds only for the camera running then. A
    camera restarted mid-take brings a new counter from zero; measured against
    the old camera's figure at arming, its drops would come to
    max(0, 7 - 5000), and a take that lost frames after the restart would be
    reported as losing none of them.
    """
    import re

    win = window
    config = win.show_file.recording
    config.embed_markers = False
    config.keep_sidecar_files = True
    first = win.preview.capture
    _add_encoder_drops(first, 5000)       # before the take: not its to report

    _armed_take_with_a_part(win, tmp_path)
    win.preview.stop_capture()
    win.preview.start_capture()
    second = win.preview.capture
    assert second is not None and second is not first, "precondition: no restart"
    _add_encoder_drops(second, 7)
    win.recorder.stats.frames_dropped = 40
    win.recorder.stats.frames_written = 900

    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)

    message = win.statusBar().currentMessage()
    found = re.search(r"([\d,]+) frames? dropped", message)
    assert found, message
    dropped = int(found.group(1).replace(",", ""))
    assert 47 <= dropped < 5000, f"{dropped} reported: {message}"


def _without_the_cameras_drops(win) -> None:
    """Keep the camera's encoder queue out of a take's drop count.

    The feed is paused the way _add_encoder_drops pauses it, and the take's
    share counted from there, so a busy machine's pump cannot add a camera drop
    to figures a test sets exactly.
    """
    capture = win.preview.capture
    capture.feed_encoder.clear()
    seen = capture.stats.frames_captured
    deadline = time.monotonic() + 5.0
    while capture.stats.frames_captured <= seen and time.monotonic() < deadline:
        time.sleep(0.005)
    assert capture.stats.frames_captured > seen, "the camera is not producing frames"
    win._take_capture_drops = capture.stats.frames_dropped_encoder


def test_frames_lost_to_ffmpeg_failing_are_not_put_down_to_the_encoder(
    window, qt_app, tmp_path, caplog
) -> None:
    """The overnight soak's h264_mf take. ffmpeg failed seven times in a row,
    the take was given up, and the frames turned away meanwhile were reported
    as "2,638 frames dropped (99.5%): the encoder could not keep up", on the
    status bar and in the log. The count stays; the cause is what the recorder
    saw."""
    import logging

    win = window
    config = win.show_file.recording
    config.embed_markers = False
    config.keep_sidecar_files = True
    part_path = _armed_take_with_a_part(win, tmp_path)
    _without_the_cameras_drops(win)
    win.recorder.parts = [
        RecordingPart(
            path=part_path.with_name(f"embedtake-part{n + 1}.mkv") if n else part_path,
            session_offset=2.0 * n,
        )
        for n in range(7)
    ]
    stats = win.recorder.stats
    stats.frames_dropped = 2638
    stats.frames_dropped_ffmpeg_failing = 2638
    stats.most_ffmpeg_failures_in_a_row = 7
    stats.frames_written = 14

    win.recorder.state = RecorderState.ERROR
    with caplog.at_level(logging.WARNING, logger="wer.ui.main_window"):
        win._on_recorder_state(
            RecorderState.ERROR,
            "ffmpeg failed 7 times in a row; giving up rather than restarting again.",
        )
        assert wait_for(qt_app, lambda: not win._finishers)

    message = win.statusBar().currentMessage()
    assert "2,638 frames dropped" in message, message
    assert "not recorded because ffmpeg kept failing" in message, message
    assert "keep up" not in message, message
    lost = [
        record.getMessage() for record in caplog.records
        if " lost " in record.getMessage()
    ]
    assert lost, "the take's lost frames were not logged"
    assert not any("keep up" in line for line in lost), lost


def test_a_take_that_lost_frames_both_ways_gives_each_cause_its_count(
    window, qt_app, tmp_path
) -> None:
    """ffmpeg stopped once and the take carried on in a new file, and the
    encoder fell behind as well. Each share of the figure is given with its own
    cause, so neither is hidden behind the other."""
    win = window
    config = win.show_file.recording
    config.embed_markers = False
    config.keep_sidecar_files = True
    part_path = _armed_take_with_a_part(win, tmp_path)
    _without_the_cameras_drops(win)
    win.recorder.parts.append(
        RecordingPart(
            path=part_path.with_name("embedtake-part2.mkv"), session_offset=600.0
        )
    )
    stats = win.recorder.stats
    stats.frames_dropped = 400
    stats.frames_dropped_ffmpeg_failing = 90
    stats.most_ffmpeg_failures_in_a_row = 1
    stats.frames_written = 9000

    win._stop_recording()
    assert wait_for(qt_app, lambda: not win._finishers)

    message = win.statusBar().currentMessage()
    assert "400 frames dropped" in message, message
    assert "90 not recorded while ffmpeg was failing or restarting" in message, message
    assert "310 because the encoder could not keep up" in message, message


def test_two_ffmpeg_deaths_hours_apart_are_not_called_ffmpeg_that_kept_failing(
    window, qt_app, tmp_path, caplog
) -> None:
    """ffmpeg died an hour in and again two hours in, and the take recorded
    properly after each. Chosen from how many files the take was in, the frames
    lost around those deaths were put down to ffmpeg that "kept failing": what
    the soak take given up on after seven failures in a row was told."""
    import logging

    win = window
    config = win.show_file.recording
    config.embed_markers = False
    config.keep_sidecar_files = True
    part_path = _armed_take_with_a_part(win, tmp_path)
    _without_the_cameras_drops(win)
    win.recorder.parts.extend(
        RecordingPart(
            path=part_path.with_name(f"embedtake-part{n}.mkv"),
            session_offset=3600.0 * (n - 1),
        )
        for n in (2, 3)
    )
    stats = win.recorder.stats
    stats.frames_dropped = 120
    stats.frames_dropped_ffmpeg_failing = 120
    stats.most_ffmpeg_failures_in_a_row = 1
    stats.frames_written = 324_000

    with caplog.at_level(logging.WARNING, logger="wer.ui.main_window"):
        win._stop_recording()
        assert wait_for(qt_app, lambda: not win._finishers)

    message = win.statusBar().currentMessage()
    assert "120 frames dropped" in message, message
    assert "not recorded while ffmpeg was failing or restarting" in message, message
    assert "kept failing" not in message, message
    lost = [
        record.getMessage() for record in caplog.records
        if " lost " in record.getMessage()
    ]
    assert lost, "the take's lost frames were not logged"
    assert not any("kept failing" in line for line in lost), lost
