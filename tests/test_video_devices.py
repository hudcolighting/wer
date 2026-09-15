"""Device enumeration and format selection.

Enumeration touches real hardware, so those tests skip when no camera is
present rather than failing - CI and a headless box are both legitimate places
to run the suite. The pure logic (format ranking, defaults) is always tested.

No Qt in this process.
"""

from __future__ import annotations

import pytest

from wer.video.devices import (
    FOURCC_PREFERENCE,
    CaptureDevice,
    VideoFormat,
    best_format,
    enumerate_video_devices,
    probe_formats,
)


def fmt(width: int, height: int, fourcc: str, fps: float = 30.0) -> VideoFormat:
    return VideoFormat(width, height, 15.0, fps, fourcc)


# ------------------------------------------------------------------- pure logic


def test_best_format_prefers_the_target_resolution() -> None:
    formats = [fmt(2560, 1440, "NV12"), fmt(1920, 1080, "NV12"), fmt(1280, 720, "NV12")]
    assert best_format(formats, target_height=1080).height == 1080


def test_best_format_never_overshoots_the_target() -> None:
    """A camera that can do 1440p is not a reason to record a four-hour tech at 1440p."""
    formats = [fmt(2560, 1440, "NV12"), fmt(1280, 720, "NV12")]
    assert best_format(formats, target_height=1080).height == 720


def test_best_format_prefers_mjpg_when_resolutions_tie() -> None:
    """Uncompressed formats are why a camera silently delivers 5 fps."""
    formats = [fmt(1920, 1080, "YUY2"), fmt(1920, 1080, "MJPG"), fmt(1920, 1080, "NV12")]
    assert best_format(formats, target_height=1080).fourcc == "MJPG"


def test_best_format_copes_with_mjpg_being_absent() -> None:
    """This machine's integrated camera offers only nv12 and yuyv422."""
    formats = [fmt(1920, 1080, "YUY2"), fmt(1920, 1080, "NV12")]
    chosen = best_format(formats, target_height=1080)
    assert chosen.fourcc == "NV12", "NV12 should outrank YUY2 when MJPG is missing"


def test_best_format_of_nothing_is_none() -> None:
    assert best_format([]) is None


def test_fourcc_preference_order_is_meaningful() -> None:
    assert FOURCC_PREFERENCE[0] == "MJPG"


def test_video_format_renders_readably() -> None:
    assert str(fmt(1920, 1080, "MJPG")) == "1920x1080 @ 15-30 fps (MJPG)"
    assert str(VideoFormat(1280, 720, 30.0, 30.0, "NV12")) == "1280x720 @ 30 fps (NV12)"


# ---------------------------------------------------------------- real hardware


def test_enumeration_does_not_raise_without_a_camera() -> None:
    """A machine with no camera is a normal state, not a crash."""
    assert isinstance(enumerate_video_devices(), list)


@pytest.mark.skipif(not enumerate_video_devices(), reason="no camera present")
def test_probe_formats_returns_something_for_a_real_device() -> None:
    device = enumerate_video_devices()[0]
    formats = probe_formats(device)
    assert formats, f"ffmpeg reported no formats for {device.name}"
    assert all(f.width > 0 and f.height > 0 for f in formats)
    assert best_format(formats) is not None


def test_probe_formats_of_a_bogus_device_is_empty_not_an_error() -> None:
    """Callers fall back to common resolutions; they must not get an exception."""
    assert probe_formats(CaptureDevice(99, "No Such Camera")) == []


# ---------------------------------------------------- audio sample formats

