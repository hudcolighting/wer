"""Wer keeps the camera backend that delivers the rate, not the first one that opens.

Both Windows backends open this laptop's camera, both report 1920x1080, and
both hand over frames. One of them does it at half the rate in poor light.
Measured back to back in the same room, alternating so the light could not be
confounded with the backend: DirectShow 30.10 and 30.08 fps against Media
Foundation's 15.59 and 22.26, at identical resolution and identical mean
brightness. Media Foundation spent 97.7% and 55.8% of its seconds under 29 fps.

Wer tried Media Foundation first and stopped as soon as a frame arrived, so it
had been using the worse one for every recording.

These tests use a fake VideoCapture: the point is the selection logic, and a
test that needs a real camera in a dark room is a test nobody runs.
"""

from __future__ import annotations

import pytest

from wer.video import capture as capture_module
from wer.video.capture import CameraCapture, CaptureSettings


class FakeCapture:
    """Enough of cv2.VideoCapture to be chosen or rejected."""

    def __init__(self, fps: float, *, opens: bool = True, width: int = 1920,
                 height: int = 1080) -> None:
        self.fps = fps
        self._opens = opens
        self.width, self.height = width, height
        self.released = False
        self.reads = 0

    def isOpened(self) -> bool:
        return self._opens

    def set(self, prop, value) -> bool:
        return True

    def get(self, prop) -> float:
        return self.fps

    def read(self):
        self.reads += 1
        if not self._opens:
            return False, None
        import numpy as np
        return True, np.zeros((self.height, self.width, 3), dtype=np.uint8)

    def release(self) -> None:
        self.released = True


@pytest.fixture
def fake_backends(monkeypatch):
    """Install a fake camera per backend, and make timing deterministic.

    Rate is measured from a clock, so the clock advances by exactly one frame
    period per read of whichever capture is in front of it.
    """
    import cv2

    created: dict[int, FakeCapture] = {}
    plan: dict[int, FakeCapture] = {}
    now = {"t": 0.0}
    current: dict[str, FakeCapture | None] = {"cap": None}

    def fake_video_capture(index, backend):
        cap = plan[backend]
        created[backend] = cap
        real_read = cap.read

        def timed_read():
            current["cap"] = cap
            now["t"] += 1.0 / cap.fps if cap.fps else 1.0
            return real_read()

        cap.read = timed_read
        return cap

    monkeypatch.setattr(cv2, "VideoCapture", fake_video_capture)
    monkeypatch.setattr(capture_module.time, "perf_counter", lambda: now["t"])
    return plan, created


DSHOW = 700    # cv2.CAP_DSHOW
MSMF = 1400    # cv2.CAP_MSMF


def test_the_faster_backend_wins_even_though_both_work(fake_backends) -> None:
    """The real case: both open, both deliver frames, one is half the rate."""
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(30.0)
    plan[MSMF] = FakeCapture(15.0)

    camera = CameraCapture(CaptureSettings())
    assert camera.open()
    assert camera.backend == "DSHOW"


def test_a_good_first_backend_stops_the_search(fake_backends) -> None:
    """Opening a camera is slow -- DirectShow took four seconds on the machine
    this was written on. Probing the second one for nothing doubles that."""
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(30.0)
    plan[MSMF] = FakeCapture(30.0)

    camera = CameraCapture(CaptureSettings())
    assert camera.open()
    assert MSMF not in created, "probed the second backend after the first was fine"


def test_the_order_is_not_the_safeguard(fake_backends) -> None:
    """A machine where this comes out the other way must correct itself,
    without anyone editing the backend list."""
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(12.0)
    plan[MSMF] = FakeCapture(30.0)

    camera = CameraCapture(CaptureSettings())
    assert camera.open()
    assert camera.backend == "MSMF"


def test_the_loser_is_released(fake_backends) -> None:
    """Two open handles on one camera is how you get a device nothing can use
    until the process dies."""
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(12.0)
    plan[MSMF] = FakeCapture(30.0)

    CameraCapture(CaptureSettings()).open()
    assert created[DSHOW].released
    assert not created[MSMF].released


