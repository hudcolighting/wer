"""The main window can be made shorter than a laptop screen.

It could not. A tab widget is as tall as its tallest page, and the Recording
tab stacked five group boxes with nothing to scroll them: 1022 px, which held
the whole window at least 1106 px tall. On a 2560x1600 laptop at 150% scaling
Windows leaves 1067 px for it, so the bottom of the window sat off the screen
and the window would not shrink to fit. It had been that tall since the
Snapshots box went in.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPoint, Qt

from wer.core.showfile import RecordingConfig

#: A 1920x1080 laptop screen at 125% scaling is 864 logical pixels tall. Less
#: the Windows 11 taskbar (48) and the title bar and frame (about 37, measured
#: on the development laptop), that leaves 779 for the window itself.
LAPTOP_WINDOW_HEIGHT = 864 - 48 - 37


def test_the_window_can_be_made_shorter_than_a_laptop_screen(qt_app) -> None:
    """Every tab counts towards this, so the next page to grow is caught too."""
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        needed = window.minimumSizeHint().height()
        tabs = window.tabs
        pages = ", ".join(
            f"{tabs.tabText(i)} {tabs.widget(i).minimumSizeHint().height()}"
            for i in range(tabs.count())
        )
        assert needed <= LAPTOP_WINDOW_HEIGHT, (
            f"the window cannot be made shorter than {needed} px (pages: {pages})"
        )
    finally:
        window.close()
        window.deleteLater()


def test_the_record_button_stays_in_sight_while_the_settings_scroll(qt_app) -> None:
    """Only the settings scroll. The Record button, the take's state and the
    encoder queue are what someone needs to see mid-take, so they must not go
    out of sight whichever way the settings are scrolled."""
    from wer.ui.record_panel import RecordPanel

    panel = RecordPanel(RecordingConfig())
    deadline = time.monotonic() + 10
    while panel.encoders_pending and time.monotonic() < deadline:
        qt_app.processEvents()
    panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    try:
        panel.resize(700, 450)
        panel.show()
        qt_app.processEvents()
        assert panel.height() == 450, f"the panel would not shrink below {panel.height()} px"

        scroll = panel._settings_scroll
        bar = scroll.verticalScrollBar()
        assert bar.isVisible(), "at 450 px the settings should have needed a scroll bar"
        for position in (bar.minimum(), bar.maximum()):
            bar.setValue(position)
            qt_app.processEvents()
            for widget in (panel._record_button, panel._state_label, panel._queue_bar):
                top = widget.mapTo(panel, QPoint(0, 0)).y()
                assert not scroll.isAncestorOf(widget)
                assert 0 <= top and top + widget.height() <= panel.height(), (
                    f"{type(widget).__name__} at y={top} is outside a "
                    f"{panel.height()} px panel with the settings scrolled to {position}"
                )
    finally:
        panel.close()
        panel.deleteLater()
