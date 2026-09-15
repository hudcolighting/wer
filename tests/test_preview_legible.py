"""The preview's own words can be read whichever theme Windows is in.

The preview is painted near-black (#111) and its text colour was never set, so
the label took it from the Windows theme: white in dark mode, where it was
written and never looked wrong, and black in light mode. On a Surface Pro 8 in
light mode (12 Sep 2026) "Starting Surface Camera Front…" was black on #111,
and the preview looked as though it said nothing for the whole start-up wait.
"""

from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtGui import QColor, QImage, QPalette

#: How much brighter than the #111 ground the brightest pixel must be. Measured
#: at 150% scaling with the stylesheet as it was, in light mode: 0, since black
#: text on #111 is no brighter than the ground. As it is now: 191 in either
#: theme, which is the #d0d0d0 it sets.
LEGIBLE_ABOVE_GROUND = 120


def _theme(qt_app, name: str) -> QPalette:
    if name == "light":
        # The palette Windows gives Qt in light mode: the style's own, with
        # black window text. Rendering with it is how the bug was reproduced.
        return qt_app.style().standardPalette()
    palette = QPalette()
    for role, colour in (
        (QPalette.ColorRole.Window, "#202020"),
        (QPalette.ColorRole.WindowText, "#ffffff"),
        (QPalette.ColorRole.Base, "#202020"),
        (QPalette.ColorRole.Text, "#ffffff"),
    ):
        palette.setColor(role, QColor(colour))
    return palette


def _grey_levels(image: QImage) -> np.ndarray:
    grey = image.convertToFormat(QImage.Format.Format_Grayscale8)
    rows = np.frombuffer(grey.constBits(), dtype=np.uint8)
    # Copied: the array only borrows grey's memory, which goes with grey when
    # this returns. Uncopied, the first measurements for this test read freed
    # memory, and passed the bug.
    return rows.reshape(grey.height(), grey.bytesPerLine())[:, : grey.width()].copy()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("text", ["No camera", "Starting Surface Camera Front…"])
def test_the_preview_says_what_it_is_doing_in_either_theme(qt_app, theme, text) -> None:
    from wer.ui.preview import VideoSurface

    previous = qt_app.palette()
    qt_app.setPalette(_theme(qt_app, theme))
    try:
        surface = VideoSurface()
        surface.resize(480, 270)
        surface.clear_frame(text)
        levels = _grey_levels(surface.grab().toImage())
    finally:
        qt_app.setPalette(previous)
        surface.deleteLater()

    ground = int(np.median(levels))
    brightest = int(levels.max())
    assert brightest - ground >= LEGIBLE_ABOVE_GROUND, (
        f"{text!r} in {theme} mode: the brightest pixel is {brightest} on a ground "
        f"of {ground}, so the text cannot be read"
    )