def test_media_foundation_stays_an_option(fake_backends, monkeypatch) -> None:
    """Both backends are kept, and either can carry a camera on its own.

    The list used to be written out inside open(), which meant the fallback
    could not be pointed at from outside and so could never be shown to work
    by itself. Measured through Wer on the Integrated Camera with the list
    pointed at Media Foundation alone: opened 1920x1080, 30.6 fps measured,
    30.0 fps delivered.
    """
    assert [backend for backend, _ in capture_module.CAPTURE_BACKENDS] == [DSHOW, MSMF]

    plan, created = fake_backends
    plan[MSMF] = FakeCapture(30.0)
    monkeypatch.setattr(capture_module, "CAPTURE_BACKENDS", ((MSMF, "MSMF"),))

    camera = CameraCapture(CaptureSettings())
    assert camera.open()
    assert camera.backend == "MSMF"
    assert DSHOW not in created, "DirectShow was opened when the list left it out"


def test_a_backend_that_will_not_open_is_skipped(fake_backends) -> None:
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(30.0, opens=False)
    plan[MSMF] = FakeCapture(30.0)

    camera = CameraCapture(CaptureSettings())
    assert camera.open()
    assert camera.backend == "MSMF"


def test_two_slow_backends_still_give_a_working_camera(fake_backends, caplog) -> None:
    """A dim room is not a failure. The recording still comes out the right
    length, because the encoder holds constant rate from arrival times -- it is
    just choppier, and the user is the only one who can add light."""
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(12.0)
    plan[MSMF] = FakeCapture(14.0)

    camera = CameraCapture(CaptureSettings())
    with caplog.at_level("WARNING"):
        assert camera.open()
    assert camera.backend == "MSMF", "should still keep the better of the two"
    assert any(
        "against the 30 requested" in record.getMessage()
        for record in caplog.records
    ), "said nothing about a half-rate camera"


def test_no_camera_at_all_fails_with_an_explanation(fake_backends) -> None:
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(30.0, opens=False)
    plan[MSMF] = FakeCapture(30.0, opens=False)

    errors = []
    camera = CameraCapture(CaptureSettings(), on_error=errors.append)
    assert not camera.open()
    assert errors and "DirectShow" in errors[0]


# ------------------------------------------------------------ the format asked for


class FormatCapture(FakeCapture):
    """A fake that records the properties set on it and reports a format."""

    def __init__(self, fps: float, *, reports: str = "MJPG") -> None:
        super().__init__(fps)
        self.sets: list[int] = []
        self.reports = reports

    def set(self, prop, value) -> bool:
        self.sets.append(prop)
        return True

    def get(self, prop) -> float:
        import cv2

        if prop == cv2.CAP_PROP_FOURCC:
            return float(cv2.VideoWriter_fourcc(*self.reports))
        return self.fps


def test_the_format_is_asked_for_after_the_rate(fake_backends) -> None:
    """OpenCV's DirectShow backend rebuilds the capture graph when the size or
    rate is set and can drop the format. A USB camera asked for MJPG before the
    size came up as YUY2 at 5.0 fps; asked after the rate, MJPG at 29.9. So the
    format must be the LAST thing set.

    It was also asked for first, until measurement showed that set was pure
    cost: each one rebuilds the graph, and on DirectShow that is seconds. Four
    alternating trials per camera gave identical formats and rates either way,
    and 1.4 s back on the USB camera. This pins what actually matters -- the
    format lands after the rate -- rather than the number of times it is sent.
    """
    import cv2

    plan, created = fake_backends
    plan[DSHOW] = FormatCapture(30.0)
    camera = CameraCapture(CaptureSettings(fourcc="MJPG"))
    assert camera.open()

    sets = created[DSHOW].sets
    assert sets[-1] == cv2.CAP_PROP_FOURCC, "the format was not asked for after the rate"
    assert sets.index(cv2.CAP_PROP_FPS) < len(sets) - 1
    assert sets.count(cv2.CAP_PROP_FOURCC) == 1, (
        "the format is sent once; a second send costs seconds on DirectShow"
    )


def test_a_camera_without_a_format_to_ask_for_is_not_given_one(fake_backends) -> None:
    import cv2

    plan, created = fake_backends
    plan[DSHOW] = FormatCapture(30.0)
    assert CameraCapture(CaptureSettings(fourcc=None)).open()
    assert cv2.CAP_PROP_FOURCC not in created[DSHOW].sets


def test_a_slow_camera_left_on_another_format_is_blamed_on_the_format(
    fake_backends, caplog
) -> None:
    """The warning used to blame the light, which sent the operator looking
    for a lamp when the camera was on its slow uncompressed format."""
    plan, created = fake_backends
    plan[DSHOW] = FormatCapture(5.0, reports="YUY2")
    plan[MSMF] = FormatCapture(4.0, reports="YUY2")

    with caplog.at_level("WARNING"):
        assert CameraCapture(CaptureSettings(fourcc="MJPG")).open()
    said = " ".join(record.getMessage() for record in caplog.records)
    assert "sending YUY2 rather than the MJPG asked for" in said, said
    assert "poor light" not in said, said


