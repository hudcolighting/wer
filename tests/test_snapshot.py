"""Snapshots: saving a still from the feed, recording or not.

No Qt needed for the saving itself, which is the point of keeping it separate.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from wer.core.showfile import RecordingConfig, ShowFile, load_show, save_show
from wer.video.snapshot import SnapshotFormat, save_snapshot


def frame(width: int = 320, height: int = 180) -> np.ndarray:
    image = np.zeros((height, width, 3), np.uint8)
    # Content, so an empty file or a wrong decode is obvious.
    image[: height // 2, : width // 2] = (30, 180, 240)
    image[height // 2 :, width // 2 :] = (200, 60, 40)
    return image


# ------------------------------------------------------------------- saving


def test_png_is_written_and_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "shot.png"
    result = save_snapshot(frame(), path)
    assert result.ok, result.detail
    assert path.is_file() and result.size_bytes > 0

    decoded = cv2.imread(str(path))
    assert decoded is not None
    assert decoded.shape == (180, 320, 3)


def test_png_is_lossless(tmp_path: Path) -> None:
    """The reason PNG is the default: a still of a lighting state is a thing
    you may want to look at closely."""
    original = frame()
    path = tmp_path / "shot.png"
    save_snapshot(original, path)
    assert np.array_equal(cv2.imread(str(path)), original)


def test_jpeg_is_written_and_is_smaller(tmp_path: Path) -> None:
    """Compared on camera-like content, not flat colour.

    PNG beats JPEG on large flat areas, so a synthetic block image would prove
    the opposite of what a real photograph does.
    """
    rng = np.random.default_rng(1)
    noisy = rng.integers(0, 255, (1080, 1920, 3), dtype=np.uint8)

    png = tmp_path / "a.png"
    jpg = tmp_path / "a.jpg"
    assert save_snapshot(noisy, png, image_format=SnapshotFormat.PNG).ok
    assert save_snapshot(noisy, jpg, image_format=SnapshotFormat.JPEG).ok
    assert jpg.stat().st_size < png.stat().st_size


def test_the_directory_is_created(tmp_path: Path) -> None:
    path = tmp_path / "not" / "there" / "yet" / "shot.png"
    assert save_snapshot(frame(), path).ok
    assert path.is_file()


def test_a_non_ascii_path_works(tmp_path: Path) -> None:
    """cv2.imwrite fails SILENTLY on these, returning False rather than raising.

    A show called "Rosencrantz och Gyldenstern", or a Windows account with an
    accent in the name, would produce no file and no error. This is why the
    encode-then-write approach exists.
    """
    path = tmp_path / "Rosencrantz och Gyldenstern — cue 58.png"
    result = save_snapshot(frame(), path)
    assert result.ok, result.detail
    assert path.is_file()
    assert path.stat().st_size > 0


def test_an_empty_frame_is_refused_not_crashed(tmp_path: Path) -> None:
    result = save_snapshot(np.zeros((0, 0, 3), np.uint8), tmp_path / "x.png")
    assert not result.ok
    assert result.detail


def test_none_is_refused(tmp_path: Path) -> None:
    result = save_snapshot(None, tmp_path / "x.png")
    assert not result.ok and result.detail


def test_an_unwritable_destination_is_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    result = save_snapshot(frame(), blocker / "shot.png")
    assert not result.ok
    assert result.detail


def test_the_overlay_is_included_because_the_caller_supplies_it(qt_app, tmp_path: Path) -> None:
    """save_snapshot encodes what it is given; the window hands it the
    composited frame, so what you see is what you get."""
    from wer.core.databus import DataBus
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    bus.publish("eos.cue.active.number", "58", source_connection_id="eos")
    comp = Compositor(bus)
    comp.add(TextWidget("t", "Cue {eos.cue.active.number}"))

    image = frame(1280, 720)
    clean = image.copy()
    comp.composite(image, in_place=True)
    assert not np.array_equal(image, clean), "nothing was drawn"

    path = tmp_path / "with_overlay.png"
    assert save_snapshot(image, path).ok
    assert np.array_equal(cv2.imread(str(path)), image)


# ------------------------------------------------------------------ settings


def test_snapshot_settings_have_sensible_defaults() -> None:
    config = RecordingConfig()
    assert config.snapshot_format == "png"
    assert "{cue}" in config.snapshot_template, (
        "the cue is usually the reason a still was taken"
    )
    assert config.snapshot_marks_recording is True


def test_snapshots_default_to_a_folder_beside_the_recordings(tmp_path: Path) -> None:
    config = RecordingConfig(directory=str(tmp_path))
    assert config.resolved_snapshot_directory() == tmp_path / "Snapshots"


def test_a_chosen_snapshot_folder_is_used(tmp_path: Path) -> None:
    config = RecordingConfig(snapshot_directory=str(tmp_path / "stills"))
    assert config.resolved_snapshot_directory() == tmp_path / "stills"


def test_snapshot_settings_round_trip(tmp_path: Path) -> None:
    show = ShowFile()
    show.recording = RecordingConfig(
        snapshot_directory=str(tmp_path / "s"),
        snapshot_template="{cue}_{time}",
        snapshot_format="jpg",
        snapshot_marks_recording=False,
    )
    path = tmp_path / "show.wer"
    save_show(show, path)
    loaded = load_show(path)
    assert loaded.recording.snapshot_template == "{cue}_{time}"
    assert loaded.recording.snapshot_format == "jpg"
    assert loaded.recording.snapshot_marks_recording is False


def test_the_cue_token_is_substituted() -> None:
    from wer.core.showfile import render_filename

    name = render_filename("{show}_cue{cue}", show_name="Errors", cue="58")
    assert name == "Errors_cue58"


def test_a_missing_cue_does_not_leave_a_brace() -> None:
    """No console connected is a normal state; the filename must still work."""
    from wer.core.showfile import render_filename

    name = render_filename("{show}_cue{cue}", show_name="Errors", cue="")
    assert "{" not in name and "}" not in name


# ---------------------------------------------------------------- in the app


def test_snapshots_work_without_recording(qt_app, tmp_path: Path) -> None:
    """The whole point: the camera is live from launch, recording is separate."""
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.show_file.recording.directory = str(tmp_path)
        # Stand in for a live camera without needing hardware in the test.
        window.preview._last_frame = frame(640, 360)

        assert not window.recorder.is_recording
        window.take_snapshot()

        saved = list(window.show_file.recording.resolved_snapshot_directory().glob("*"))
        assert len(saved) == 1
        assert saved[0].stat().st_size > 0
    finally:
        window.close()
        window.deleteLater()


def test_two_snapshots_in_the_same_second_do_not_overwrite(qt_app, tmp_path: Path) -> None:
    """During a tech this happens, and losing one silently would be poor."""
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.show_file.recording.directory = str(tmp_path)
        window.preview._last_frame = frame(320, 180)
        window.take_snapshot()
        window.take_snapshot()

        saved = list(window.show_file.recording.resolved_snapshot_directory().glob("*"))
        assert len(saved) == 2
    finally:
        window.close()
        window.deleteLater()


def test_a_snapshot_with_no_camera_says_so_rather_than_crashing(qt_app) -> None:
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.preview._last_frame = None
        window.take_snapshot()  # must not raise
    finally:
        window.close()
        window.deleteLater()
