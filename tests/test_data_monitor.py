"""The Data Monitor, mostly its colours.

Colour is the whole readability of this panel, and it is the sort of thing that
breaks without any test noticing -- which is exactly what happened: rows were
rendered in explicit black, invisible on the dark theme this app lives on.
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import Qt

from wer.core.databus import DataBus
from wer.ui.data_monitor import DataMonitorPanel


@pytest.fixture()
def bus() -> DataBus:
    return DataBus()


@pytest.fixture()
def panel(qt_app, bus: DataBus) -> DataMonitorPanel:
    panel = DataMonitorPanel(bus)
    yield panel
    panel.deleteLater()


def fill(panel: DataMonitorPanel, bus: DataBus) -> None:
    """Push whatever is on the bus through the panel, as the bridge would."""
    panel.apply_batch({entry.key: entry for entry in bus.snapshot()})


def foreground(panel: DataMonitorPanel, row: int = 0):
    return panel._model.item(row, 0).data(Qt.ItemDataRole.ForegroundRole)


# ------------------------------------------------------------------- colours


def test_a_settled_row_uses_the_theme_colour_not_an_explicit_one(
    panel: DataMonitorPanel, bus: DataBus
) -> None:
    """The bug this test exists for.

    setForeground(QColor()) does NOT mean "inherit the palette" -- an invalid
    QColor becomes an explicit black brush, which is unreadable on a dark theme.
    Clearing the role is the only way back to the palette.
    """
    bus.publish("a.b", 1, source_connection_id="t")
    fill(panel, bus)

    # Let it age past the "just changed" highlight.
    time.sleep(1.1)
    panel._refresh_ages()

    assert foreground(panel) is None, (
        "a settled row must not carry a hard-coded colour"
    )


def test_no_row_is_ever_rendered_black(panel: DataMonitorPanel, bus: DataBus) -> None:
    """Black is invisible on the theme this app is used with."""
    for index in range(10):
        bus.publish(f"k.{index}", index, source_connection_id="t")
    fill(panel, bus)
    time.sleep(1.1)
    panel._refresh_ages()

    for row in range(panel._model.rowCount()):
        brush = foreground(panel, row)
        if brush is None:
            continue
        colour = brush.color() if hasattr(brush, "color") else brush
        assert colour.name() != "#000000", f"row {row} is black"


def test_a_recent_change_is_highlighted(panel: DataMonitorPanel, bus: DataBus) -> None:
    bus.publish("a.b", 1, source_connection_id="t")
    fill(panel, bus)

    brush = foreground(panel)
    assert brush is not None, "a just-changed row should stand out"
    colour = brush.color() if hasattr(brush, "color") else brush
    assert colour.lightness() > 100, "the highlight must be visible on dark"


def test_a_stale_value_is_marked(panel: DataMonitorPanel, bus: DataBus) -> None:
    """Stale data must never pass for current data."""
    bus.publish("a.b", 1, source_connection_id="t", stale_after=0.05)
    fill(panel, bus)
    time.sleep(0.15)
    panel._refresh_ages()

    brush = foreground(panel)
    assert brush is not None
    colour = brush.color() if hasattr(brush, "color") else brush
    assert colour.name() == "#c0392b", "stale rows should be red"


def test_a_row_recovers_its_normal_colour_when_data_returns(
    panel: DataMonitorPanel, bus: DataBus
) -> None:
    """A console that comes back must not leave rows looking broken."""
    bus.publish("a.b", 1, source_connection_id="t", stale_after=0.05)
    fill(panel, bus)
    time.sleep(0.15)
    panel._refresh_ages()
    assert foreground(panel) is not None  # red

    bus.publish("a.b", 2, source_connection_id="t", stale_after=30.0)
    fill(panel, bus)
    time.sleep(1.1)
    panel._refresh_ages()
    assert foreground(panel) is None, "should be back to the theme colour"


# -------------------------------------------------------------------- basics


def test_rows_appear_for_bus_keys(panel: DataMonitorPanel, bus: DataBus) -> None:
    bus.publish("eos.cue.active.number", "58", source_connection_id="eos")
    bus.publish("clock.wall", "21:57:31", source_connection_id="system")
    fill(panel, bus)
    assert panel._model.rowCount() == 2


def test_values_are_shown(panel: DataMonitorPanel, bus: DataBus) -> None:
    bus.publish("eos.cue.active.number", "58", source_connection_id="eos")
    fill(panel, bus)
    assert panel._model.item(0, 1).text() == "58"


def test_floats_are_rounded_for_reading(panel: DataMonitorPanel, bus: DataBus) -> None:
    """Fade progress arrives as 0.30000001192092896."""
    bus.publish("eos.cue.active.progress", 0.30000001192092896,
                source_connection_id="eos")
    fill(panel, bus)
    assert panel._model.item(0, 1).text() == "0.3"


def test_pausing_freezes_the_view_only(panel: DataMonitorPanel, bus: DataBus) -> None:
    bus.publish("a.b", 1, source_connection_id="t")
    fill(panel, bus)

    panel._set_paused(True)
    bus.publish("c.d", 2, source_connection_id="t")
    fill(panel, bus)
    assert panel._model.rowCount() == 1, "the view should be frozen"
    assert len(bus) == 2, "but the bus keeps running"

    panel._set_paused(False)
    fill(panel, bus)
    assert panel._model.rowCount() == 2


def test_unpausing_catches_the_table_up_with_the_bus(
    panel: DataMonitorPanel, bus: DataBus
) -> None:
    """Pausing drops the batches that arrive while it is paused.

    Un-pausing therefore left a key that changed during the pause showing its
    old value -- and because the age timer keeps re-reading the entry from the
    bus, the row settled at "0.0s" in just-changed green while displaying a
    number the console stopped sending. Only another change to that same key
    ever repaired it. This is the debugging tool the app's other failures are
    diagnosed with; it must not be the thing that lies.
    """
    bus.publish("eos.cue.active.number", "58", source_connection_id="eos")
    fill(panel, bus)
    assert panel._model.item(0, 1).text() == "58"

    panel._pause.setChecked(True)
    bus.publish("eos.cue.active.number", "61", source_connection_id="eos")
    bus.publish("eos.cmdline.text", "LIVE: ", source_connection_id="eos")
    fill(panel, bus)

    panel._pause.setChecked(False)
    assert panel._model.item(0, 1).text() == "61", "showed a pre-pause value as current"
    assert panel._model.rowCount() == 2, "a key first seen during the pause is missing"


def test_unpausing_catches_up_the_type_and_source_too(
    panel: DataMonitorPanel, bus: DataBus
) -> None:
    """Type drift is what the Type column is for; it went stale the same way."""
    bus.publish("eos.cue.active.progress", 0.5, source_connection_id="eos")
    fill(panel, bus)

    panel._pause.setChecked(True)
    bus.publish("eos.cue.active.progress", "0.9", source_connection_id="manual")
    panel._pause.setChecked(False)

    assert panel._model.item(0, 2).text() == "str"
    assert panel._model.item(0, 4).text() == "manual"


def test_clearing_empties_the_table(panel: DataMonitorPanel, bus: DataBus) -> None:
    bus.publish("a.b", 1, source_connection_id="t")
    fill(panel, bus)
    panel.clear()
    assert panel._model.rowCount() == 0


def test_cleared_rows_come_back_as_values_change_or_all_at_once_on_unpause(
    panel: DataMonitorPanel, bus: DataBus
) -> None:
    """What the Clear button's tooltip promises, so the tooltip cannot drift.

    It used to say rows "reappear as keys are republished", but the bridge
    only carries a key whose value changed: a connection healthily repeating
    the same value brings nothing back. The two ways back are a change to
    that key, and unpausing, which resyncs the whole table from the bus.
    """
    from PySide6.QtWidgets import QPushButton

    bus.publish("a.b", 1, source_connection_id="t")
    bus.publish("c.d", 2, source_connection_id="t")
    fill(panel, bus)
    panel.clear()

    # The bridge would carry only c.d here: a.b did not change.
    entry = bus.publish("c.d", 3, source_connection_id="t")
    panel.apply_batch({"c.d": entry})
    assert [panel._model.item(r, 0).text()
            for r in range(panel._model.rowCount())] == ["c.d"]

    panel._pause.setChecked(True)
    panel._pause.setChecked(False)
    assert panel._model.rowCount() == 2, "unpausing should bring every key back"

    buttons = panel.findChildren(QPushButton)
    clear = next(b for b in buttons if b.text() == "Clear")
    tip = clear.toolTip()
    assert "value next changes" in tip and "Pause" in tip
    assert "republished" not in tip
