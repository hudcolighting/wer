"""A long take keeps its camera, and the log says when the camera struggles.

Two failures, both of the kind found the next morning.

**A camera that drops out and comes back.** Ten minutes into a four-hour take
the Blackmagic's Thunderbolt link drops for a second. Capture went on calling
read() on the handle it already had, and nothing ever reopened the device:
Device, Format and Refresh are all locked while recording, for good reason.
The take stayed open and markers and chapters kept landing over the rest of
the evening, with video only if that one handle started delivering again --
which nobody has measured a DirectShow handle doing.

**A rate that falls and stays down.** In a three-hour soak take the Integrated
Camera spent 66 s under 20 fps, as low as 12.3, with no failed reads and no
dropped frames. cfr filled the gaps, so the file looked right, and wer.log said
nothing at all.

Nothing here opens a real camera. The capture loop plays a scripted stand-in
for cv2.VideoCapture on a fake clock, and the preview's reopen runs against a
stand-in CameraCapture and a fake device list. How a real DirectShow device
fails when it is pulled, and what index it comes back under, are exactly what
these cannot show; that takes a replug test on the rig.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QObject, Signal

from wer.video import capture as capture_module
from wer.video.capture import CameraCapture, CaptureSettings, CaptureStats
from wer.video.devices import CaptureDevice, VideoFormat

# ------------------------------------------------------------ the capture loop


class Clock:
    """Stands in for the time module inside wer.video.capture."""

    def __init__(self) -> None:
        self.t = 1000.0

    def perf_counter(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class ScriptedDevice:
    """Enough of cv2.VideoCapture to fail on cue.

    One script entry per read. "frame" is a flat black frame, which is what the
    Blackmagic delivers once its HDMI signal is lost; "fail" is read() returning
    False; "raise" is a cv2.error out of read(). A frame costs one frame period
    on the clock, a failed read only the capture loop's own sleep. The last
    entry stops the capture, so the loop ends having handled it.
    """

    def __init__(self, camera, clock: Clock, script, *, watch_stalled: bool) -> None:
        self.camera = camera
        self.clock = clock
        self.script = deque(script)
        self.watch_stalled = watch_stalled
        self.image = np.zeros((36, 64, 3), dtype=np.uint8)
        #: CameraCapture.stalled_for as it stood at each read.
        self.stalled: list[float] = []

    def read(self):
        if self.watch_stalled:
            self.stalled.append(self.camera.stalled_for)
        step = self.script.popleft()
        if not self.script:
            self.camera._stop.set()
        if step == "frame":
            self.clock.t += 1 / 30
            return True, self.image
        if step == "raise":
            raise cv2.error("stand-in for an error inside the backend")
        return False, None

    def release(self) -> None:
        pass


def scripted_camera(monkeypatch, script, *, on_error=None, watch_stalled=False):
    """A CameraCapture over a script, on a fake clock. _run() plays it here."""
    clock = Clock()
    monkeypatch.setattr(capture_module, "time", clock)
    camera = CameraCapture(CaptureSettings(width=64, height=36), on_error=on_error)
    device = ScriptedDevice(camera, clock, script, watch_stalled=watch_stalled)
    camera._capture = device
    camera.stats = CaptureStats(started_at=clock.t)
    return camera, clock, device


def test_reads_that_keep_failing_are_timed_as_the_camera_being_gone(monkeypatch) -> None:
    """What the preview reopens a camera on. Six hundred failed reads at the
    loop's 10 ms apiece is six seconds of a camera that is not there."""
    camera, _, _ = scripted_camera(monkeypatch, ["frame"] * 30 + ["fail"] * 600)
    camera._run()
    assert camera.stalled_for == pytest.approx(6.0, abs=0.05)


def test_one_good_frame_ends_the_count(monkeypatch) -> None:
    """A camera that came back is not gone, however long it was away."""
    camera, _, _ = scripted_camera(
        monkeypatch, ["frame"] * 30 + ["fail"] * 600 + ["frame"]
    )
    camera._run()
    assert camera.stalled_for == 0.0


def test_a_black_picture_is_never_counted_as_a_dropout(monkeypatch) -> None:
    """The Blackmagic with its HDMI cable out: flat black at 30 fps, every read
    a success. Known and accepted, and nothing a reopen could fix, so it must
    never count towards one -- or a lost signal would cycle the device through
    reopens for the rest of the take."""
    camera, _, device = scripted_camera(
        monkeypatch, ["frame"] * 30 * 60, watch_stalled=True
    )
    camera._run()
    assert camera.stats.frames_captured == 1800
    assert max(device.stalled) == 0.0, "a black picture was timed as a dropout"


def test_a_read_that_raises_is_a_failed_read_not_the_end_of_capture(monkeypatch) -> None:
    """An exception out of read() was outside the loop's only try, so it ended
    the capture thread with a crash line in the log and nothing else: no
    message on the picture, no failures counted, nothing left to recover."""
    camera, _, _ = scripted_camera(
        monkeypatch, ["frame"] * 10 + ["raise"] * 5 + ["frame"] * 10
    )
    camera._run()
    assert camera.stats.frames_captured == 20, "capture did not survive the exception"
    assert camera.stats.read_failures == 5


def test_a_capture_thread_that_dies_says_so_and_counts_as_gone(monkeypatch) -> None:
    """Anything else that escapes the loop still ends capture. It has to end it
    out loud, and in a way the preview can reopen the camera on."""
    errors: list[str] = []
    camera, clock, _ = scripted_camera(
        monkeypatch, ["frame"] * 60, on_error=errors.append
    )
    calls = {"n": 0}
    real_orient = camera._orient

    def orient_then_fail(image):
        calls["n"] += 1
        if calls["n"] > 5:
            raise ValueError("stand-in for a bug inside the loop")
        return real_orient(image)

    monkeypatch.setattr(camera, "_orient", orient_then_fail)
    camera._run()

    assert errors and "stopped" in errors[-1].lower(), errors
    clock.t += 10.0
    assert camera.stalled_for >= 10.0