def test_a_slow_camera_on_the_format_it_was_asked_for_still_points_at_the_light(
    fake_backends, caplog
) -> None:
    plan, created = fake_backends
    plan[DSHOW] = FormatCapture(12.0, reports="MJPG")
    plan[MSMF] = FormatCapture(11.0, reports="MJPG")

    with caplog.at_level("WARNING"):
        assert CameraCapture(CaptureSettings(fourcc="MJPG")).open()
    said = " ".join(record.getMessage() for record in caplog.records)
    assert "poor light" in said, said
    assert "rather than" not in said, said


def test_the_opened_line_names_the_format_the_camera_really_sent(
    fake_backends, caplog
) -> None:
    plan, created = fake_backends
    plan[DSHOW] = FormatCapture(30.0, reports="NV12")

    with caplog.at_level("INFO", logger="wer.video.capture"):
        assert CameraCapture(CaptureSettings(fourcc="MJPG")).open()
    opened = [r.getMessage() for r in caplog.records if " opened device " in r.getMessage()]
    assert opened and opened[0].endswith("NV12"), opened


# --------------------------------------------------------- backend memory


@pytest.fixture()
def backend_memory(tmp_path, monkeypatch):
    """Point the remembered-backend store at a scratch file.

    Never at the real one: a test run must not rewrite the preference of
    whoever is running it, which the settings store has done once for real.
    """
    import wer.video.capture as capture_module

    monkeypatch.setattr(capture_module, "_memory_path", lambda: tmp_path / "backends.json")
    return tmp_path / "backends.json"


def test_the_backend_that_delivered_is_remembered(fake_backends, backend_memory) -> None:
    """So the next launch does not pay to discover it again.

    Measured on the USB camera: 11.7 s to reject DirectShow and reach Media
    Foundation the first time, 1.3 s when the answer is already known.
    """
    from wer.video.capture import remembered_backend

    plan, _ = fake_backends
    plan[DSHOW] = FakeCapture(30.0)
    assert CameraCapture(CaptureSettings(device_name="Stage Left")).open()

    assert remembered_backend("Stage Left") == DSHOW


def test_the_remembered_backend_is_tried_first(fake_backends, backend_memory) -> None:
    """Order only. DirectShow is first by default; a camera remembered as
    Media Foundation must not pay for DirectShow again."""
    from wer.video.capture import remember_backend

    remember_backend("Stage Left", MSMF)
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(30.0)
    plan[MSMF] = FakeCapture(30.0)

    assert CameraCapture(CaptureSettings(device_name="Stage Left")).open()
    assert DSHOW not in created, "DirectShow was opened despite the preference"


def test_a_poor_backend_is_not_remembered(fake_backends, backend_memory) -> None:
    """Recording a backend that limped would make the next launch try the
    loser first and pay for both, which is the opposite of the point."""
    from wer.video.capture import remembered_backend

    plan, _ = fake_backends
    plan[DSHOW] = FakeCapture(4.0)
    plan[MSMF] = FakeCapture(4.0)
    assert CameraCapture(CaptureSettings(device_name="Stage Left", fps=30.0)).open()

    assert remembered_backend("Stage Left") is None


def test_the_preference_never_overrides_the_measurement(fake_backends, backend_memory) -> None:
    """A remembered backend that has since become poor is overtaken.

    The preference changes which is tried first, never which is kept. A camera
    whose driver was updated, or which was moved to another port, must correct
    itself without anyone clearing a file.
    """
    from wer.video.capture import remember_backend, remembered_backend

    remember_backend("Stage Left", DSHOW)
    plan, _ = fake_backends
    plan[DSHOW] = FakeCapture(5.0)
    plan[MSMF] = FakeCapture(30.0)

    camera = CameraCapture(CaptureSettings(device_name="Stage Left", fps=30.0))
    assert camera.open()
    assert camera.backend == "MSMF", "the slow remembered backend was kept"
    assert remembered_backend("Stage Left") == MSMF, "the preference did not move"


def test_a_camera_with_no_name_still_opens(fake_backends, backend_memory) -> None:
    """The name is only for the preference. Capture must not depend on it."""
    plan, _ = fake_backends
    plan[DSHOW] = FakeCapture(30.0)
    assert CameraCapture(CaptureSettings()).open()
    assert not backend_memory.exists(), "an unnamed camera wrote a preference"


