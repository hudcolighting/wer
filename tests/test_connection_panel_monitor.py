"""The raw traffic monitor on the Connections tab keeps up with its connection.

It froze. The connection keeps its monitor in a buffer capped at 500 entries,
and the pane worked out what was new from the buffer's length. Once the buffer
was full its length stopped changing, so within minutes of connecting to a desk
running cues the pane stopped adding lines for good, while the packet counter
above it went on rising. Its timestamps give no hint that the lines are old,
and this is the pane someone reads while working out whether the desk is
talking at all.
"""

from __future__ import annotations

import pytest

from wer.connections.base import Connection
from wer.core.databus import DataBus
from wer.ui.connection_panel import ConnectionPanel


class HandFedConnection(Connection):
    """Never started: each test records traffic into it directly."""

    @property
    def display_name(self) -> str:
        return "Hand-fed"

    def _run_once(self) -> None:
        raise AssertionError("the monitor tests never start the connection")


@pytest.fixture()
def connection() -> HandFedConnection:
    return HandFedConnection("hand-fed", DataBus())


@pytest.fixture()
def panel(qt_app, connection: HandFedConnection) -> ConnectionPanel:
    panel = ConnectionPanel(connection)
    # The test decides when a refresh happens, so nothing arrives between two
    # lines of it by accident.
    panel._timer.stop()
    yield panel
    panel.deleteLater()


def arrive(connection: Connection, first: int, last: int) -> None:
    """Record packets numbered first..last-1, as the connection thread would."""
    for number in range(first, last):
        connection.record(f"packet {number}")


def shown(panel: ConnectionPanel) -> list[str]:
    """The pane's lines, without their timestamps and arrows."""
    return [
        line.split(" <- ", 1)[-1]
        for line in panel._monitor.toPlainText().splitlines()
    ]


def test_the_pane_keeps_scrolling_once_the_buffer_is_full(
    panel: ConnectionPanel, connection: HandFedConnection
) -> None:
    """The freeze itself: 500 on screen, 100 more arrive, none appeared."""
    arrive(connection, 0, 500)
    panel._refresh()
    arrive(connection, 500, 600)
    panel._refresh()

    lines = shown(panel)
    assert lines[-1] == "packet 599", "the pane froze at the buffer's cap"
    assert lines == [f"packet {n}" for n in range(600)]
    assert panel._monitor_count.text() == "600 lines"


def test_lines_arriving_as_the_buffer_fills_are_not_skipped(
    panel: ConnectionPanel, connection: HandFedConnection
) -> None:
    """With 480 on screen, 30 arriving used to append only the last 20."""
    arrive(connection, 0, 480)
    panel._refresh()
    arrive(connection, 480, 510)
    panel._refresh()

    assert shown(panel) == [f"packet {n}" for n in range(510)]


def test_lines_the_buffer_dropped_between_refreshes_are_owned_up_to(
    panel: ConnectionPanel, connection: HandFedConnection
) -> None:
    """More than the buffer holds, between two refreshes.

    The pane cannot show what the buffer no longer has, but it must not
    present the 500 it does have as everything that arrived.
    """
    arrive(connection, 0, 1200)
    panel._refresh()

    lines = shown(panel)
    assert lines[0].startswith("[700 lines not shown")
    assert lines[1:] == [f"packet {n}" for n in range(700, 1200)]
    assert panel._monitor_count.text() == "1200 lines"


def test_clearing_the_pane_is_not_reported_as_lost_lines(
    panel: ConnectionPanel, connection: HandFedConnection
) -> None:
    """Clear throws lines away on purpose, including any that arrived since
    the last refresh. That is not the buffer losing them."""
    arrive(connection, 0, 10)
    panel._refresh()
    arrive(connection, 10, 15)
    panel._clear_monitor()
    arrive(connection, 15, 18)
    panel._refresh()

    assert shown(panel) == ["packet 15", "packet 16", "packet 17"]
    assert panel._monitor_count.text() == "3 lines"


def test_pausing_the_monitor_is_not_reported_as_lost_lines(
    panel: ConnectionPanel, connection: HandFedConnection
) -> None:
    """Pause keeps traffic out of the buffer on purpose, and those lines must
    not be counted among the ones the buffer dropped.

    That only shows once the buffer overflows after unpausing. Until then the
    lines from before the pause are still in it, and the dropped count is
    taken from the oldest line kept, so a count that went on rising through
    the pause stays hidden. The first version of this test stopped short of
    that and passed with the bug.
    """
    arrive(connection, 0, 5)
    panel._refresh()
    panel._pause.setChecked(True)
    arrive(connection, 5, 1005)
    panel._pause.setChecked(False)
    # More than the buffer holds, before the next refresh.
    arrive(connection, 1005, 1605)
    panel._refresh()

    lines = shown(panel)
    assert lines[:5] == [f"packet {n}" for n in range(5)]
    assert lines[5].startswith("[100 lines not shown"), lines[5]
    assert lines[6:] == [f"packet {n}" for n in range(1105, 1605)]
    assert panel._monitor_count.text() == "605 lines"


def test_a_burst_of_traffic_reaches_the_pane_in_one_append(
    panel: ConnectionPanel, connection: HandFedConnection, monkeypatch
) -> None:
    """The pane fills on the main thread every 250 ms for the whole session.
    Line by line, a full buffer's worth measured 44 ms against 6 ms joined, so
    a burst of desk traffic stalled the window for every tick it lasted."""
    appends: list[str] = []
    append = panel._monitor.appendPlainText
    monkeypatch.setattr(
        panel._monitor, "appendPlainText",
        lambda text: (appends.append(text), append(text)),
    )
    arrive(connection, 0, 1200)
    panel._refresh()

    assert len(appends) == 1
    lines = shown(panel)
    assert lines[0].startswith("[700 lines not shown")
    assert lines[1:] == [f"packet {n}" for n in range(700, 1200)]

    panel._refresh()
    assert len(appends) == 1, "a tick with nothing new still touched the pane"