def test_frames_that_come_back_on_the_same_handle_are_logged(monkeypatch, caplog) -> None:
    """Whether a DirectShow handle ever delivers again after its device drops
    out has never been measured. When one does, wer.log has to say so, or a
    replug test on the rig cannot tell it apart from a reopen."""
    camera, _, _ = scripted_camera(
        monkeypatch, ["frame"] * 30 + ["fail"] * 100 + ["frame"] * 5
    )
    with caplog.at_level(logging.WARNING, logger="wer.video.capture"):
        camera._run()
    messages = [record.getMessage() for record in caplog.records]
    assert any("same camera handle" in message for message in messages), messages


def test_a_device_that_flaps_does_not_log_every_time_its_frames_come_back(
    monkeypatch, caplog
) -> None:
    """Thirty-odd failed reads and one frame, over and over. A line every time
    frames resumed would be three a second for as long as it lasted, on top of
    the failed-read alarm's own, burying the line a replug test is looking
    for. The 1st, 10th, 100th and 1000th are logged, and the total goes in the
    line capture stops with."""
    camera, _, _ = scripted_camera(monkeypatch, (["fail"] * 30 + ["frame"]) * 200)
    with caplog.at_level(logging.INFO, logger="wer.video.capture"):
        camera._run()
    messages = [record.getMessage() for record in caplog.records]
    resumed = [message for message in messages if "same camera handle" in message]
    assert len(resumed) == 3, resumed
    exiting = [
        message for message in messages if message.startswith("Capture thread exiting")
    ]
    assert len(exiting) == 1, exiting
    assert "resumed on the same handle 200 time(s)" in exiting[0], exiting[0]


def test_a_slow_overlay_is_never_timed_as_a_dropout(monkeypatch) -> None:
    """stalled_for counts failed reads, not time since the last frame. An
    overlay that takes six seconds over each frame -- on a starved machine,
    say -- runs up six seconds between frames on a camera that is delivering,
    and timed that way the preview would close and reopen a healthy camera in
    the middle of a take."""
    camera, clock, device = scripted_camera(
        monkeypatch, ["frame"] * 20, watch_stalled=True
    )

    def slow_overlay(image):
        clock.t += 6.0
        return image

    camera.frame_processor = slow_overlay
    camera._run()
    assert camera.stats.frames_captured == 20
    assert max(device.stalled) == 0.0, device.stalled


class BlockingDevice:
    """A read() that does not come back until told to.

    ``failures`` reads fail first, as a device that has dropped out does. With
    ``with_frame`` the stuck read comes back with a frame rather than nothing.
    """

    def __init__(self, *, failures: int = 0, with_frame: bool = False) -> None:
        self.reading = threading.Event()
        self.let_go = threading.Event()
        self.in_read = False
        self.released = False
        self.released_during_read = False
        self.failures = failures
        self.with_frame = with_frame

    def read(self):
        if self.failures:
            self.failures -= 1
            return False, None
        self.in_read = True
        self.reading.set()
        self.let_go.wait(5.0)
        self.in_read = False
        if self.with_frame:
            return True, np.zeros((36, 64, 3), dtype=np.uint8)
        return False, None

    def release(self) -> None:
        self.released_during_read = self.in_read
        self.released = True


def test_stop_leaves_the_device_to_a_read_that_has_not_returned() -> None:
    """stop() released the device once its join timed out, whatever the
    capture thread was doing, and said nothing to its caller about it. Now the
    preview reopens cameras mid-take, and opening a device an old read still
    holds is two handles on one camera -- so stop() has to say when it has
    not really let go, and leave the release to the thread."""
    device = BlockingDevice()
    camera = CameraCapture(CaptureSettings(width=64, height=36))
    camera._capture = device
    camera.start()
    thread = camera._thread
    try:
        assert device.reading.wait(2.0), "the capture thread never reached read()"
        assert camera.stop(timeout=0.1) is False, (
            "claimed to have let go of a device a read still holds"
        )
        assert not device.released, "released the device underneath a read"
    finally:
        device.let_go.set()
        thread.join(2.0)
    assert device.released, "nobody released the device once the read returned"
    assert not device.released_during_read


def test_a_capture_stop_gave_up_on_does_not_read_as_delivering_once_its_read_returns() -> None:
    """The preview keeps a capture stop() gave up on while a reopen waits for
    it. When the stuck read came back with a frame, that frame cleared the
    failure count on the thread's way out, and stalled_for read 0.0 on a
    capture with no thread: the preview took the camera for delivering again by
    itself and stopped looking, and the take had no more video."""
    device = BlockingDevice(failures=5, with_frame=True)
    camera = CameraCapture(CaptureSettings(width=64, height=36))
    camera._capture = device
    camera.start()
    thread = camera._thread
    try:
        assert device.reading.wait(2.0), "the capture thread never reached the stuck read"
        assert camera.stalled_for > 0.0, "the failed reads before it were not timed"
        assert camera.stop(timeout=0.1) is False
    finally:
        device.let_go.set()
        thread.join(2.0)
    assert not camera.is_running
    assert camera.stats.frames_captured == 1, "the stuck read never delivered its frame"
    assert camera.stalled_for > 0.0, "a capture with no thread left reads as delivering"
    assert device.released and not device.released_during_read


def test_asking_a_capture_to_stop_again_does_not_wait_on_the_same_read_again(
    caplog,
) -> None:
    """The preview stops the old capture at every reopen attempt, on the
    interface thread. With a read that stayed stuck, every stop after the first
    sat out the whole timeout again: the interface, Stop Recording included,
    froze for three seconds at every attempt, every 30 s for the rest of the
    take."""
    device = BlockingDevice()
    camera = CameraCapture(CaptureSettings(width=64, height=36))
    camera._capture = device
    camera.start()
    thread = camera._thread
    try:
        assert device.reading.wait(2.0), "the capture thread never reached read()"
        with caplog.at_level(logging.WARNING, logger="wer.video.capture"):
            assert camera.stop(timeout=0.1) is False
            asked = time.monotonic()
            assert camera.stop(timeout=3.0) is False, "claimed a stuck read had let go"
            waited = time.monotonic() - asked
        assert waited < 1.0, f"waited {waited:.1f} s again on a read it had given up on"
        gave_up = [r for r in caplog.records if "did not stop" in r.getMessage()]
        assert len(gave_up) == 1, [r.getMessage() for r in gave_up]
        assert not device.released, "released the device underneath a read"
    finally:
        device.let_go.set()
        thread.join(2.0)
    assert camera.stop() is True, "the thread has let go, and stop() still says not"
    assert device.released and not device.released_during_read


