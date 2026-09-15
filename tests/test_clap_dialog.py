"""The clap test dialog, run on a take whose sound is known to be 100 ms late.

The take is made by the vendored ffmpeg: a white box flashes on at the same
instants a click sounds, and the click's input is delayed 100 ms. Picking the
first frame in which the box is lit, as a person would pick the frame where the
hands meet, has to come out at a lag of 100 ms and an offer of -100 ms.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
import shiboken6

from wer.core.avsync import CLAP, offset_for
from wer.core.showfile import RecordingConfig
from wer.video import clap as measure

LAG_S = 0.100
BOX = (100, 80, 120, 80)
CAMERA = "Blackmagic WDM Capture"
AUDIO = "Line In (Blackmagic UltraStudio Recorder 3G Audio)"


def _wait(qt_app, predicate, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "the clap test's worker never answered"
        qt_app.processEvents()
        time.sleep(0.01)


@pytest.fixture
def made(qt_app):
    """Widgets a test builds, deleted when the test ends, as the other window
    tests do, rather than left for the interpreter to tear down after the
    QApplication has gone."""
    widgets: list = []
    yield widgets
    for widget in widgets:
        if shiboken6.isValid(widget):
            widget.close()
            shiboken6.delete(widget)
    qt_app.processEvents()


@pytest.fixture(scope="module")
def clap_take(tmp_path_factory) -> Path:
    exe = measure.ffmpeg_path()
    if exe is None:
        pytest.skip("the vendored ffmpeg is not present")
    output = tmp_path_factory.mktemp("clap-dialog") / "take.mkv"
    x, y, w, h = BOX
    video = (
        f"color=c=black:s=320x240:r=30:d=8,"
        f"drawbox=x={x}:y={y}:w={w}:h={h}:color=white:t=fill:"
        r"enable='between(mod(t\,1)\,0.5\,0.8)'"
    )
    audio = r"aevalsrc='0.8*sin(2*PI*2000*t)*between(mod(t\,1)\,0.5\,0.504)':s=48000:d=8"
    subprocess.run(
        [str(exe), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", video,
         "-itsoffset", f"{LAG_S:.3f}", "-f", "lavfi", "-i", audio,
         "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", str(output)],
        check=True, capture_output=True, timeout=120,
    )
    return output


@pytest.fixture(scope="module")
def silent_take(tmp_path_factory) -> Path:
    """A take whose sound track is exact zeros: picture, and an audio input that
    delivered nothing, as a gated laptop microphone array hands over."""
    exe = measure.ffmpeg_path()
    if exe is None:
        pytest.skip("the vendored ffmpeg is not present")
    output = tmp_path_factory.mktemp("clap-silent") / "take.mkv"
    subprocess.run(
        [str(exe), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=3",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", str(output)],
        check=True, capture_output=True, timeout=120,
    )
    return output


def _lit(strip) -> int:
    brightness = strip.frames.reshape(strip.frames.shape[0], -1).mean(axis=1)
    return int(np.flatnonzero(brightness > 128)[0])


def _dialog(made, path: Path | None, *, audio: str | None = AUDIO, offset: int = 0):
    from wer.ui.clap_dialog import ClapTestDialog, TakeToMeasure

    take = None if path is None else TakeToMeasure(
        path=path, camera=CAMERA, audio=audio, av_offset_ms=offset
    )
    dialog = ClapTestDialog(take)
    made.append(dialog)
    return dialog


def _run_to_picking(qt_app, dialog) -> None:
    dialog.measure()
    _wait(qt_app, lambda: dialog.page != "working")
    assert dialog.page == "region", dialog._error.text()
    dialog.set_region(BOX)
    assert dialog._region_next.isEnabled()
    dialog.read_frames_around_claps()
    _wait(qt_app, lambda: dialog.page != "working")
    assert dialog.page == "pick", dialog._error.text()


def test_a_take_is_measured_and_the_offset_that_cancels_its_lag_is_offered(
    qt_app, made, clap_take
) -> None:
    dialog = _dialog(made, clap_take)
    applied = []
    dialog.applied.connect(applied.append)
    _run_to_picking(qt_app, dialog)

    picked = 0
    while dialog.page == "pick":
        dialog.select_frame(_lit(dialog.current_strip()))
        dialog.use_frame()
        picked += 1
    assert picked >= 5
    assert dialog.page == "result"
    assert dialog.measurement.lag_ms == pytest.approx(LAG_S * 1000, abs=5.0)
    assert "after the picture" in dialog._headline.text()
    assert dialog._apply_button.isEnabled(), dialog._refusal.text()

    dialog.apply()
    assert len(applied) == 1
    result = applied[0]
    assert result.offset_ms == pytest.approx(-100, abs=5)
    assert (result.camera, result.audio, result.recorded_offset_ms) == (CAMERA, AUDIO, 0)
    assert result.claps == picked
    assert not dialog._apply_button.isEnabled(), "the same result could be applied twice"


def test_the_offset_offered_allows_for_the_offset_the_take_was_recorded_with(
    qt_app, made, clap_take
) -> None:
    """A take recorded at -60 ms that still measures 100 ms late needs -160 ms."""
    dialog = _dialog(made, clap_take, offset=-60)
    _run_to_picking(qt_app, dialog)
    while dialog.page == "pick":
        dialog.select_frame(_lit(dialog.current_strip()))
        dialog.use_frame()
    assert dialog.result.offset_ms == pytest.approx(-160, abs=5)


def test_the_cursor_starts_on_the_suggested_frame(qt_app, made, clap_take) -> None:
    dialog = _dialog(made, clap_take)
    _run_to_picking(qt_app, dialog)
    assert dialog._film.currentRow() == _lit(dialog.current_strip())


def test_skipping_too_many_claps_shows_the_result_but_offers_nothing(
    qt_app, made, clap_take
) -> None:
    dialog = _dialog(made, clap_take)
    _run_to_picking(qt_app, dialog)
    dialog.select_frame(_lit(dialog.current_strip()))
    dialog.use_frame()
    while dialog.page == "pick":
        dialog.skip_clap()
    assert dialog.page == "result"
    assert not dialog._apply_button.isEnabled()
    assert "at least 5" in dialog._refusal.text()


def _start_over_button(dialog, page: str):
    from PySide6.QtWidgets import QPushButton

    widget = dialog._pages.widget(dialog._page_names[page])
    buttons = [b for b in widget.findChildren(QPushButton) if b.text() == "Start over"]
    assert len(buttons) == 1, f"the {page} page has {len(buttons)} Start over buttons"
    return buttons[0]


def test_start_over_goes_back_to_the_take_from_every_page(qt_app, made, clap_take) -> None:
    """Pressed as a person presses it, through the button: clicked() passes a
    bool, and the buttons once handed it to the first page as an error to show,
    which raised a TypeError and left the dialog where it was."""
    dialog = _dialog(made, clap_take)

    dialog.measure()
    _wait(qt_app, lambda: dialog.page != "working")
    assert dialog.page == "region", dialog._error.text()
    _start_over_button(dialog, "region").click()
    assert dialog.page == "intro"
    assert dialog._error.text() == ""
    assert dialog._measure_button.isEnabled()

    _run_to_picking(qt_app, dialog)
    _start_over_button(dialog, "pick").click()
    assert dialog.page == "intro"

    _run_to_picking(qt_app, dialog)
    while dialog.page == "pick":
        dialog.skip_clap()
    assert dialog.page == "result"
    _start_over_button(dialog, "result").click()
    assert dialog.page == "intro"


def test_no_take_yet_says_how_to_get_one(qt_app, made) -> None:
    dialog = _dialog(made, None)
    assert dialog.page == "intro"
    assert not dialog._measure_button.isEnabled()
    assert "No take has been recorded" in dialog._take_label.text()


def test_a_take_without_sound_cannot_be_measured(qt_app, made, clap_take) -> None:
    dialog = _dialog(made, clap_take, audio=None)
    assert not dialog._measure_button.isEnabled()
    assert "without sound" in dialog._take_label.text()


def test_a_take_of_digital_silence_blames_the_input_not_the_clapping(
    qt_app, made, silent_take
) -> None:
    """The laptop's Realtek array behind its voice effects handed Wer exact
    zeros and the clap test found nothing (13 Sep 2026). Asking for a sharper
    clap would have sent someone back on stage to no purpose."""
    dialog = _dialog(made, silent_take)
    dialog.measure()
    _wait(qt_app, lambda: dialog.page != "working")
    assert dialog.page == "intro"
    said = dialog._error.text()
    assert "digital silence" in said
    assert "audio input is the thing to change" in said
    assert "The recording has no sound" in said, "the help entry is not named"
    assert "clap sharply" not in said.lower(), "it still asks for a louder clap"


def test_a_file_that_cannot_be_read_says_why_and_goes_back(qt_app, made, tmp_path) -> None:
    broken = tmp_path / "broken.mkv"
    broken.write_bytes(b"this is not a recording")
    dialog = _dialog(made, broken)
    dialog.measure()
    _wait(qt_app, lambda: dialog.page != "working")
    assert dialog.page == "intro"
    assert dialog._error.text(), "the failure was not said"


def test_applying_a_result_sets_the_offset_for_the_devices_the_take_used(
    qt_app, made, monkeypatch
) -> None:
    """The operator may have switched inputs since the take; the result is for
    the take's own camera and audio input."""
    from wer.ui import record_panel as module
    from wer.ui.clap_dialog import ClapResult

    monkeypatch.setattr(module, "detect_encoders", lambda **_kwargs: [])
    panel = module.RecordPanel(RecordingConfig(audio_device="Microphone Array (Realtek(R) Audio)"))
    made.append(panel)
    panel.set_camera("Integrated Camera")
    changes = []
    panel.settings_changed.connect(lambda: changes.append(True))
    panel.apply_clap_test(ClapResult(
        camera=CAMERA, audio=AUDIO, offset_ms=-62, lag_ms=62.0, recorded_offset_ms=0,
        claps=6, frame_period_ms=33.3,
    ))
    assert changes, "the applied offset was not saved"
    config = panel.config
    config.audio_device = AUDIO
    offset = offset_for(config, CAMERA)
    assert (offset.value_ms, offset.source, offset.entry.claps) == (-62, CLAP, 6)
