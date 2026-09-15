"""Opening Wer: the queries it starts stay out of the interface's way and the camera's.

Measured on the rig, in seconds from process creation: encoder detection held
the interface thread from 0.50 to 1.87 s and the audio list from 1.87 to
2.17 s, so the console finder's result, queued at 0.557, was not handled until
the camera had opened at 5.42. The audio probe ran during that open.
wer.ui.startup has the whole timeline and the rules these tests hold it to.

Nothing here touches real hardware. The camera, its format probe, encoder
detection and the audio inputs are all stand-ins, and each one answers only
when the test lets it. Where the real FormatProbe is used, it is over a
probe_formats stand-in that waits for its cue.
"""

from __future__ import annotations

import gc
import logging
import re
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QTimer, Signal

from wer.core.showfile import RecordingConfig
from wer.ui.preview import FormatProbe as RealFormatProbe
from wer.video.capture import CameraCapture
from wer.video.devices import AudioDevice, AudioFormat, CaptureDevice, VideoFormat
from wer.video.encoder import (
    ALL_ENCODERS,
    AUTO_ENCODER,
    HARDWARE_ENCODERS,
    SOFTWARE_ENCODER,
    EncoderAvailability,
    forget_detected_encoders,
)

WEBCAM = "Integrated Webcam"
BLACKMAGIC = "Blackmagic WDM Capture"
BLACKMAGIC_AUDIO = "Line In (Blackmagic UltraStudio Recorder 3G Audio)"
FORMATS = [VideoFormat(1920, 1080, 30.0, 30.0, "UYVY")]
NVENC = next(encoder for encoder in HARDWARE_ENCODERS if encoder.name == "h264_nvenc")
WORKERS = ("wer-encoder-detect", "wer-audio-probe", "format-probe")

FORMATS_ANSWERED = "camera formats answered"
DETECTION_STARTED = "encoder detection started"
DETECTION_ANSWERED = "encoder detection answered"
AUDIO_LISTED = "audio inputs listed"

#: How soon the interface must answer while start-up queries run: under the
#: second both are held for in the test that uses it. See that test.
RESPONSIVE_WITHIN = 0.6


def pump(qt_app, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)