def test_a_capture_stop_gave_up_on_is_not_delivering_while_it_finishes_its_last_frame() -> None:
    """A capture stop() has given up on delivers nothing to anyone again. But
    when its stuck read came back with a frame, the thread cleared its failure
    count and took that frame through the overlay and the queues before it saw
    the stop, and for that pass stalled_for read 0.0 on a running thread. A
    dropout tick landing there logged the camera as delivering again by itself
    and dropped the reopen; the next tick found it lost again, with the gap
    measured from then."""
    device = BlockingDevice(failures=5, with_frame=True)
    camera = CameraCapture(CaptureSettings(width=64, height=36))
    camera._capture = device
    in_overlay, let_overlay_finish = threading.Event(), threading.Event()

    def overlay(image):
        in_overlay.set()
        let_overlay_finish.wait(5.0)
        return image

    camera.frame_processor = overlay
    camera.start()
    thread = camera._thread
    try:
        assert device.reading.wait(2.0), "the capture thread never reached the stuck read"
        assert camera.stop(timeout=0.1) is False
        device.let_go.set()
        assert in_overlay.wait(2.0), "the stuck read never delivered its frame"
        assert camera.is_running
        assert camera.stalled_for > 0.0, "a capture stop() gave up on reads as delivering"
    finally:
        device.let_go.set()
        let_overlay_finish.set()
        thread.join(2.0)
    assert not camera.is_running and camera.stalled_for > 0.0


# ------------------------------------------------------------ a falling rate


def feed(watch, start: float, seconds: float, fps: float) -> float:
    """Frames evenly at ``fps`` for ``seconds``, handed over as the loop does."""
    for n in range(1, round(seconds * fps) + 1):
        watch.tick(start + n / fps)
        watch.frame()
    return start + seconds


def silence(watch, start: float, seconds: float) -> float:
    """Failed reads every 10 ms: the loop goes on ticking and nothing arrives."""
    for n in range(1, round(seconds * 100) + 1):
        watch.tick(start + n / 100)
    return start + seconds


def rate_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "wer.video.capture" and record.levelno >= logging.WARNING
    ]


def test_a_rate_that_falls_and_stays_down_is_logged_once_and_so_is_its_recovery(
    caplog,
) -> None:
    """The soak-take case, a minute at 12 fps. One line when it has plainly
    happened, one when it has plainly stopped, and how long it lasted."""
    watch = capture_module.RateWatch(30.0, started_at=0.0)
    with caplog.at_level(logging.WARNING, logger="wer.video.capture"):
        t = feed(watch, 0.0, 20, 30)
        t = feed(watch, t, 60, 12)
        feed(watch, t, 30, 30)

    lines = rate_warnings(caplog)
    assert len(lines) == 2, lines
    fell, back = lines
    assert "against the 30 requested" in fell, fell
    assert float(re.search(r"delivering ([\d.]+) fps", fell).group(1)) == pytest.approx(
        12, abs=0.5
    ), fell
    assert float(re.search(r"back to ([\d.]+) fps", back).group(1)) == pytest.approx(
        30, abs=0.5
    ), back
    assert float(re.search(r"after (\d+) s", back).group(1)) == pytest.approx(
        60, abs=1
    ), back
    assert "at worst 12 fps" in back, back
    assert watch.dips == 1
    assert watch.seconds_below == pytest.approx(60, abs=1)


def test_a_short_dip_is_not_logged(caplog) -> None:
    """Eight seconds of sag is not what anyone needs to read about. A rolling
    average would have logged it; every second has to agree."""
    watch = capture_module.RateWatch(30.0, started_at=0.0)
    with caplog.at_level(logging.WARNING, logger="wer.video.capture"):
        t = feed(watch, 0.0, 20, 30)
        t = feed(watch, t, 8, 12)
        t = feed(watch, t, 20, 30)
        t = feed(watch, t, 2, 0.5)      # and a near-stall of two seconds
        feed(watch, t, 20, 30)
    assert rate_warnings(caplog) == []
    assert watch.dips == 0


def test_a_healthy_camera_logs_nothing_for_hours(caplog) -> None:
    """Including one running a shade under its nominal rate, as cameras do."""
    watch = capture_module.RateWatch(30.0, started_at=0.0)
    with caplog.at_level(logging.WARNING, logger="wer.video.capture"):
        t = feed(watch, 0.0, 3600, 30)
        feed(watch, t, 3600, 29.5)
    assert rate_warnings(caplog) == []


def test_a_dropout_is_not_reported_as_a_slow_rate(caplog) -> None:
    """No frames at all is a dropout, which three other things already report."""
    watch = capture_module.RateWatch(30.0, started_at=0.0)
    with caplog.at_level(logging.WARNING, logger="wer.video.capture"):
        t = feed(watch, 0.0, 20, 30)
        t = silence(watch, t, 30)
        feed(watch, t, 20, 30)
    assert rate_warnings(caplog) == []


def test_reads_that_fail_29_times_in_30_are_caught_as_a_slow_camera(
    monkeypatch, caplog
) -> None:
    """The failed-read alarm wants 30 in a row, and one good frame resets it. A
    device failing 29 reads and then delivering one, over and over, runs at
    about three frames a second and trips nothing: not that alarm, not the
    two-second live test, not the recorder's three-second stall check."""
    errors: list[str] = []
    camera, _, _ = scripted_camera(
        monkeypatch, (["fail"] * 29 + ["frame"]) * 200, on_error=errors.append
    )
    with caplog.at_level(logging.WARNING, logger="wer.video.capture"):
        camera._run()
    assert errors == [], "the failed-read alarm fired; this no longer covers the gap"
    lines = rate_warnings(caplog)
    assert any("delivering" in line and "30 requested" in line for line in lines), lines


