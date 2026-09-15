"""The raw data monitor: a live table of every key on the DataBus.

The single most important debugging tool in the app, and built before any real
widget. It answers the question every other failure reduces to -- *is the data
arriving?* -- which cleanly separates "the console isn't talking" from "the
widget is wrong".

Runs entirely on the main thread, fed by :class:`~wer.ui.bus_bridge.BusBridge`.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QColor, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from wer.core.databus import BusEntry, DataBus

log = logging.getLogger(__name__)

__all__ = ["DataMonitorPanel"]

COL_KEY, COL_VALUE, COL_TYPE, COL_AGE, COL_SOURCE = range(5)

#: Age at which a row is dimmed even if it has no stale_after. Purely a reading
#: aid: it makes "what just changed" obvious at a glance during a tech.
RECENT_HIGHLIGHT_SECONDS = 1.0


class DataMonitorPanel(QWidget):
    """Live table of bus keys, values and ages."""

    def __init__(self, bus: DataBus, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.bus = bus
        self._rows: dict[str, int] = {}
        self._paused = False

        self._model = QStandardItemModel(0, 5, self)
        self._model.setHorizontalHeaderLabels(
            ["Key", "Value", "Type", "Age", "Source"]
        )
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._proxy.setFilterKeyColumn(COL_KEY)

        self._build_ui()

        # Ages change with no bus activity at all, so the table needs its own
        # tick. Without this a console that stops talking looks frozen rather
        # than stale, which is the exact failure that matters: old data that
        # still looks live.
        self._age_timer = QTimer(self)
        self._age_timer.setInterval(250)
        self._age_timer.timeout.connect(self._refresh_ages)
        self._age_timer.start()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        controls = QHBoxLayout()
        self._filter = QLineEdit()
        self._filter.setPlaceholderText(
            "Filter keys — try 'eos.cue' or 'clock'"
        )
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._proxy.setFilterFixedString)
        controls.addWidget(self._filter, 1)

        self._pause = QCheckBox("Pause")
        self._pause.setToolTip(
            "Stop updating the table. The bus keeps running; only this view freezes."
        )
        self._pause.toggled.connect(self._set_paused)
        controls.addWidget(self._pause)

        clear = QPushButton("Clear")
        # The table is fed by bus change notifications, so a cleared key comes
        # back when its value next changes and not before: a healthy connection
        # repeating the same value does not fire one. Unpausing resyncs from
        # the bus, which is the way to get everything back in one go.
        clear.setToolTip(
            "Forget every row. A key comes back when its value next changes, "
            "or all of them at once when Pause is unticked."
        )
        clear.clicked.connect(self.clear)
        controls.addWidget(clear)
        layout.addLayout(controls)

        self._view = QTreeView()
        self._view.setModel(self._proxy)
        self._view.setRootIsDecorated(False)
        self._view.setAlternatingRowColors(True)
        self._view.setUniformRowHeights(True)  # required for large-table speed
        self._view.setSortingEnabled(True)
        self._view.sortByColumn(COL_KEY, Qt.SortOrder.AscendingOrder)
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self._view.header()
        header.setSectionResizeMode(COL_KEY, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_VALUE, QHeaderView.ResizeMode.Stretch)
        header.resizeSection(COL_KEY, 260)
        header.resizeSection(COL_TYPE, 70)
        header.resizeSection(COL_AGE, 80)
        header.resizeSection(COL_SOURCE, 110)
        layout.addWidget(self._view, 1)

        self._summary = QLabel("No data on the bus yet.")
        layout.addWidget(self._summary)

    # ------------------------------------------------------------- updating

    def apply_batch(self, batch: dict[str, BusEntry]) -> None:
        """Apply one coalesced batch from the BusBridge. Main thread only."""
        if self._paused:
            return
        for key, entry in batch.items():
            self._upsert(key, entry)
        self._update_summary()

    def _upsert(self, key: str, entry: BusEntry) -> None:
        row = self._rows.get(key)
        if row is None:
            row = self._model.rowCount()
            self._rows[key] = row
            items = [QStandardItem() for _ in range(5)]
            items[COL_KEY].setText(key)
            for item in items:
                item.setEditable(False)
            self._model.appendRow(items)

        self._model.item(row, COL_VALUE).setText(_render(entry.value))
        self._model.item(row, COL_TYPE).setText(entry.type_name)
        self._model.item(row, COL_SOURCE).setText(entry.source_connection_id)
        self._paint_age(row, entry)

    def _paint_age(self, row: int, entry: BusEntry) -> None:
        age = entry.age()
        item = self._model.item(row, COL_AGE)
        item.setText(f"{age:.1f}s")

        key_item = self._model.item(row, COL_KEY)
        value_item = self._model.item(row, COL_VALUE)
        if entry.is_stale():
            # Stale is the state that matters most here: it is the difference
            # between "the console said 58" and "the console said 58 once, a
            # while ago, and has since gone quiet".
            colour = QColor("#c0392b")
        elif age < RECENT_HIGHLIGHT_SECONDS:
            colour = QColor("#2ecc71")
        else:
            colour = None

        for cell in (key_item, value_item, item):
            if colour is None:
                # Clearing the role is the only way back to the palette's own
                # text colour. setForeground(QColor()) does NOT do it: an
                # invalid QColor becomes an explicit BLACK brush, which is
                # invisible on a dark theme -- and this app lives on dark
                # themes in dark rooms.
                cell.setData(None, Qt.ItemDataRole.ForegroundRole)
            else:
                cell.setForeground(colour)

    def _refresh_ages(self) -> None:
        """Re-read ages straight from the bus, independent of any change."""
        if self._paused or not self._rows:
            return
        for key, row in self._rows.items():
            entry = self.bus.get(key)
            if entry is not None:
                self._paint_age(row, entry)
        self._update_summary()

    def _update_summary(self) -> None:
        total = len(self._rows)
        if not total:
            self._summary.setText("No data on the bus yet.")
            return
        stale = sum(1 for key in self._rows if self.bus.is_stale(key))
        sources = {
            entry.source_connection_id for entry in self.bus.snapshot()
        }
        text = f"{total} keys from {len(sources)} connection(s)"
        if stale:
            text += f"  —  {stale} stale"
        self._summary.setText(text)

    # -------------------------------------------------------------- actions

    def _set_paused(self, paused: bool) -> None:
        self._paused = paused
        if not paused:
            self.resync()

    def resync(self) -> None:
        """Take every row straight from the bus.

        Un-pausing needs this. Batches that arrive while paused are dropped
        rather than queued, so the table was left showing whatever each key
        held when the pause began -- and the age timer, which re-reads the
        entry but only ever repainted the Age column, then drove that stale
        row to "0.0s" in just-changed green as the console re-sent its state.
        A row claiming to be current while showing a value the desk gave up on
        is precisely the failure this panel exists to expose in everything
        else.
        """
        for entry in self.bus.snapshot():
            self._upsert(entry.key, entry)
        self._update_summary()

    def clear(self) -> None:
        self._model.removeRows(0, self._model.rowCount())
        self._rows.clear()
        self._update_summary()


def _render(value: object) -> str:
    """Render a bus value for the table.

    Floats are rounded: fade progress arrives as 0.30000001192092896 and a
    column full of that is unreadable.
    """
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if value is None:
        return ""
    return str(value)