#: Real `-list_options` output from a Blackmagic UltraStudio Recorder 3G's
#: embedded HDMI audio, trimmed. 44.1 kHz is listed first, which is exactly why
#: ffmpeg picked it when no rate was requested. Note the padding: rates below
#: 10 kHz are space-padded, and so are single-digit bit depths.
BLACKMAGIC_AUDIO_OPTIONS = """\
[in#0 @ 000001d6fba11f00] DirectShow audio only device options (from audio devices)
[in#0 @ 000001d6fba11f00]  Pin "Capture" (alternative pin name "Capture")
[in#0 @ 000001d6fba11f00]   ch= 2, bits=16, rate= 44100
    Last message repeated 1 times
[in#0 @ 000001d6fba11f00]   ch= 1, bits=16, rate= 44100
[in#0 @ 000001d6fba11f00]   ch= 2, bits=16, rate= 32000
[in#0 @ 000001d6fba11f00]   ch= 2, bits= 8, rate= 44100
[in#0 @ 000001d6fba11f00]   ch= 2, bits=16, rate=  8000
[in#0 @ 000001d6fba11f00]   ch= 2, bits=16, rate= 48000
[in#0 @ 000001d6fba11f00]   ch= 1, bits=16, rate= 48000
[in#0 @ 000001d6fba11f00]   ch= 2, bits=16, rate= 96000
Error opening input file audio=Line In (Blackmagic UltraStudio Recorder 3G Audio).
"""


def test_audio_formats_come_from_what_directshow_lists(monkeypatch) -> None:
    from wer.video import devices

    monkeypatch.setattr(
        devices, "_run_ffmpeg_list_options",
        lambda name, kind="video": BLACKMAGIC_AUDIO_OPTIONS,
    )
    formats = devices.probe_audio_formats(
        devices.AudioDevice("Line In (Blackmagic UltraStudio Recorder 3G Audio)")
    )
    assert devices.AudioFormat(48000, 2, 16) in formats
    assert devices.AudioFormat(8000, 2, 16) in formats, "space-padded rates must parse"
    assert devices.AudioFormat(44100, 2, 8) in formats, "space-padded bit depths must parse"
    assert len(formats) == len(set(formats)), "a repeated line produced duplicates"
    assert formats == sorted(formats)


def test_the_audio_probe_asks_directshow_for_audio(monkeypatch) -> None:
    """The shim was hardwired to video=. Asked for under video=, an audio
    input is simply not found, and the empty answer would read as "this
    device offers no formats" instead of as the wrong question."""
    from wer.video import devices

    asked = []
    monkeypatch.setattr(
        devices, "_run_ffmpeg_list_options",
        lambda name, kind="video": asked.append(kind) or "",
    )
    devices.probe_audio_formats(devices.AudioDevice("Mic"))
    assert asked == ["audio"]


def test_no_answer_from_ffmpeg_means_no_formats_not_an_error(monkeypatch) -> None:
    from wer.video import devices

    monkeypatch.setattr(devices, "_run_ffmpeg_list_options", lambda name, kind="video": None)
    assert devices.probe_audio_formats(devices.AudioDevice("Mic")) == []


def test_48k_stereo_16_bit_is_chosen_when_offered() -> None:
    from wer.video.devices import AudioFormat, best_audio_format

    offered = [
        AudioFormat(44100, 2, 16), AudioFormat(48000, 1, 16),
        AudioFormat(48000, 2, 16), AudioFormat(48000, 2, 8), AudioFormat(96000, 2, 16),
    ]
    assert best_audio_format(offered) == AudioFormat(48000, 2, 16)


def test_mono_48k_is_still_better_than_a_resample() -> None:
    from wer.video.devices import AudioFormat, best_audio_format

    assert best_audio_format([AudioFormat(44100, 2, 16), AudioFormat(48000, 1, 16)]) == (
        AudioFormat(48000, 1, 16)
    )


def test_without_48k_on_offer_nothing_is_forced() -> None:
    """DirectShow refuses a format the pin does not list, so a guess would
    lose the audio input entirely. Declining leaves today's behaviour intact."""
    from wer.video.devices import AudioFormat, best_audio_format

    assert best_audio_format([AudioFormat(44100, 2, 16), AudioFormat(96000, 2, 16)]) is None
    assert best_audio_format([]) is None