def test_the_line_capture_stops_with_totals_the_dips(monkeypatch, caplog) -> None:
    """A dip still going at the end is counted, so the last line in the log
    about this camera says how much of the run was choppy."""
    camera, _, _ = scripted_camera(
        # 20 s at 30 fps, then 30 s at 12: a frame, then 50 ms of failed reads.
        monkeypatch, ["frame"] * 600 + (["fail"] * 5 + ["frame"]) * 360
    )
    with caplog.at_level(logging.INFO, logger="wer.video.capture"):
        camera._run()
    exiting = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Capture thread exiting")
    ]
    assert len(exiting) == 1, exiting
    assert "under 27 fps for 30 s in 1 dip(s), at worst 12 fps" in exiting[0], exiting[0]


def test_a_dip_that_wobbles_is_logged_once_and_so_is_its_recovery(caplog) -> None:
    """A camera at 12 fps that manages one good second in every five. Recovery
    wants ten good seconds in a row: taken on one, the log would say it fell
    and came back a dozen times over for what the recording shows as one long
    choppy stretch."""
    watch = capture_module.RateWatch(30.0, started_at=0.0)
    with caplog.at_level(logging.WARNING, logger="wer.video.capture"):
        t = feed(watch, 0.0, 20, 30)
        t = feed(watch, t, 20, 12)
        for _ in range(12):
            t = feed(watch, t, 4, 12)
            t = feed(watch, t, 1, 30)
        t = feed(watch, t, 10, 12)
        feed(watch, t, 30, 30)
    lines = rate_warnings(caplog)
    assert len(lines) == 2, lines
    assert watch.dips == 1


def test_slow_seconds_now_and_then_never_add_up_to_a_dip(caplog) -> None:
    """One slow second in thirty, for an hour. The ten have to be slow in a
    row; counted up across the good seconds between them, the log would report
    dips that never happened on a camera doing its job."""
    watch = capture_module.RateWatch(30.0, started_at=0.0)
    with caplog.at_level(logging.WARNING, logger="wer.video.capture"):
        t = 0.0
        for _ in range(120):
            t = feed(watch, t, 29, 30)
            t = feed(watch, t, 1, 20)
    assert rate_warnings(caplog) == []
    assert watch.dips == 0


def test_a_camera_open_already_found_slow_is_not_warned_about_again(
    monkeypatch, caplog
) -> None:
    """open() measures the delivered rate before capture starts, and warns when
    the best backend is under the floor: in poor light, or on a mode whose
    probed rate is more than it delivers. The rate watch then warned again ten
    seconds into every capture session, about a dip that began before it did.
    It is still counted, so the line capture stops with says how long."""
    camera, _, device = scripted_camera(
        # 12 fps for 30 s: a frame, then 50 ms of failed reads.
        monkeypatch, (["fail"] * 5 + ["frame"]) * 360
    )
    monkeypatch.setattr(camera, "_try_backend", lambda backend, name: device)
    monkeypatch.setattr(camera, "_measure_rate", lambda capture: 12.0)
    with caplog.at_level(logging.INFO, logger="wer.video.capture"):
        assert camera.open()
        camera._run()
    lines = rate_warnings(caplog)
    assert len(lines) == 1 and "Best available capture rate" in lines[0], lines
    exiting = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Capture thread exiting")
    ]
    assert len(exiting) == 1 and "in 1 dip(s)" in exiting[0], exiting


def test_a_camera_slow_at_open_that_came_up_is_warned_about_when_it_falls_again(
    caplog,
) -> None:
    """Quiet only about what open() already said. Once the camera has delivered
    a good second, a fall is news again, however the session started."""
    watch = capture_module.RateWatch(30.0, started_at=0.0, delivering_at_open=15.0)
    with caplog.at_level(logging.WARNING, logger="wer.video.capture"):
        t = feed(watch, 0.0, 30, 15)
        t = feed(watch, t, 30, 30)
        feed(watch, t, 30, 12)
    lines = rate_warnings(caplog)
    assert len(lines) == 2, lines
    back, fell = lines
    assert back.startswith("Camera back to"), back
    assert "against the 30 requested" in fell, fell
    assert watch.dips == 2


# ------------------------------------------------------- reopening in a take

BLACKMAGIC = "Blackmagic WDM Capture"
WEBCAM = "Integrated Camera"


class StandInCamera(CameraCapture):
    """A CameraCapture that opens nothing, and drops out when told to."""

    instances: list[StandInCamera] = []
    #: ("open", index) and ("stop", index), in order, across every instance.
    events: list[tuple[str, int]] = []
    #: Whether open() works.
    opens = True
    #: Whether open() raises, as cv2.error out of a native backend would.
    open_raises = False
    #: The size open() comes back delivering, whatever it asked for, when set.
    comes_back_at: tuple[int, int] | None = None

    def __init__(self, settings: CaptureSettings, *, on_error=None) -> None:
        super().__init__(settings, on_error=on_error)
        StandInCamera.instances.append(self)
        self.gone_for = 0.0
        self.lets_go = True
        self.running = False
        self.stopped = False

    def open(self) -> bool:
        StandInCamera.events.append(("open", self.settings.device_index))
        if StandInCamera.open_raises:
            raise cv2.error("stand-in for an error inside the backend")
        self._backend_name = "STAND-IN"
        width, height = StandInCamera.comes_back_at or (
            self.settings.width, self.settings.height
        )
        self._actual = (width, height, self.settings.fps)
        return StandInCamera.opens

    def start(self) -> None:
        self.running = True

    def stop(self, timeout: float = 3.0) -> bool:
        StandInCamera.events.append(("stop", self.settings.device_index))
        self.stopped = True
        if self.lets_go:
            self.running = False
        return self.lets_go

    @property
    def is_running(self) -> bool:
        return self.running

    @property
    def stalled_for(self) -> float:
        return self.gone_for