def test_an_unreadable_preference_is_ignored_rather_than_fatal(
    fake_backends, backend_memory
) -> None:
    """A camera that will not open because its preference file is corrupt
    would be a poor trade for an optimisation."""
    backend_memory.write_text("{ this is not json", encoding="utf-8")
    plan, _ = fake_backends
    plan[DSHOW] = FakeCapture(30.0)
    assert CameraCapture(CaptureSettings(device_name="Stage Left")).open()


# ------------------------------------- the index belongs to one backend only


def test_a_second_camera_rules_out_media_foundation(fake_backends, backend_memory) -> None:
    """The bug this prevents, found on a real rig on 12 Sep 2026.

    A device index is a DirectShow index -- the names come from pygrabber,
    which is DirectShow. Media Foundation numbers devices its own way. With an
    SDI source on a Blackmagic at DirectShow index 2, DirectShow could not open
    2048x1080@59.94, the code fell through to Media Foundation with the SAME
    index, and Media Foundation opened the USB webcam. The interface went on
    naming the Blackmagic over a picture from a different camera.

    Opening nothing is the right outcome there. Opening something else is not.
    """
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(30.0, opens=False)   # refuses, as DirectShow did
    plan[MSMF] = FakeCapture(30.0)                 # would open the wrong camera

    camera = CameraCapture(
        CaptureSettings(device_index=2, device_name="Blackmagic", device_count=3)
    )
    assert not camera.open(), "it opened a camera the index does not name"
    assert MSMF not in created, "Media Foundation was tried with a DirectShow index"


def test_one_camera_still_gets_both_backends(fake_backends, backend_memory) -> None:
    """With a single device the index cannot be ambiguous, so the measurement
    decides as before. This is what keeps a webcam that Media Foundation drives
    properly from being held to DirectShow's worse rate."""
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(10.0)
    plan[MSMF] = FakeCapture(30.0)

    camera = CameraCapture(
        CaptureSettings(device_index=0, device_name="USB Color Camera",
                        device_count=1, fps=30.0)
    )
    assert camera.open()
    assert camera.backend == "MSMF", "the faster backend was not kept"


def test_the_remembered_backend_is_ignored_when_it_could_mean_another_camera(
    fake_backends, backend_memory
) -> None:
    """A preference is stored by name but applied by index, so it is unsafe for
    exactly the same reason the fallback is."""
    from wer.video.capture import remember_backend

    remember_backend("Blackmagic", MSMF)
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(30.0)
    plan[MSMF] = FakeCapture(30.0)

    camera = CameraCapture(
        CaptureSettings(device_index=2, device_name="Blackmagic", device_count=3)
    )
    assert camera.open()
    assert camera.backend == "DSHOW"
    assert MSMF not in created, "a remembered backend reintroduced the ambiguity"


# ------------------------------------------------- slow sources and the rate


def test_a_one_fps_source_is_measured_in_seconds_not_half_a_minute(fake_backends) -> None:
    """The freeze. A capture device with no input signal sends about one frame a
    second, and the old fixed count of 5 + 20 frames held the interface thread
    for some 25 seconds -- 28.7 s measured on the Blackmagic on 12 Sep 2026."""
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(1.0)

    camera = CameraCapture(CaptureSettings(device_index=2, device_count=3))
    before = capture_module.time.perf_counter()
    assert camera.open()
    spent = capture_module.time.perf_counter() - before

    assert spent <= 4.0, f"opening a 1 fps source took {spent:.0f} s of clock"
    assert created[DSHOW].reads <= 4, f"{created[DSHOW].reads} reads at 1 fps"


def test_a_healthy_camera_is_measured_exactly_as_before(fake_backends) -> None:
    """At 30 fps the frame caps come first: one probe, 5 settle, 20 timed."""
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(30.0)

    assert CameraCapture(CaptureSettings(device_index=2, device_count=3)).open()
    assert created[DSHOW].reads == 26


def test_one_frame_a_second_points_at_the_signal_not_the_light(fake_backends, caplog) -> None:
    """Blaming the light at 1 fps sent the reader looking for a lamp while the
    HDMI source had gone."""
    plan, created = fake_backends
    plan[DSHOW] = FakeCapture(1.0)

    with caplog.at_level("WARNING"):
        assert CameraCapture(CaptureSettings(device_index=2, device_count=3, fps=30.0)).open()
    said = " ".join(record.getMessage() for record in caplog.records)
    assert "nothing is reaching its input" in said, said
    assert "lengthens its exposure" not in said, said
