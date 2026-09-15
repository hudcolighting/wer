"""Formats a device lists but cannot deliver through OpenCV.

Measured 12 Sep 2026 on a Blackmagic UltraStudio Recorder 3G over SDI: of the
four pixel formats it lists -- UYVY, V210, R210 and BGR0 -- OpenCV's DirectShow
backend delivered frames only for UYVY, at every size and rate tried. The other
three opened, accepted the format, and never produced a frame.

Two faults followed from that. Wer's default pick for the card was BGR0, so it
came up showing nothing on every launch; and a picked format that delivered
nothing used to fall through to Media Foundation with a DirectShow index, which
opened a different camera while the interface named the Blackmagic.
"""

from __future__ import annotations

import pytest

import wer.video.capture as capture_module
from wer.video.capture import CameraCapture, CaptureSettings
from wer.video.devices import (
    FOURCC_PREFERENCE,
    UNREADABLE_FOURCCS,
    VideoFormat,
    _fourcc_rank,
    best_format,
)

DSHOW = 700
MSMF = 1400


def fmt(width: int, height: int, fps: float, fourcc: str) -> VideoFormat:
    return VideoFormat(width, height, fps, fps, fourcc)


#: The shape of what the card lists: every readable and unreadable format at
#: the same sizes and rates, so only the ranking can separate them.
BLACKMAGIC_3G = [
    fmt(width, height, fps, fourcc)
    for width, height in ((2048, 1080), (1920, 1080))
    for fps in (30.0, 29.97, 25.0)
    for fourcc in ("BGR0", "R210", "UYVY", "V210")
]


# ------------------------------------------------------------------ ranking


def test_the_blackmagic_default_is_a_format_it_can_deliver() -> None:
    """The every-launch fault. All four formats tied, and BGR0 sorted first."""
    chosen = best_format(BLACKMAGIC_3G)
    assert chosen is not None
    assert chosen.fourcc not in UNREADABLE_FOURCCS, f"defaulted to {chosen}"
    assert chosen.fourcc == "UYVY"


def test_an_unreadable_format_ranks_below_one_nothing_is_known_about() -> None:
    """Unknown might work. Unreadable has been measured not to."""
    for code in UNREADABLE_FOURCCS:
        assert _fourcc_rank(code) > _fourcc_rank("ZZZZ"), code


def test_mjpg_still_leads_for_usb_cameras() -> None:
    """Unchanged: a USB camera at 1080p30 needs its compressed mode."""
    assert FOURCC_PREFERENCE[0] == "MJPG"
    assert best_format([fmt(1920, 1080, 30.0, "YUY2"), fmt(1920, 1080, 30.0, "MJPG")]).fourcc == "MJPG"


# ----------------------------------------------------------------- fallback


class _FussyHandle:
    """Delivers frames only if no pixel format was set on this handle."""

    def __init__(self, delivers_default: bool) -> None:
        self.fps = 30.0
        self.fourcc_set = False
        self.delivers_default = delivers_default
        self.released = False

    def isOpened(self) -> bool:
        return True

    def set(self, prop, value) -> bool:
        import cv2

        if prop == cv2.CAP_PROP_FOURCC:
            self.fourcc_set = True
        return True

    def get(self, prop) -> float:
        return self.fps

    def read(self):
        if self.fourcc_set or not self.delivers_default:
            return False, None
        import numpy as np

        return True, np.zeros((1080, 1920, 3), dtype=np.uint8)

    def release(self) -> None:
        self.released = True


@pytest.fixture
def handles(monkeypatch, tmp_path):
    """A FRESH fake per cv2.VideoCapture call, as a reopen really gets."""
    import cv2

    state = {"opened": [], "delivers_default": True}
    now = {"t": 0.0}

    def factory(index, backend):
        handle = _FussyHandle(state["delivers_default"])
        real_read = handle.read

        def timed_read():
            now["t"] += 1.0 / handle.fps
            return real_read()

        handle.read = timed_read
        state["opened"].append((backend, handle))
        return handle

    monkeypatch.setattr(cv2, "VideoCapture", factory)
    monkeypatch.setattr(capture_module.time, "perf_counter", lambda: now["t"])
    monkeypatch.setattr(capture_module, "_memory_path", lambda: tmp_path / "backends.json")
    return state


def _blackmagic(**overrides) -> CaptureSettings:
    settings = dict(device_index=2, device_name="Blackmagic WDM Capture",
                    device_count=3, width=1920, height=1080, fps=30.0, fourcc="BGR0")
    settings.update(overrides)
    return CaptureSettings(**settings)


def test_a_format_that_delivers_nothing_falls_back_on_the_same_device(handles) -> None:
    """Same backend, same index, format left to the driver -- still the device asked for."""
    camera = CameraCapture(_blackmagic())

    assert camera.open(), "a card that delivers in its own format was reported as dead"
    assert camera.backend == "DSHOW"
    assert [backend for backend, _ in handles["opened"]] == [DSHOW, DSHOW], (
        "expected the failed open and one retry, both on DirectShow"
    )
    first, retry = (handle for _, handle in handles["opened"])
    assert first.released, "the handle that delivered nothing was left open"
    assert not retry.fourcc_set, "the retry asked for the unreadable format again"


def test_the_fallback_never_reaches_for_the_other_backend(handles) -> None:
    """The wrong-camera fault. A DirectShow index under Media Foundation is another device."""
    CameraCapture(_blackmagic()).open()
    assert MSMF not in [backend for backend, _ in handles["opened"]]


def test_no_retry_when_no_format_was_asked_for(handles) -> None:
    """With the format already left to the driver there is nothing to fall back to."""
    handles["delivers_default"] = False
    camera = CameraCapture(_blackmagic(fourcc=None))

    assert not camera.open()
    assert len(handles["opened"]) == 1


def test_the_failure_names_only_the_backend_tried_and_the_format_asked(handles) -> None:
    """ "Either Media Foundation or DirectShow" sent the reader after a backend never involved."""
    handles["delivers_default"] = False
    errors: list[str] = []
    camera = CameraCapture(
        _blackmagic(width=2048, height=1080, fps=60.0, fourcc="V210"),
        on_error=errors.append,
    )

    assert not camera.open()
    assert errors, "a camera that would not open said nothing"
    assert "DirectShow" in errors[0]
    assert "Media Foundation" not in errors[0], errors[0]
    assert "2048x1080@60" in errors[0] and "V210" in errors[0], errors[0]


# ------------------------------------------------------------ format names


def test_a_format_code_that_is_not_ascii_is_not_reported_as_a_name() -> None:
    """The card reports a code whose bytes decode to printable non-ASCII
    characters, and the log printed that as though it were a format."""
    import cv2

    not_ascii = 0x7D | (0xEB << 8) | (0x36 << 16) | (0xE4 << 24)
    assert CameraCapture._fourcc_text(float(not_ascii)) == ""
    assert CameraCapture._fourcc_text(float(cv2.VideoWriter_fourcc(*"UYVY"))) == "UYVY"