#: What SyncProbe finds on every device.
PROBED = [VideoFormat(1920, 1080, 30.0, 30.0, "UYVY")]


class SyncProbe(QObject):
    """FormatProbe without the thread, so nothing has to pump events."""

    finished = Signal(object)

    def probe(self, devices) -> None:
        self.finished.emit({d.index: list(PROBED) for d in devices})


class PanelClock:
    """perf_counter for wer.ui.preview under the test's control."""

    def __init__(self) -> None:
        self.t = 5000.0

    def perf_counter(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return time.monotonic()


@pytest.fixture
def rig(qt_app, monkeypatch):
    """A real PreviewPanel, live on a stand-in Blackmagic beside the webcam."""
    from PySide6.QtWidgets import QMessageBox

    from wer.ui import preview as preview_module

    clock = PanelClock()
    connected = [CaptureDevice(0, WEBCAM), CaptureDevice(1, BLACKMAGIC)]
    enumerations: list[int] = []

    def enumerate_now():
        enumerations.append(len(connected))
        return list(connected)

    dialogs: list[str] = []

    def no_dialog(parent, title, *args, **kwargs):
        dialogs.append(title)
        return QMessageBox.StandardButton.Ok

    for name in ("warning", "critical", "question", "information"):
        monkeypatch.setattr(QMessageBox, name, no_dialog)
    monkeypatch.setattr(preview_module, "time", clock)
    monkeypatch.setattr(preview_module, "enumerate_video_devices", enumerate_now)
    monkeypatch.setattr(preview_module, "FormatProbe", SyncProbe)
    monkeypatch.setattr(preview_module, "CameraCapture", StandInCamera)
    StandInCamera.instances, StandInCamera.events, StandInCamera.opens = [], [], True
    StandInCamera.open_raises, StandInCamera.comes_back_at = False, None

    panel = preview_module.PreviewPanel(preferred_device=BLACKMAGIC)
    try:
        camera = panel.capture
        assert camera is not None and camera.settings.device_index == 1
        yield SimpleNamespace(
            panel=panel, camera=camera, clock=clock, connected=connected,
            enumerations=enumerations, dialogs=dialogs, preview=preview_module,
        )
    finally:
        panel.stop_capture()
        panel.deleteLater()


def roll_take(rig) -> None:
    """What MainWindow does to the panel and the camera when a take starts."""
    rig.panel.set_recording(True)
    rig.camera.feed_encoder.set()


def test_a_camera_that_drops_out_of_a_take_is_opened_again_when_it_comes_back(
    rig,
) -> None:
    """The finding itself. Thunderbolt drops, the device goes, the device comes
    back -- under another DirectShow index, which the one it was opened by no
    longer names."""
    panel, first = rig.panel, rig.camera
    roll_take(rig)
    mark = len(StandInCamera.events)

    first.gone_for = rig.preview.REOPEN_AFTER + 1
    rig.connected[:] = [CaptureDevice(0, WEBCAM)]
    panel._check_camera_still_delivering()
    assert panel.capture is first, "let go of the old camera with nothing to open"
    assert StandInCamera.events[mark:] == []

    rig.clock.t += 60
    rig.connected[:] = [CaptureDevice(0, WEBCAM), CaptureDevice(2, BLACKMAGIC)]
    panel._check_camera_still_delivering()

    second = panel.capture
    assert second is not first and second.is_running, "the camera was not reopened"
    assert StandInCamera.events[mark:] == [("stop", 1), ("open", 2)], (
        "must let go of the old handle first, then open the camera by its name"
    )
    assert second.feed_encoder.is_set(), "reopened, but not feeding the take"
    assert panel._frame_timer.isActive(), (
        "reopened, but the preview and snapshots never see it"
    )
    assert panel._device_box.currentData() == CaptureDevice(2, BLACKMAGIC), (
        "the picker still holds the index from before the camera came back"
    )
    assert panel._formats_by_index.get(2) == PROBED, (
        "the formats probed for the camera were dropped when it was renumbered"
    )
    assert panel._formats_by_index.get(0) == PROBED


def test_the_panel_checks_on_its_own_timer(rig, qt_app) -> None:
    """Nothing else calls the check, so prove the panel runs it by itself."""
    roll_take(rig)
    rig.camera.gone_for = rig.preview.REOPEN_AFTER + 1
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and rig.panel.capture is rig.camera:
        qt_app.processEvents()
        time.sleep(0.02)
    assert rig.panel.capture is not rig.camera, "no reopen within three seconds"


def test_a_healthy_camera_is_never_reopened(rig) -> None:
    """An hour of checks on a camera that delivers -- now and then after a
    short run of failed reads -- opens nothing and enumerates nothing."""
    roll_take(rig)
    mark, enumerated = len(StandInCamera.events), len(rig.enumerations)
    for second in range(3600):
        rig.clock.t += 1
        rig.camera.gone_for = (
            rig.preview.REOPEN_AFTER - 0.5 if second % 7 == 0 else 0.0
        )
        rig.panel._check_camera_still_delivering()
    assert rig.panel.capture is rig.camera
    assert StandInCamera.events[mark:] == []
    assert len(rig.enumerations) == enumerated


def test_outside_a_take_a_lost_camera_is_left_to_the_operator(rig) -> None:
    """Refresh works outside a take, and a camera another application has just
    taken is theirs: Wer must not keep snatching at it."""
    mark, enumerated = len(StandInCamera.events), len(rig.enumerations)
    rig.camera.gone_for = 600.0
    rig.clock.t += 600
    rig.panel._check_camera_still_delivering()
    assert rig.panel.capture is rig.camera
    assert StandInCamera.events[mark:] == []
    assert len(rig.enumerations) == enumerated


def test_the_device_is_not_opened_again_until_the_old_capture_has_let_go(rig) -> None:
    """A read that has not returned still holds the device, and opening it
    again underneath is two handles on one camera."""
    roll_take(rig)
    mark = len(StandInCamera.events)
    rig.camera.gone_for = rig.preview.REOPEN_AFTER + 1
    rig.camera.lets_go = False
    rig.panel._check_camera_still_delivering()
    assert StandInCamera.events[mark:] == [("stop", 1)]
    assert rig.panel.capture is rig.camera

    rig.camera.lets_go = True
    rig.clock.t += rig.preview.REOPEN_FIRST_GAP
    rig.panel._check_camera_still_delivering()
    assert StandInCamera.events[mark:] == [("stop", 1), ("stop", 1), ("open", 1)]
    assert rig.panel.capture is not rig.camera


def test_a_reopen_that_fails_is_tried_again_less_often_and_never_with_a_dialog(
    rig,
) -> None:
    """Every attempt runs on the interface thread, and an open that fails can
    take seconds, so attempts back off. None of them may raise a modal: in an
    unattended take a dialog is one more thing nobody answers."""
    panel = rig.panel
    roll_take(rig)
    rig.camera.gone_for = rig.preview.REOPEN_AFTER + 1
    StandInCamera.opens = False
    mark = len(StandInCamera.events)

    start = rig.clock.t
    attempts: list[int] = []
    for second in range(180):
        rig.clock.t = start + second
        before = StandInCamera.events[mark:].count(("open", 1))
        panel._check_camera_still_delivering()
        if StandInCamera.events[mark:].count(("open", 1)) > before:
            attempts.append(second)

    assert attempts[:5] == [0, 5, 15, 35, 65], attempts
    assert all(b - a == 30 for a, b in zip(attempts[3:], attempts[4:])), attempts
    assert rig.dialogs == [], f"a reopen raised a dialog: {rig.dialogs}"
    panel._refresh_stats()
    assert "CAMERA LOST" in panel._stats.text(), panel._stats.text()

    StandInCamera.opens = True
    rig.clock.t = start + attempts[-1] + 30
    panel._check_camera_still_delivering()
    assert panel.capture is not None and panel.capture.is_running
    assert panel.capture.feed_encoder.is_set()


def test_a_camera_that_fails_again_straight_after_a_reopen_is_not_cycled(rig) -> None:
    """A device that opens and then fails within seconds, every time. Each
    reopen holds the interface for seconds, so trying again the moment it drops
    would cycle the device for the rest of the take and never let it settle."""
    roll_take(rig)
    start = rig.clock.t
    opened_at: list[int] = []
    for second in range(240):
        rig.clock.t = start + second
        for camera in StandInCamera.instances:
            camera.gone_for = rig.preview.REOPEN_AFTER + 1
        before = len(StandInCamera.events)
        rig.panel._check_camera_still_delivering()
        if ("open", 1) in StandInCamera.events[before:]:
            opened_at.append(second)

    assert opened_at[0] == 0, opened_at
    assert all(
        b - a > rig.preview.REOPEN_FIRST_GAP for a, b in zip(opened_at, opened_at[1:])
    ), f"cycled the device: opened at {opened_at}"
    assert len(opened_at) <= 10, opened_at


def test_a_camera_that_comes_back_by_itself_is_left_alone(rig) -> None:
    """If the old handle delivers again before an attempt reaches it there is
    nothing to fix, and a reopen would only add seconds to the gap."""
    roll_take(rig)
    mark = len(StandInCamera.events)
    rig.camera.gone_for = rig.preview.REOPEN_AFTER + 1
    rig.connected[:] = [CaptureDevice(0, WEBCAM)]
    rig.panel._check_camera_still_delivering()      # looks, and finds nothing

    rig.camera.gone_for = 0.0
    enumerated = len(rig.enumerations)
    for _ in range(120):
        rig.clock.t += 1
        rig.panel._check_camera_still_delivering()
    assert rig.panel.capture is rig.camera
    assert StandInCamera.events[mark:] == []
    assert len(rig.enumerations) == enumerated, "went on looking for a camera that was back"


@pytest.mark.parametrize(
    ("known", "connected"),
    [
        pytest.param(
            [CaptureDevice(0, BLACKMAGIC), CaptureDevice(1, BLACKMAGIC)],
            [CaptureDevice(0, BLACKMAGIC)],
            id="two-when-it-was-lost",
        ),
        pytest.param(
            [CaptureDevice(0, WEBCAM), CaptureDevice(1, BLACKMAGIC)],
            [
                CaptureDevice(0, WEBCAM),
                CaptureDevice(1, BLACKMAGIC),
                CaptureDevice(2, BLACKMAGIC),
            ],
            id="two-when-it-is-looked-for",
        ),
    ],
)
def test_two_cameras_with_one_name_are_not_guessed_between(
    rig, caplog, known, connected
) -> None:
    """Two of the same capture device share a name, and DirectShow may number
    them differently when one returns. Reopening "the one called that" could
    record the wrong camera for the rest of the take, which looks fine and is
    not. So it says it cannot tell, and does not guess -- whether the second
    one was there when the camera was lost, or was plugged in while it was
    being looked for."""
    roll_take(rig)
    rig.panel._devices = list(known)
    mark = len(StandInCamera.events)
    rig.camera.gone_for = rig.preview.REOPEN_AFTER + 1
    rig.connected[:] = connected
    with caplog.at_level(logging.ERROR, logger="wer.ui.preview"):
        rig.panel._check_camera_still_delivering()
    assert StandInCamera.events[mark:] == []
    assert rig.panel.capture is rig.camera
    messages = [record.getMessage() for record in caplog.records]
    assert any("more than one" in message for message in messages), messages


def test_a_take_that_ends_with_its_camera_lost_says_so_and_stops_looking(
    rig, caplog
) -> None:
    """After the take the operator is back and Refresh works again, so the
    reopen is theirs to do -- but they have to be told it is needed. And the
    loss has to end with the take: a stats line still reading CAMERA LOST,
    reopening, after the take or into the next one, is a line nobody believes
    the next time it is true."""
    roll_take(rig)
    rig.camera.gone_for = rig.preview.REOPEN_AFTER + 1
    rig.connected[:] = [CaptureDevice(0, WEBCAM)]
    rig.panel._check_camera_still_delivering()
    with caplog.at_level(logging.ERROR, logger="wer.ui.preview"):
        rig.panel.set_recording(False)
    messages = [record.getMessage() for record in caplog.records]
    assert any("Refresh" in message for message in messages), messages
    rig.panel._refresh_stats()
    assert "CAMERA LOST" not in rig.panel._stats.text(), rig.panel._stats.text()

    # The next take rolls on a camera that delivers.
    rig.camera.gone_for = 0.0
    roll_take(rig)
    rig.panel._refresh_stats()
    assert "CAMERA LOST" not in rig.panel._stats.text(), rig.panel._stats.text()
    mark, enumerated = len(StandInCamera.events), len(rig.enumerations)
    for _ in range(60):
        rig.clock.t += 1
        rig.panel._check_camera_still_delivering()
    assert StandInCamera.events[mark:] == []
    assert len(rig.enumerations) == enumerated


def test_a_capture_whose_thread_ends_while_a_reopen_waits_is_not_taken_for_recovered(
    rig, caplog
) -> None:
    """An attempt finds the old capture stuck in a read and leaves it be. Then
    the read comes back with a frame and the thread ends on it, with
    stalled_for at 0.0. That capture has not recovered; it has nothing left to
    deliver with. Taken for recovered, the reopen stopped looking, and the rest
    of the take had no video under a log line saying the camera had come back
    by itself."""
    panel, first = rig.panel, rig.camera
    roll_take(rig)
    mark = len(StandInCamera.events)
    first.gone_for = rig.preview.REOPEN_AFTER + 1
    first.lets_go = False
    panel._check_camera_still_delivering()
    assert StandInCamera.events[mark:] == [("stop", 1)]

    first.running, first.gone_for, first.lets_go = False, 0.0, True
    start = rig.clock.t
    with caplog.at_level(logging.WARNING, logger="wer.ui.preview"):
        for second in range(1, int(rig.preview.REOPEN_FIRST_GAP)):
            rig.clock.t = start + second
            panel._check_camera_still_delivering()
            assert panel._lost is not None, f"taken for recovered {second} s on"
        rig.clock.t = start + rig.preview.REOPEN_FIRST_GAP
        panel._check_camera_still_delivering()
    messages = [record.getMessage() for record in caplog.records]
    assert not any("by itself" in message for message in messages), messages
    assert StandInCamera.events[mark:] == [("stop", 1), ("stop", 1), ("open", 1)]
    assert panel.capture is not first and panel.capture.is_running
    assert panel.capture.feed_encoder.is_set()


def test_a_capture_with_no_thread_left_is_lost_whatever_it_reports(rig) -> None:
    """stalled_for is the capture's own account, and it has been wrong about a
    thread that had gone. A capture that is not running cannot deliver, so a
    take does not wait on that account before reopening it."""
    roll_take(rig)
    mark = len(StandInCamera.events)
    rig.camera.running, rig.camera.gone_for = False, 0.0
    rig.panel._check_camera_still_delivering()
    assert StandInCamera.events[mark:] == [("stop", 1), ("open", 1)]
    assert rig.panel.capture is not rig.camera and rig.panel.capture.is_running
    assert rig.panel.capture.feed_encoder.is_set()


def test_an_open_that_raises_is_tried_again_on_the_schedule_of_one_that_fails(
    rig, caplog
) -> None:
    """open() calls into OpenCV's native backends, which report trouble by
    raising cv2.error. Raised out of the timer slot, it went past the backoff:
    every one-second tick enumerated, opened on the interface thread and logged
    a crash traceback, for the rest of the take."""
    panel = rig.panel
    roll_take(rig)
    rig.camera.gone_for = rig.preview.REOPEN_AFTER + 1
    StandInCamera.open_raises = True
    mark = len(StandInCamera.events)

    start = rig.clock.t
    attempts: list[int] = []
    with caplog.at_level(logging.ERROR, logger="wer.ui.preview"):
        for second in range(180):
            rig.clock.t = start + second
            before = StandInCamera.events[mark:].count(("open", 1))
            panel._check_camera_still_delivering()
            if StandInCamera.events[mark:].count(("open", 1)) > before:
                attempts.append(second)

    assert attempts[:5] == [0, 5, 15, 35, 65], attempts
    assert all(b - a == 30 for a, b in zip(attempts[3:], attempts[4:])), attempts
    half_built = StandInCamera.instances[1:]
    assert len(half_built) == len(attempts)
    assert all(camera.stopped for camera in half_built), "kept a camera whose open raised"
    tracebacks = [
        record
        for record in caplog.records
        if record.exc_info and BLACKMAGIC in record.getMessage()
    ]
    assert len(tracebacks) == len(attempts), [r.getMessage() for r in tracebacks]
    panel._refresh_stats()
    assert "CAMERA LOST" in panel._stats.text(), panel._stats.text()

    StandInCamera.open_raises = False
    rig.clock.t = start + attempts[-1] + 30
    panel._check_camera_still_delivering()
    assert panel.capture is not None and panel.capture.is_running
    assert panel.capture.feed_encoder.is_set()


def test_a_camera_that_comes_back_at_another_size_is_not_fed_into_the_take(
    rig, caplog
) -> None:
    """The take's ffmpeg reads raw frames of the size the take started at, and
    nothing checks a frame against it. A camera that came back in another mode
    -- after a replug, or on the other backend -- was armed all the same, and
    every frame after the reopen sheared across the pipe for the rest of the
    take, under a log line saying the camera had been reopened."""
    panel, first = rig.panel, rig.camera
    roll_take(rig)
    assert first.actual_resolution == (1920, 1080)
    first.gone_for = rig.preview.REOPEN_AFTER + 1
    StandInCamera.comes_back_at = (1280, 720)
    with caplog.at_level(logging.ERROR, logger="wer.ui.preview"):
        panel._check_camera_still_delivering()

    wrong = StandInCamera.instances[-1]
    assert wrong is not first and wrong.actual_resolution == (1280, 720)
    assert panel.capture is not wrong, "armed a camera the take cannot carry"
    assert not wrong.is_running and not wrong.feed_encoder.is_set()
    assert wrong.stopped, "kept hold of the camera it refused"
    messages = [record.getMessage() for record in caplog.records]
    assert any("1280x720" in m and "1920x1080" in m for m in messages), messages
    panel._refresh_stats()
    assert "CAMERA LOST" in panel._stats.text(), panel._stats.text()

    StandInCamera.comes_back_at = None
    rig.clock.t += rig.preview.REOPEN_FIRST_GAP
    panel._check_camera_still_delivering()
    assert panel.capture is not None and panel.capture.is_running
    assert panel.capture.actual_resolution == (1920, 1080)
    assert panel.capture.feed_encoder.is_set()


def test_a_reopen_asks_for_the_size_the_take_is_being_written_at(rig) -> None:
    """Cameras accept a mode and deliver another, and a take is written at what
    was delivered. Asked again for the mode picked in the Format box, a camera
    that honoured it this time would come back at a size the take cannot
    carry, and be refused at every attempt for the rest of the take."""
    first = rig.camera
    first._actual = (1280, 720, 30.0)   # asked for 1920x1080, delivered this
    roll_take(rig)
    first.gone_for = rig.preview.REOPEN_AFTER + 1
    rig.panel._check_camera_still_delivering()
    second = rig.panel.capture
    assert second is not None and second is not first and second.is_running
    assert (second.settings.width, second.settings.height) == (1280, 720)
    assert second.settings.fourcc == first.settings.fourcc


def test_a_flip_ticked_while_the_camera_is_lost_is_on_the_camera_that_comes_back(
    rig,
) -> None:
    """The flips are not locked during a take, so one can be ticked while a
    lost camera is being looked for. Reopened on the settings it had when it
    was lost, the camera would come back the other way up under a box still
    showing ticked."""
    panel, first = rig.panel, rig.camera
    roll_take(rig)
    first.gone_for = rig.preview.REOPEN_AFTER + 1
    rig.connected[:] = [CaptureDevice(0, WEBCAM)]
    panel._check_camera_still_delivering()
    assert panel._lost is not None and panel.capture is first

    panel._mirror_box.setChecked(True)
    rig.clock.t += 60
    rig.connected[:] = [CaptureDevice(0, WEBCAM), CaptureDevice(1, BLACKMAGIC)]
    panel._check_camera_still_delivering()

    second = panel.capture
    assert second is not first and second.is_running, "the camera was not reopened"
    assert second.settings.flip_horizontal, "came back unmirrored under a ticked box"
    assert not second.settings.flip_vertical


def test_a_reopen_waits_for_a_format_probe_still_running(rig) -> None:
    """ffmpeg listing formats touches the same devices. A probe that Refresh
    started just before Record can still be running when the camera drops,
    and a camera is not opened alongside it."""
    roll_take(rig)
    mark, enumerated = len(StandInCamera.events), len(rig.enumerations)
    rig.panel._probing = True
    rig.camera.gone_for = rig.preview.REOPEN_AFTER + 1
    for _ in range(10):
        rig.clock.t += 1
        rig.panel._check_camera_still_delivering()
    assert StandInCamera.events[mark:] == []
    assert len(rig.enumerations) == enumerated

    rig.panel._probe_finished(rig.panel._formats_by_index)
    rig.panel._check_camera_still_delivering()
    assert StandInCamera.events[mark:] == [("stop", 1), ("open", 1)]
    assert rig.panel.capture is not rig.camera


def test_a_take_that_ends_with_its_camera_still_lost_keeps_the_rate_it_was_recorded_at(
    rig,
) -> None:
    """The main window reads the take's frame rate from preview.capture when
    the take ends, and numbers every marker in the markers file by it. An
    attempt that had let go of the old camera and could not open it again left
    None there, so the main window fell back to 30, and a take at any other
    rate got a markers file at the wrong rate with nothing said."""
    panel, first = rig.panel, rig.camera
    first.settings = replace(first.settings, fps=25.0)
    roll_take(rig)
    first.gone_for = rig.preview.REOPEN_AFTER + 1
    StandInCamera.opens = False
    mark = len(StandInCamera.events)
    panel._check_camera_still_delivering()
    assert StandInCamera.events[mark:] == [("stop", 1), ("open", 1)]
    panel.set_recording(False)

    assert panel.capture is not None, "forgot the take's camera before the take ended"
    assert panel.capture.settings.fps == 25.0
    assert not panel.capture.is_running
    assert not panel._record_button.isEnabled(), "offers to record with no camera"
    assert not panel._snapshot_button.isEnabled(), "offers a snapshot with no camera"


def test_a_tick_that_arrives_inside_an_attempt_does_not_start_another(
    rig, monkeypatch
) -> None:
    """An attempt enumerates through COM and opens through DirectShow on the
    interface thread, and either may pump that thread's messages while it
    waits. A dropout tick delivered in there would start a second attempt on
    the same device halfway through the first: the camera opened twice, and
    the outer attempt stopping the camera the inner one had just armed."""
    panel = rig.panel
    roll_take(rig)
    rig.camera.gone_for = rig.preview.REOPEN_AFTER + 1
    enumerate_now = rig.preview.enumerate_video_devices
    pumped: list[int] = []

    def enumerate_and_pump():
        if not pumped:
            pumped.append(len(StandInCamera.events))
            panel._check_camera_still_delivering()
        return enumerate_now()

    monkeypatch.setattr(rig.preview, "enumerate_video_devices", enumerate_and_pump)
    mark = len(StandInCamera.events)
    panel._check_camera_still_delivering()
    assert pumped, "the stand-in never delivered a tick inside the attempt"
    assert StandInCamera.events[mark:] == [("stop", 1), ("open", 1)]
    assert panel.capture is not rig.camera and panel.capture.is_running
    assert panel.capture.feed_encoder.is_set()
