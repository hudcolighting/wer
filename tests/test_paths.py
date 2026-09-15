"""Path resolution behaviour that the frozen build depends on."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wer import paths


def test_source_checkout_is_not_frozen() -> None:
    assert paths.is_frozen() is False


def test_repo_root_contains_the_source_tree() -> None:
    """repo_root() walks up three levels; catch it if the layout moves."""
    assert (paths.repo_root() / "src" / "wer" / "paths.py").is_file()


def test_ffmpeg_is_found_in_a_developer_checkout() -> None:
    """Requires tools/fetch-ffmpeg.ps1 to have run."""
    found = paths.ffmpeg_path()
    assert found is not None, "run tools/fetch-ffmpeg.ps1"
    assert found.name == "ffmpeg.exe"
    assert found.is_file()


def test_log_dir_is_writable() -> None:
    """A log directory we cannot write to is the same as having no log."""
    directory = paths.log_dir()
    assert paths._is_writable(directory)


def test_user_data_dir_is_absolute() -> None:
    assert Path(paths.user_data_dir()).is_absolute()


def test_the_data_dir_can_be_moved(tmp_path, monkeypatch) -> None:
    """Wanted for two reasons: a portable install, and keeping the test suite
    out of the settings of whoever runs it."""
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "elsewhere"))
    assert paths.user_data_dir() == tmp_path / "elsewhere"


def test_an_empty_override_is_ignored(tmp_path, monkeypatch) -> None:
    """An unset variable often arrives as "" rather than absent."""
    monkeypatch.setenv(paths.DATA_DIR_ENV, "")
    assert paths.user_data_dir().name == "Wer"


def test_the_test_run_is_not_writing_to_the_real_settings() -> None:
    """A guard on the guard: this failing means the suite is about to
    overwrite the settings of whoever is running it, which it once did."""
    assert os.environ.get(paths.DATA_DIR_ENV), (
        "the isolated_data_dir fixture is not in effect"
    )
    # Not a path check on "AppData": pytest's own temp directories live under
    # it on Windows. What matters is that this is not the installed location.
    monkeypatched = paths.user_data_dir()
    os.environ.pop(paths.DATA_DIR_ENV)
    try:
        real = paths.user_data_dir()
    finally:
        os.environ[paths.DATA_DIR_ENV] = str(monkeypatched)
    assert monkeypatched != real


# ------------------------------------------------------------- the app icon


def test_the_icon_ships_with_the_source_tree() -> None:
    """A checkout must be able to find it too, not just a frozen build."""
    assert paths.icon_path() is not None
    assert paths.icon_path().name == "wer.ico"


def test_the_icon_carries_every_size_windows_asks_for() -> None:
    """Windows picks per context: 16 in the taskbar, 256 in Explorer's
    extra-large view. A missing size gets scaled and looks it."""
    import struct

    data = paths.icon_path().read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1), "not an .ico"
    sizes = set()
    for i in range(count):
        w, h = data[6 + i * 16], data[7 + i * 16]
        sizes.add((w or 256, h or 256))
    for wanted in (16, 24, 32, 48, 256):
        assert (wanted, wanted) in sizes, f"{wanted}px missing from the .ico"


def test_the_icon_is_not_invisible_on_a_dark_taskbar() -> None:
    """The reason the mark sits on a filled hexagon rather than floating as a
    bare black outline: Windows 11's taskbar is dark by default, and a black
    outline on transparency disappears into it entirely."""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    svg = paths.icon_path().with_name("wer-icon.svg")
    assert svg.is_file(), "the icon's svg source should sit beside the .ico"

    renderer = QSvgRenderer(str(svg))
    assert renderer.isValid()
    image = QImage(64, 64, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, 64, 64))
    painter.end()

    # Against a dark taskbar, something in the mark has to be light.
    brightest = max(
        max(
            (lambda c: (c.red() + c.green() + c.blue()) / 3)(image.pixelColor(x, y))
            for x in range(64)
            if image.pixelColor(x, y).alpha() > 128
        )
        for y in range(64)
        if any(image.pixelColor(x, y).alpha() > 128 for x in range(64))
    )
    assert brightest > 180, (
        f"brightest opaque pixel is {brightest:.0f}; this icon would vanish "
        "on a dark taskbar"
    )