def wait_for(qt_app, predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        qt_app.processEvents()
        time.sleep(0.01)
    return predicate()


class HeldProbe(QObject):
    """The camera format probe, answering only when the test calls answer()."""

    finished = Signal(object)
    last: HeldProbe | None = None
    events: list = []

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.devices: list[CaptureDevice] = []
        HeldProbe.last = self

    def probe(self, devices) -> None:
        self.devices = list(devices)

    def answer(self) -> None:
        HeldProbe.events.append(FORMATS_ANSWERED)
        self.finished.emit({device.index: list(FORMATS) for device in self.devices})


class CountingCamera(CameraCapture):
    """A CameraCapture that opens nothing, and counts how often it is opened."""

    opened: list[int] = []
    #: Called as the camera is opened, so a test can see what else had started.
    on_open = None

    def open(self) -> bool:
        CountingCamera.opened.append(self.settings.device_index)
        if CountingCamera.on_open is not None:
            CountingCamera.on_open()
        self._backend_name = "STAND-IN"
        self._actual = (self.settings.width, self.settings.height, self.settings.fps)
        return True

    def start(self) -> None:
        self.running = True

    def stop(self, timeout: float = 3.0) -> bool:
        self.running = False
        return True

    @property
    def is_running(self) -> bool:
        return getattr(self, "running", False)


class IdleRecorder:
    """Just enough recorder for RecordPanel._refresh, with no take running."""

    is_recording = False
    detail = ""
    queue_depth = 0
    queue_capacity = 90
    stats = SimpleNamespace(frames_written=0)

    def __init__(self) -> None:
        from wer.video.recorder import RecorderState

        self.state = RecorderState.IDLE


@pytest.fixture
def startup(qt_app, monkeypatch):
    """A main window's start-up on stand-ins that answer when told to.

    Detection and the audio list wait on events the test sets; everything left
    waiting is let go, and its thread joined, before the stand-ins are removed.
    """
    from PySide6.QtWidgets import QMessageBox

    from wer.ui import preview as preview_module
    from wer.ui import record_panel as record_module

    rig = SimpleNamespace(
        events=[],
        encoders_go=threading.Event(),
        audio_go=threading.Event(),
        gates=[],
        detected=[EncoderAvailability(SOFTWARE_ENCODER, True)],
        windows=[],
    )

    def detect(**_kwargs):
        rig.events.append(DETECTION_STARTED)
        rig.encoders_go.wait(10.0)
        rig.events.append(DETECTION_ANSWERED)
        return list(rig.detected)

    def list_audio():
        rig.events.append(AUDIO_LISTED)
        rig.audio_go.wait(10.0)
        return [AudioDevice(BLACKMAGIC_AUDIO)]

    def probe_audio(_device):
        return [AudioFormat(48000, 2, 16)]

    def no_dialog(*_args, **_kwargs):
        return QMessageBox.StandardButton.Ok

    for name in ("warning", "critical", "question", "information"):
        monkeypatch.setattr(QMessageBox, name, no_dialog)
    monkeypatch.setattr(
        preview_module, "enumerate_video_devices", lambda: [CaptureDevice(0, BLACKMAGIC)]
    )
    monkeypatch.setattr(preview_module, "FormatProbe", HeldProbe)
    monkeypatch.setattr(preview_module, "CameraCapture", CountingCamera)
    monkeypatch.setattr(record_module, "detect_encoders", detect)
    monkeypatch.setattr(record_module, "enumerate_audio_devices", list_audio)
    monkeypatch.setattr(record_module, "probe_audio_formats", probe_audio)
    CountingCamera.opened, CountingCamera.on_open = [], None
    HeldProbe.last, HeldProbe.events = None, rig.events
    forget_detected_encoders()

    def build():
        from wer.ui.main_window import MainWindow

        window = MainWindow()
        rig.windows.append(window)
        return window

    rig.build = build
    try:
        yield rig
    finally:
        rig.encoders_go.set()
        rig.audio_go.set()
        for gate in rig.gates:
            gate.set()
        for thread in threading.enumerate():
            if thread.name in WORKERS:
                thread.join(10.0)
        for window in rig.windows:
            if shiboken6.isValid(window):
                window.close()
        qt_app.processEvents()
        CountingCamera.on_open = None
        forget_detected_encoders()


def probe_formats_on_cue(rig, monkeypatch, looks: int) -> SimpleNamespace:
    """Put the real FormatProbe back, over a probe_formats that waits for a cue.

    Every camera look probes the one stand-in camera, so call n is look n. Look
    n waits for gates[n], and any look past the last gate for the last one.
    Counts how many looks were ever asking at the same time.
    """
    from wer.ui import preview as preview_module

    cues = SimpleNamespace(
        gates=[threading.Event() for _ in range(looks)],
        started=0,
        asking=0,
        most_at_once=0,
    )
    rig.gates.extend(cues.gates)
    lock = threading.Lock()

    def probe_formats(_device):
        with lock:
            number = cues.started
            cues.started += 1
            cues.asking += 1
            cues.most_at_once = max(cues.most_at_once, cues.asking)
        try:
            cues.gates[min(number, looks - 1)].wait(10.0)
        finally:
            with lock:
                cues.asking -= 1
        return list(FORMATS)

    monkeypatch.setattr(preview_module, "FormatProbe", RealFormatProbe)
    monkeypatch.setattr(preview_module, "probe_formats", probe_formats)
    return cues


@pytest.mark.parametrize("held", ["the audio input check", "encoder detection"])
def test_the_interface_answers_while_a_start_up_query_runs(startup, qt_app, held) -> None:
    """Each query is held for the whole measurement here, where on the rig the
    audio list held the interface thread for 0.30 s and detection for 1.37 s.
    They no longer run together -- the camera waits for the audio check, and
    detection starts once the camera has opened -- so each is measured on its
    own. A timer must keep ticking, and a queued signal from another thread --
    the console finder's result is one -- must be handled as soon as it lands."""
    window = startup.build()
    HeldProbe.last.answer()  # the cameras' formats are in, so the audio check starts
    if held == "encoder detection":
        startup.audio_go.set()  # so the camera starts, and detection after it
        assert wait_for(qt_app, lambda: DETECTION_STARTED in startup.events), (
            "encoder detection never started"
        )
    else:
        assert wait_for(qt_app, lambda: AUDIO_LISTED in startup.events), (
            "the audio check never started"
        )

    # Measured: this test saw a 0.53 s stall in a full run of 1,249 tests while
    # other test runs shared the machine, and passed alone three times out of
    # three and among the tests before it twice. Earlier tests leave windows
    # for Qt to delete and cycles for the collector, and either can land inside
    # the measured second; neither is what this test is about. They are
    # settled first, and the collector waits until the second is over.
    #
    # Only deletions are flushed here, never other pending events. A query
    # posted to the interface thread and run before the timer starts spends its
    # stall outside the measurement -- the wait above can do exactly that -- so
    # the precondition at the end checks the query is still running, which a
    # query run to completion on this thread is not.
    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    ticks = [time.monotonic()]
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    sent: list[float] = []
    handled: list[float] = []
    window.console_search_finished.connect(
        lambda _profile, _results: handled.append(time.monotonic())
    )

    def console_finder_answers() -> None:
        time.sleep(0.4)
        sent.append(time.monotonic())
        window.console_search_finished.emit(None, [])

    loop = QEventLoop()
    QTimer.singleShot(1000, loop.quit)
    threading.Thread(target=console_finder_answers, daemon=True).start()
    gc.disable()
    try:
        timer.start()
        loop.exec()
        timer.stop()
    finally:
        gc.enable()

    # Both queries are held for the whole second, so either one back on the
    # interface thread stalls it for all of that second or more (10 s and 20 s
    # in the mutants that proved this test). 0.6 s still fails that plainly and
    # does not fail a busy machine: 0.15 s did, at 0.53 s.
    worst = max(later - earlier for earlier, later in zip(ticks, ticks[1:] + [time.monotonic()]))
    assert worst < RESPONSIVE_WITHIN, f"the interface thread stalled for {worst:.2f} s"
    assert sent and handled, "the console's result was never handled"
    assert handled[0] - sent[0] < RESPONSIVE_WITHIN, (
        f"the console's result waited {handled[0] - sent[0]:.2f} s to be handled"
    )
    # And the measurement meant something: the query was running throughout
    # it. A mutant with the query back on the interface thread spends its
    # stall inside the wait before the measurement instead, and is caught
    # here, having answered.
    if held == "encoder detection":
        assert DETECTION_ANSWERED not in startup.events, "precondition: detection answered"
    else:
        assert window.record_panel.audio_inputs_pending, (
            "precondition: the audio check answered"
        )


def test_the_camera_waits_for_its_formats_and_the_audio_inputs_starts_once_and_then_detects_encoders(
    startup, qt_app, caplog
) -> None:
    """Nothing may query a capture device while the camera is opened and its
    rate measured, so its automatic start waits for the format probe and the
    audio check, and says in the log how long each took. Nor may a test encode
    run then, but opening a camera needs nothing detection finds, so detection
    is not waited for: it starts once the camera has opened. On a freshly
    installed Surface Pro 8 it was the last to answer, at 8.59 s."""
    caplog.set_level(logging.INFO, logger="wer.ui.startup")
    detection_started_at_open: list[bool] = []
    CountingCamera.on_open = lambda: detection_started_at_open.append(
        startup.windows[-1].record_panel._detection_started
    )
    window = startup.build()
    panel = window.record_panel
    pump(qt_app, 0.2)
    assert CountingCamera.opened == [], "opened before its formats were in"
    assert DETECTION_STARTED not in startup.events, "encoders were detected before the camera opened"

    HeldProbe.last.answer()
    assert wait_for(qt_app, lambda: AUDIO_LISTED in startup.events)
    pump(qt_app, 0.2)
    assert CountingCamera.opened == [], "opened while the audio inputs were being asked"

    startup.audio_go.set()
    assert wait_for(qt_app, lambda: CountingCamera.opened), "the camera never started"
    assert detection_started_at_open == [False], "encoder detection started before the camera opened"
    assert wait_for(qt_app, lambda: DETECTION_STARTED in startup.events), (
        "encoder detection never started once the camera had opened"
    )
    pump(qt_app, 0.3)
    assert panel.encoders_pending, "precondition: detection is still being held"
    assert CountingCamera.opened == [0], "the camera was opened more than once"
    assert window.preview.capture is not None and window.preview.capture.is_running

    released = [
        record.getMessage() for record in caplog.records
        if "Camera autostart released" in record.getMessage()
    ]
    assert len(released) == 1, released
    for name in ("camera format probe", "audio input check"):
        assert re.search(rf"{name} took \d+\.\d\d s", released[0]), released[0]
    assert "encoder detection" not in released[0], released[0]

    startup.encoders_go.set()
    assert wait_for(qt_app, lambda: not panel.encoders_pending)
    pump(qt_app, 0.1)
    assert CountingCamera.opened == [0], "detection answering opened the camera again"
    assert window._pinned_messages == [], (
        "a start that waited for everything, with no take running, pinned something"
    )


def test_the_audio_inputs_are_not_asked_until_the_camera_formats_are_in(
    startup, qt_app
) -> None:
    """On the Blackmagic rig the audio input is the same UltraStudio whose video
    the format probe is asking about. The two sides are never queried at once."""
    window = startup.build()
    pump(qt_app, 0.3)
    assert AUDIO_LISTED not in startup.events, (
        "the audio inputs were listed while the camera formats were being probed"
    )
    assert window.record_panel.audio_inputs_pending

    HeldProbe.last.answer()
    assert wait_for(qt_app, lambda: AUDIO_LISTED in startup.events), (
        "the audio check never started once the formats were in"
    )
    assert startup.events.index(FORMATS_ANSWERED) < startup.events.index(AUDIO_LISTED)


def test_refresh_during_the_first_format_probe_holds_the_camera_and_the_audio_until_the_newest_look_answers(
    startup, qt_app, monkeypatch
) -> None:
    """"Checking supported formats..." is up for a second or so on the Blackmagic
    rig, and Refresh is not disabled meanwhile. Pressed then, it started a second
    look alongside the first, and the first to answer let the camera open and
    the audio check start while ffmpeg was still asking for the UltraStudio's
    video formats."""
    cues = probe_formats_on_cue(startup, monkeypatch, looks=2)
    startup.encoders_go.set()
    startup.audio_go.set()
    window = startup.build()
    assert wait_for(qt_app, lambda: cues.started == 1), "precondition: the first look never began"

    window.preview.refresh_devices()  # Refresh, with the first look still asking
    cues.gates[0].set()  # and then the first look answers
    assert wait_for(qt_app, lambda: cues.started == 2), "the newest look never ran"
    pump(qt_app, 0.3)
    assert window.preview.is_probing, "the first look's answer was taken for the newest"
    assert AUDIO_LISTED not in startup.events, (
        "the audio inputs were listed while a camera was still being probed"
    )
    assert CountingCamera.opened == [], "the camera opened while a camera was still being probed"

    cues.gates[1].set()
    assert wait_for(qt_app, lambda: CountingCamera.opened), "the camera never started"
    pump(qt_app, 0.3)
    assert CountingCamera.opened == [0], "the camera was opened more than once"
    assert AUDIO_LISTED in startup.events, "the audio check never started"
    assert cues.most_at_once == 1, "two looks were asking the cameras at the same time"


def test_a_format_answer_overtaken_by_a_newer_look_is_never_sent(qt_app, monkeypatch) -> None:
    """An answer is posted to the interface thread some time before it is handled
    there. A Refresh handled in between starts a new look, and the answer
    already posted is the old look's: taken for the new one, it would clear the
    panel's note that the cameras are still being probed."""
    from wer.ui import preview as preview_module

    blackmagic_go = threading.Event()

    def probe_formats(device):
        if device.name == BLACKMAGIC:
            blackmagic_go.wait(10.0)
        return list(FORMATS)

    monkeypatch.setattr(preview_module, "probe_formats", probe_formats)
    prober = RealFormatProbe()
    sent: list[dict] = []
    prober.finished.connect(lambda formats: sent.append(formats))
    try:
        prober.probe([CaptureDevice(0, WEBCAM)])
        for thread in [each for each in threading.enumerate() if each.name == "format-probe"]:
            thread.join(5.0)
        # The webcam's answer is posted, and nothing has run the loop to handle it.
        prober.probe([CaptureDevice(1, BLACKMAGIC)])
        pump(qt_app, 0.3)
        assert sent == [], "the old look's answer was sent as the newest"

        blackmagic_go.set()
        assert wait_for(qt_app, lambda: sent), "the newest look never answered"
        pump(qt_app, 0.1)
        assert sent == [{1: FORMATS}]
    finally:
        blackmagic_go.set()
        for thread in [each for each in threading.enumerate() if each.name == "format-probe"]:
            thread.join(5.0)


def test_refresh_that_finds_no_camera_while_a_look_is_running_leaves_the_audio_check_to_that_look(
    startup, qt_app, monkeypatch
) -> None:
    """A camera unplugged, and Refresh pressed, while its formats are still being
    probed. Finding nothing to probe is a finished look, but the look already
    running is still querying, and the audio check waits for its answer."""
    from wer.ui import preview as preview_module

    cues = probe_formats_on_cue(startup, monkeypatch, looks=1)
    startup.encoders_go.set()
    startup.audio_go.set()
    window = startup.build()
    assert wait_for(qt_app, lambda: cues.started == 1), "precondition: the look never began"

    monkeypatch.setattr(preview_module, "enumerate_video_devices", lambda: [])
    window.preview.refresh_devices()
    pump(qt_app, 0.3)
    assert AUDIO_LISTED not in startup.events, (
        "the audio inputs were listed while a camera was still being probed"
    )

    cues.gates[0].set()
    assert wait_for(qt_app, lambda: AUDIO_LISTED in startup.events), "the audio check never started"
    assert wait_for(qt_app, lambda: not window.record_panel.audio_inputs_pending)
    pump(qt_app, 0.2)
    assert CountingCamera.opened == [], "a camera was opened with none connected"


def test_a_query_that_never_answers_starts_the_camera_anyway_and_says_what_is_missing(
    startup, qt_app, monkeypatch, caplog
) -> None:
    """An audio input whose driver hangs its query must not keep the stage off
    the screen. The camera starts when the wait runs out, and the booth is told
    what it started without: here, that a take's audio input is left on the
    format DirectShow lists first. When the check does answer, that is said too."""
    from wer.ui import startup as startup_module

    # Three seconds. The format probe answers at once here, so even a machine
    # busy with the overnight soaks is well inside it.
    monkeypatch.setattr(startup_module, "FALLBACK_SECONDS", 3.0)
    caplog.set_level(logging.INFO, logger="wer.ui.startup")
    startup.encoders_go.set()
    window = startup.build()
    HeldProbe.last.answer()
    assert wait_for(qt_app, lambda: AUDIO_LISTED in startup.events), "the audio check never started"
    assert CountingCamera.opened == [], "started before the wait ran out"

    assert wait_for(qt_app, lambda: CountingCamera.opened, 8.0), "the camera never started"
    assert window.record_panel.audio_inputs_pending, "precondition: the audio check never answered"

    warnings = [
        record.getMessage() for record in caplog.records
        if record.name == "wer.ui.startup" and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, warnings
    assert "audio input check" in warnings[0]
    assert "format probe" not in warnings[0] and "encoder detection" not in warnings[0]
    assert window._pinned_messages, "nothing was pinned for the booth"
    pinned = window._pinned_messages[-1]
    assert "audio input check" in pinned and "44.1 kHz" in pinned, pinned
    assert window.statusBar().currentMessage() == pinned

    startup.audio_go.set()
    assert wait_for(qt_app, lambda: not window.record_panel.audio_inputs_pending)
    pump(qt_app, 0.2)
    assert CountingCamera.opened == [0], "the late answer opened the camera again"
    assert "audio input check has now answered" in window._pinned_messages[-1], (
        window._pinned_messages
    )


def test_a_format_probe_that_outlasts_the_wait_opens_the_camera_when_it_answers_and_only_then_asks_the_audio(
    startup, qt_app, monkeypatch, caplog
) -> None:
    """A slow Blackmagic probe can outlast the wait. The camera must then open
    the moment its formats land, not never. And the audio check, which only a
    finished format probe starts, must start after that open and not alongside
    it, or the UltraStudio's audio is queried while its video is being opened."""
    from wer.ui import startup as startup_module

    monkeypatch.setattr(startup_module, "FALLBACK_SECONDS", 0.5)
    caplog.set_level(logging.INFO, logger="wer.ui.startup")
    startup.encoders_go.set()
    window = startup.build()
    panel = window.record_panel
    started_at_open: list[tuple[bool, bool]] = []
    CountingCamera.on_open = lambda: started_at_open.append(
        (panel._audio_probe is not None, panel._detection_started)
    )

    assert wait_for(qt_app, lambda: window._pinned_messages), "nothing was pinned when the wait ran out"
    pinned = window._pinned_messages[-1]
    assert "camera format probe" in pinned, pinned
    pump(qt_app, 0.3)
    assert CountingCamera.opened == [], "the camera opened before its formats were in"
    assert AUDIO_LISTED not in startup.events, (
        "the audio inputs were asked while the camera formats were being probed"
    )
    assert DETECTION_STARTED not in startup.events, (
        "encoder detection started with the camera still to open"
    )

    HeldProbe.last.answer()
    assert wait_for(qt_app, lambda: CountingCamera.opened), (
        "the camera never started once its formats came in"
    )
    assert started_at_open == [(False, False)], (
        "the audio check or encoder detection was started before the camera was opened"
    )
    assert wait_for(qt_app, lambda: DETECTION_STARTED in startup.events), (
        "encoder detection never started"
    )
    assert wait_for(qt_app, lambda: AUDIO_LISTED in startup.events), "the audio check never started"
    startup.audio_go.set()
    assert wait_for(qt_app, lambda: not panel.audio_inputs_pending)
    pump(qt_app, 0.2)
    assert CountingCamera.opened == [0], "the camera was opened more than once"
    assert any(
        "camera format probe answered" in record.getMessage()
        and "after the camera stopped waiting" in record.getMessage()
        for record in caplog.records
    ), "the late answer was not logged"


def test_a_take_started_before_detection_answers_says_it_is_on_software_and_again_when_detection_lands(
    startup, qt_app
) -> None:
    """Detection finishes after the picture is up, so a take can start before it
    has answered -- from the desk, at the top of a session. That take is armed on
    software and stays on it (see the next test), which on a laptop without the
    headroom is a take dropping frames for as long as it runs, so it is pinned
    as the take starts. That stays until Record is pressed at the machine, so
    detection landing has to be said too, or the booth goes on reading that
    Automatic means software."""
    startup.detected = [
        EncoderAvailability(SOFTWARE_ENCODER, True),
        EncoderAvailability(NVENC, True),
    ]
    window = startup.build()
    HeldProbe.last.answer()
    startup.audio_go.set()
    assert wait_for(qt_app, lambda: DETECTION_STARTED in startup.events), "detection never started"
    assert window.record_panel.encoders_pending, "precondition: detection has not answered"
    armed = window.record_panel.output_settings()  # a take armed now, as _start_recording would

    # What _start_recording does once the recorder is running. No events run
    # while the take is pretended, so nothing else sees it.
    window._take_in_progress, window.recorder.settings = True, armed
    try:
        window._note_a_take_armed_before_detection()
    finally:
        window._take_in_progress, window.recorder.settings = False, None
    assert window._pinned_messages, "the take going out on software was not pinned"
    assert f"records on {SOFTWARE_ENCODER.label}" in window._pinned_messages[-1], (
        window._pinned_messages[-1]
    )

    startup.encoders_go.set()
    assert wait_for(qt_app, lambda: not window.record_panel.encoders_pending)
    pump(qt_app, 0.1)
    follow_ups = [
        message for message in window._pinned_messages
        if "Encoder detection has now answered" in message
    ]
    assert len(follow_ups) == 1, window._pinned_messages
    assert f"records on {NVENC.label}" in follow_ups[0], follow_ups[0]
    assert "already running" not in follow_ups[0], "no take was running by then"

    # The same answer arriving with that take still running, straight into the slot.
    window._take_armed_before_detection = True
    window._take_in_progress, window.recorder.settings = True, armed
    try:
        window._encoders_detected()
    finally:
        window._take_in_progress, window.recorder.settings = False, None
    assert f"take already running stays on {SOFTWARE_ENCODER.label}" in (
        window._pinned_messages[-1]
    ), window._pinned_messages[-1]


def test_a_take_started_once_detection_has_answered_pins_nothing_about_its_encoder(
    startup, qt_app
) -> None:
    startup.detected = [
        EncoderAvailability(SOFTWARE_ENCODER, True),
        EncoderAvailability(NVENC, True),
    ]
    startup.encoders_go.set()
    window = startup.build()
    HeldProbe.last.answer()
    startup.audio_go.set()
    assert wait_for(qt_app, lambda: not window.record_panel.encoders_pending)
    armed = window.record_panel.output_settings()

    window._take_in_progress, window.recorder.settings = True, armed
    try:
        window._note_a_take_armed_before_detection()
        window._encoders_detected()
    finally:
        window._take_in_progress, window.recorder.settings = False, None
    assert window._pinned_messages == [], window._pinned_messages


def test_a_take_armed_before_detection_answers_keeps_software_when_detection_lands(
    startup, qt_app
) -> None:
    """The recorder builds a new ffmpeg command for every part it restarts.
    Handed "auto", it resolved it again each time, so a take armed on software
    after the wait ran out would come out on NVENC from its next part once
    detection had found the GPU, with nothing on screen to say so."""
    from wer.ui import record_panel as module

    startup.detected = [
        EncoderAvailability(SOFTWARE_ENCODER, True),
        EncoderAvailability(NVENC, True),
    ]
    panel = module.RecordPanel(RecordingConfig())
    armed = panel.output_settings()
    assert armed.resolve_encoder().name == SOFTWARE_ENCODER.name, "precondition: nothing detected yet"

    startup.encoders_go.set()
    assert wait_for(qt_app, lambda: not panel.encoders_pending)
    assert panel.output_settings().resolve_encoder().name == NVENC.name, (
        "precondition: Automatic now means NVENC"
    )
    assert armed.resolve_encoder().name == SOFTWARE_ENCODER.name, (
        "the armed take would change encoder at its next part"
    )


def test_the_placeholders_never_change_the_saved_encoder_or_audio_input(
    startup, qt_app
) -> None:
    """Both boxes write whichever row is current into the show file, and the
    first row added to an empty box becomes current. A placeholder that counted
    as a choice would replace a saved Quick Sync or UltraStudio setting, and the
    next autosave would keep it."""
    from wer.ui import record_panel as module

    startup.detected = [
        EncoderAvailability(encoder, encoder.name in ("libx264", "h264_qsv"))
        for encoder in ALL_ENCODERS
    ]
    config = RecordingConfig(encoder="h264_qsv", audio_device=BLACKMAGIC_AUDIO)
    saved = (config.encoder, config.audio_device)
    panel = module.RecordPanel(config)
    changes: list[bool] = []
    panel.settings_changed.connect(lambda: changes.append(True))
    panel.attach(IdleRecorder())

    panel.start_audio_inputs()
    assert wait_for(qt_app, lambda: AUDIO_LISTED in startup.events)
    assert panel.encoders_pending and panel.audio_inputs_pending, "precondition"
    assert (config.encoder, config.audio_device) == saved

    panel.load_settings()  # another show file opened meanwhile
    panel._refresh()  # the 500 ms tick, which enables what a take is not using
    assert (config.encoder, config.audio_device) == saved
    for box in (panel._encoder_box, panel._audio_box):
        assert not box.isEnabled(), f"the placeholder {box.currentText()!r} can be chosen"
    assert "not connected" not in panel._audio_box.currentText(), (
        "the saved input was called absent before the inputs had been listed"
    )

    startup.encoders_go.set()
    startup.audio_go.set()
    assert wait_for(
        qt_app, lambda: not panel.encoders_pending and not panel.audio_inputs_pending
    )
    assert (config.encoder, config.audio_device) == saved
    assert panel._encoder_box.currentData() == "h264_qsv"
    assert panel._audio_box.currentData() == BLACKMAGIC_AUDIO
    assert panel._encoder_box.isEnabled() and panel._audio_box.isEnabled()
    assert changes == [], "filling the boxes was reported as a settings change"


def test_encoder_detection_that_fails_still_answers_so_the_box_says_why(
    startup, qt_app, monkeypatch, caplog
) -> None:
    """Silence from a failed detection would leave the Encoder box disabled on
    its placeholder for the whole session. It answers instead: software, and
    every hardware encoder listed as unavailable with the reason."""
    from wer.ui import record_panel as module

    def detection_that_fails(**_kwargs):
        raise RuntimeError("ffmpeg -encoders crashed")

    monkeypatch.setattr(module, "detect_encoders", detection_that_fails)
    caplog.set_level(logging.INFO)
    window = startup.build()
    panel = window.record_panel
    HeldProbe.last.answer()
    startup.audio_go.set()  # so the camera starts, and detection after it
    assert wait_for(qt_app, lambda: CountingCamera.opened), "the camera never started"
    assert wait_for(qt_app, lambda: not panel.encoders_pending), "failed detection never answered"
    assert any(
        record.levelno >= logging.ERROR and "Encoder detection failed" in record.getMessage()
        for record in caplog.records
    ), "the failure was not logged"

    box = panel._encoder_box
    assert box.isEnabled(), "the Encoder box stayed disabled"
    rows = {
        box.itemData(row): (box.itemText(row), box.model().item(row).isEnabled())
        for row in range(box.count())
    }
    assert AUTO_ENCODER in rows, rows
    assert rows[SOFTWARE_ENCODER.name][1], "software was not offered"
    for encoder in HARDWARE_ENCODERS:
        text, enabled = rows[encoder.name]
        assert "detection failed; see the log" in text and not enabled, (encoder.name, text)
    assert window._pinned_messages == [], window._pinned_messages


def test_an_audio_list_that_fails_still_answers_with_video_only_and_the_camera_starts(
    startup, qt_app, monkeypatch, caplog
) -> None:
    """The same for the audio inputs. An error listing them leaves the Audio box
    offering video only, not a placeholder that cannot be changed, and the
    camera is not held for an answer that is never coming."""
    from wer.ui import record_panel as module

    def list_that_fails():
        startup.events.append(AUDIO_LISTED)
        raise RuntimeError("ffmpeg -list_devices crashed")

    monkeypatch.setattr(module, "enumerate_audio_devices", list_that_fails)
    caplog.set_level(logging.INFO)
    startup.encoders_go.set()

    # On its own, with nothing saved, so what the box offers is exactly this.
    lone = module.RecordPanel(RecordingConfig())
    lone.start_audio_inputs()
    assert wait_for(qt_app, lambda: not lone.audio_inputs_pending), "a failed audio list never answered"
    assert [lone._audio_box.itemText(row) for row in range(lone._audio_box.count())] == [
        "None — video only"
    ]
    assert lone._audio_box.isEnabled(), "the Audio box stayed disabled"
    assert any(
        record.levelno >= logging.ERROR and "Could not list the audio inputs" in record.getMessage()
        for record in caplog.records
    ), "the failure was not logged"

    window = startup.build()
    HeldProbe.last.answer()
    assert wait_for(qt_app, lambda: CountingCamera.opened), (
        "the camera was held for an audio check that had already answered"
    )
    assert window._pinned_messages == [], window._pinned_messages


@pytest.mark.parametrize("running", ["the audio input check", "encoder detection"])
def test_a_window_closed_while_a_start_up_query_runs_survives_its_answer(
    startup, qt_app, monkeypatch, running
) -> None:
    """test_app_lifecycle closes the window 100 ms after launch, well inside the
    time either query takes. The panel the answer was for is gone by the time it
    comes back, and nothing may raise on either thread. Closed before its camera
    started, the window must not go on to start detection either: a test encode
    on every GPU, for a window that no longer exists."""
    thread_errors: list[BaseException] = []
    slot_errors: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: thread_errors.append(args.exc_value))
    monkeypatch.setattr(sys, "excepthook", lambda _kind, value, _tb: slot_errors.append(value))

    window = startup.build()
    HeldProbe.last.answer()
    if running == "encoder detection":
        startup.audio_go.set()  # so the camera starts, and detection after it
        assert wait_for(qt_app, lambda: DETECTION_STARTED in startup.events)
    else:
        assert wait_for(qt_app, lambda: AUDIO_LISTED in startup.events)
    opened_before_close = list(CountingCamera.opened)
    panel = window.record_panel
    window.close()
    shiboken6.delete(window)
    assert not shiboken6.isValid(panel), "precondition: the panel outlived its window"

    startup.encoders_go.set()
    startup.audio_go.set()
    for thread in [thread for thread in threading.enumerate() if thread.name in WORKERS]:
        thread.join(5.0)
        assert not thread.is_alive(), f"{thread.name} never finished"
    pump(qt_app, 0.3)

    assert thread_errors == [], f"a worker raised answering a closed window: {thread_errors}"
    assert slot_errors == [], f"a slot raised for a closed window: {slot_errors}"
    assert CountingCamera.opened == opened_before_close, (
        "a camera was opened for a window that had closed"
    )
    if running == "the audio input check":
        assert DETECTION_STARTED not in startup.events, (
            "encoder detection started for a window that had closed"
        )


def test_an_answer_that_lands_after_the_window_has_closed_opens_no_camera(
    startup, qt_app
) -> None:
    """Shutting down closes the window and quits the application a moment later.
    A start-up query answering in between, or the wait running out, must not
    set about opening a camera for a window that is going away."""
    window = startup.build()
    HeldProbe.last.answer()
    assert wait_for(qt_app, lambda: AUDIO_LISTED in startup.events)
    # The wait set to run out half a second from now, just before the close, so
    # the test does not depend on how long the window took to build.
    window._startup._fallback.start(500)
    window.close()

    startup.encoders_go.set()
    startup.audio_go.set()
    assert wait_for(qt_app, lambda: not window.record_panel.audio_inputs_pending)
    pump(qt_app, 0.8)  # past the wait running out, too
    assert CountingCamera.opened == [], "a camera was opened for a window that had closed"
    assert DETECTION_STARTED not in startup.events, (
        "encoder detection started for a window that had closed"
    )


@pytest.mark.parametrize("autosaved", [False, True], ids=["first launch", "a later launch"])
def test_a_first_launch_starts_on_a_microphone_and_a_later_one_keeps_video_only(
    startup, qt_app, monkeypatch, tmp_path, autosaved
) -> None:
    """The main window decides what counts as a first launch -- no autosave yet
    -- and hands that to the audio check. A later launch saved on None stays on
    it: RecordPanel._choose_first_input says why."""
    from wer.ui import main_window as main_module
    from wer.ui import record_panel as record_module

    saved = tmp_path / "autosave.wer"
    if autosaved:
        saved.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main_module, "autosave_path", lambda: saved)
    monkeypatch.setattr(record_module, "default_audio_input_name", lambda: BLACKMAGIC_AUDIO)
    window = startup.build()
    window.record_panel.config.audio_device = None  # whatever an earlier test left saved
    HeldProbe.last.answer()
    startup.audio_go.set()
    assert wait_for(qt_app, lambda: not window.record_panel.audio_inputs_pending)

    expected = None if autosaved else BLACKMAGIC_AUDIO
    assert window.record_panel.config.audio_device == expected
    assert window.record_panel._audio_box.currentData() == expected
