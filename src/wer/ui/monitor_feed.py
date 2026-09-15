"""A connection's traffic monitor, appended to a text pane as it arrives.

Used by the monitor panes on the Connections tab. A pane once worked out what
was new from the length of the connection's monitor buffer, and froze once
that buffer filled; MonitorFeed.update has the details.

Main thread only: it writes to widgets.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QLabel, QPlainTextEdit

from wer.connections.base import Connection, MonitorEntry

__all__ = ["MONITOR_PANE_LINES", "MonitorFeed"]

#: How many lines a monitor pane keeps before the oldest scroll away. More than
#: a connection's monitor buffer holds (500 by default), because a notice of
#: dropped lines only ever comes with a full buffer's worth after it, in the
#: same append. A pane that kept no more than the buffer would scroll the
#: notice away before anyone saw it.
MONITOR_PANE_LINES = 2000


class MonitorFeed:
    """Puts what a connection has recorded into a pane, and counts the lines.

    Keeps its place by sequence number rather than by counting lines, and keeps
    the "N lines" label beside the pane up to date.
    """

    def __init__(
        self,
        connection: Connection,
        pane: QPlainTextEdit,
        count: QLabel,
        line: Callable[[MonitorEntry], str],
    ) -> None:
        self.connection = connection
        self._pane = pane
        self._count = count
        #: Turns one entry into the text of its line; the panel decides how a
        #: line is laid out.
        self._line = line
        #: Sequence number of the last entry put in the pane: a place in
        #: everything the connection has recorded, not a count of what its
        #: buffer holds.
        self._seen = 0
        #: Lines that have reached the pane since it was last cleared,
        #: counting any the buffer dropped before they could be shown.
        self._lines = 0

    def update(self) -> None:
        """Append whatever the connection has recorded since the last call.

        Asked by position, not by length. The buffer is capped at 500 entries,
        and both panes used to take its length as the number of lines so far:
        once the buffer filled, the length stopped changing, nothing ever
        looked new again, and the pane stopped adding lines for good, within
        minutes of real traffic. The counters above it went on rising, and the
        pane's timestamps gave no sign the lines were old. Near the cap it
        skipped lines as well: with 480 shown, 30 arriving appended only the
        last 20.
        """
        entries, dropped = self.connection.monitor_since(self._seen)
        lines: list[str] = []
        if dropped:
            # More arrived between two refreshes than the buffer holds. The
            # pane cannot show them, but it must not pass for a complete record.
            lines.append(
                f"[{dropped} line{'' if dropped == 1 else 's'} not shown: "
                "more arrived between refreshes than the monitor keeps]"
            )
        lines.extend(self._line(entry) for entry in entries)
        if lines:
            # One append for the tick, not one per line. This runs on the main
            # thread on every refresh of its panel (every 250 ms on the
            # Connections tab) for the whole session, whether or not the tab
            # is showing. Measured offscreen against a full 2000-line pane:
            # 100 lines took 9.3 ms one at a time and 2.0 ms joined, and 500
            # lines 44 ms and 6.0 ms -- so a burst of traffic stalled the
            # window for every tick it lasted.
            self._pane.appendPlainText("\n".join(lines))
        if entries:
            self._seen = entries[-1].seq
        self._lines += dropped + len(entries)
        self._count.setText(f"{self._lines} lines")

    def clear(self) -> None:
        """Empty the pane, and the connection's buffer with it."""
        # _seen stays where it is. The connection remembers where it was
        # cleared, so lines that arrived just before Clear are not later
        # reported as lost.
        self.connection.clear_monitor()
        self._pane.clear()
        self._lines = 0
