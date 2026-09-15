"""The sACN output tab: Wer's recording status on the lighting network.

Main thread only. The sender runs on its own thread, so its status is polled on
a timer, as the other connection panels do. The adapter list is read on a
worker thread, because Windows' adapter table is slow to read.

Settings change on the show file's SacnConfig in place and are announced with
settings_changed; the window applies them to the sender and autosaves.

A new universe or network is a new stream, which the window starts by ending
the old one, so those two are announced only once the value has stopped moving
for APPLY_AFTER_MS. Keyboard tracking alone stopped typing from passing through
universes on the way (150 by way of 1 and 15), but not the arrow keys, the step
buttons or the mouse wheel, which emit on every step: holding Up from 101 to
150 ended and started 49 streams.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from wer.connections import adapters as network
from wer.connections.adapters import AdapterChoice, NetworkAdapter, choose_adapter
from wer.connections.base import ConnectionState
from wer.connections.sacn import PRIORITY_MAX, SLOT_COUNT, UNIVERSE_MAX, UNIVERSE_MIN
from wer.core.showfile import SacnConfig
from wer.ui.connection_panel import STATE_COLOURS

log = logging.getLogger(__name__)

__all__ = ["APPLY_AFTER_MS", "SacnPanel"]

#: How long the universe or network must stay put before it is applied.
APPLY_AFTER_MS = 800

#: What the status line says for each state of the sender.
_STATE_WORDS = {
    ConnectionState.DISCONNECTED: "Off",
    ConnectionState.CONNECTING: "Starting",
    ConnectionState.WAITING: "Starting",
    ConnectionState.LIVE: "Sending",
    ConnectionState.STALE: "Stalled",
    ConnectionState.ERROR: "Not sending",
}


class _AdapterAnswer(QObject):
    """Carries the worker's adapter list to the interface thread.

    Parentless and held by the worker, as record_panel._Answer is and for the
    reasons given there: the tab can be deleted while Windows is still
    answering, and Qt drops a queued call whose receiver has gone.
    """

    ready = Signal(object)


def _read_networks(
    answer: _AdapterAnswer,
    list_adapters: Callable[[], Sequence[NetworkAdapter]],
    route_source_ip: Callable[[], str | None],
) -> None:
    try:
        listed = list(list_adapters())
    except Exception:  # noqa: BLE001
        log.exception("Could not list network adapters for the sACN tab")
        listed = []
    try:
        route = route_source_ip()
    except Exception:  # noqa: BLE001
        route = None
    answer.ready.emit((listed, route))


class SacnPanel(QWidget):
    """Settings and status for the sACN status output."""

    #: The settings on the show file changed; apply them and autosave.
    settings_changed = Signal()

    def __init__(
        self,
        config: SacnConfig,
        sender,
        parent: QWidget | None = None,
        *,
        list_adapters: Callable[[], Sequence[NetworkAdapter]] = network.list_adapters,
        route_source_ip: Callable[[], str | None] = network.route_source_ip,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.sender = sender
        self._list_adapters = list_adapters
        self._route_source_ip = route_source_ip
        self._adapters: list[NetworkAdapter] = []
        self._route_ip: str | None = None
        self._listed = False
        self._listing = False
        #: The running sender's own answer last shown, so the tab follows where
        #: the stream really is, not where it was when the tab was filled.
        self._shown_choice: AdapterChoice | None = None

        self._apply_later = QTimer(self)
        self._apply_later.setSingleShot(True)
        self._apply_later.setInterval(APPLY_AFTER_MS)
        self._apply_later.timeout.connect(self._apply_now)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self._build_settings())
        layout.addWidget(self._build_status())
        layout.addStretch(1)

        self.load_settings()

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start()
        self._refresh_status()
        self.refresh_adapters()

    # ---------------------------------------------------------------- pieces

    def _build_settings(self) -> QWidget:
        box = QGroupBox("Recording status over sACN")
        column = QVBoxLayout(box)

        about = QLabel(
            "While Wer is recording, one address fades from 0 to full and back "
            "every 2 seconds. While it is not, the address sits at 0. Nothing is "
            "sent until this is switched on."
        )
        about.setWordWrap(True)
        column.addWidget(about)

        form = QFormLayout()
        self._enabled = QCheckBox("Send Wer's recording status")
        self._enabled.toggled.connect(self._enabled_changed)
        form.addRow(self._enabled)

        self._universe = QSpinBox()
        self._universe.setRange(UNIVERSE_MIN, UNIVERSE_MAX)
        self._universe.setKeyboardTracking(False)
        self._universe.setToolTip(
            "The sACN universe to send on, 1-63999; 101 to start with. A universe\n"
            "nothing else is sent on is the safe choice: see Help, sACN status output."
        )
        self._universe.valueChanged.connect(self._universe_changed)
        form.addRow("Universe:", self._universe)

        self._address = QSpinBox()
        self._address.setRange(1, SLOT_COUNT)
        self._address.setKeyboardTracking(False)
        self._address.setToolTip(
            "The address in that universe that shows Wer's status, 1-512; 1 to start with."
        )
        self._address.valueChanged.connect(self._address_changed)
        form.addRow("Address:", self._address)

        self._priority = QSpinBox()
        self._priority.setRange(1, PRIORITY_MAX)
        self._priority.setKeyboardTracking(False)
        self._priority.setToolTip(
            "sACN priority, 1-200. 100 is the default, and what a console sends\n"
            "unless told otherwise."
        )
        self._priority.valueChanged.connect(self._priority_changed)
        form.addRow("Priority:", self._priority)

        self._per_address = QCheckBox("Per-address priority")
        self._per_address.setToolTip(
            "Also send ETC per-address priorities: Wer's priority at its address\n"
            "and 0 everywhere else, so receivers that understand them give Wer\n"
            "that one address and nothing more. Without them, a receiver that\n"
            "merges by priority treats the whole universe as Wer's."
        )
        self._per_address.toggled.connect(self._per_address_changed)
        form.addRow("", self._per_address)

        row = QHBoxLayout()
        self._network = QComboBox()
        self._network.setToolTip(
            "The network card to send on. Automatic is whichever Windows\n"
            "prefers, which on a laptop is often the Wi-Fi."
        )
        self._network.currentIndexChanged.connect(self._network_changed)
        row.addWidget(self._network, 1)
        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.setToolTip("Look at this computer's networks again.")
        self._refresh_button.clicked.connect(self.refresh_adapters)
        row.addWidget(self._refresh_button)
        form.addRow("Network:", row)
        column.addLayout(form)

        self._warning = QLabel()
        self._warning.setWordWrap(True)
        self._warning.setStyleSheet("color: #e67e22;")
        self._warning.hide()
        column.addWidget(self._warning)
        return box

    def _build_status(self) -> QWidget:
        box = QGroupBox("Status")
        column = QVBoxLayout(box)
        row = QHBoxLayout()
        self._lamp = QLabel("●")
        lamp_font = QFont()
        lamp_font.setPointSize(18)
        self._lamp.setFont(lamp_font)
        row.addWidget(self._lamp)
        self._state = QLabel("Off")
        state_font = QFont()
        state_font.setBold(True)
        self._state.setFont(state_font)
        row.addWidget(self._state)
        row.addStretch(1)
        column.addLayout(row)
        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        column.addWidget(self._detail)
        return box

    # -------------------------------------------------------------- settings

    def load_settings(self) -> None:
        """Show what the settings hold, announcing nothing."""
        config = self.config
        widgets = (
            self._enabled, self._universe, self._address, self._priority,
            self._per_address, self._network,
        )
        blocked = [widget.blockSignals(True) for widget in widgets]
        try:
            self._universe.setValue(config.universe)
            self._address.setValue(config.address)
            self._priority.setValue(config.priority)
            self._per_address.setChecked(config.per_address_priority)
            self._enabled.setChecked(config.enabled)
            self._fill_networks()
        finally:
            for widget, was in zip(widgets, blocked):
                widget.blockSignals(was)
        self._update_warning()

    def _enabled_changed(self, on: bool) -> None:
        self.config.enabled = on
        self._changed()

    def _universe_changed(self, value: int) -> None:
        self.config.universe = value
        self._changed_later()

    def _address_changed(self, value: int) -> None:
        self.config.address = value
        self._changed()

    def _priority_changed(self, value: int) -> None:
        self.config.priority = value
        self._changed()

    def _per_address_changed(self, on: bool) -> None:
        self.config.per_address_priority = on
        self._changed()

    def _network_changed(self, index: int) -> None:
        key = self._network.itemData(index) or ""
        self.config.adapter = key
        if key:
            adapter = next((a for a in self._adapters if a.key == key), None)
            if adapter is not None:
                self.config.adapter_name = adapter.name
        else:
            self.config.adapter_name = ""
        self._changed_later()

    def _changed(self) -> None:
        # Anything pending goes with it: the settings are read whole.
        self._apply_later.stop()
        self._update_warning()
        self.settings_changed.emit()

    def _changed_later(self) -> None:
        self._update_warning()
        self._apply_later.start()

    def _apply_now(self) -> None:
        self.settings_changed.emit()

    def _update_warning(self, choice: AdapterChoice | None = None) -> None:
        """What to say about the settings: per-address priority off, and where
        the stream goes -- the running sender's own answer when there is one."""
        notes = []
        if not self.config.per_address_priority:
            notes.append(
                "Without per-address priority, a receiver that merges by priority "
                "treats all 512 addresses in this universe as Wer's, at this "
                "priority. If that is higher than the console's, everything else "
                "on the universe goes to 0. Use a universe nothing else is on."
            )
        if choice is None and self._listed:
            choice = choose_adapter(
                self._adapters, self.config.adapter, route_ip=self._route_ip,
                saved_name=self.config.adapter_name,
            )
        if choice is not None:
            if choice.problem:
                notes.append(choice.problem)
            elif choice.warning:
                notes.append(choice.warning)
        self._warning.setText("\n\n".join(notes))
        self._warning.setVisible(bool(notes))

    # -------------------------------------------------------------- networks

    def refresh_adapters(self) -> None:
        """Read this computer's networks again, off the interface thread."""
        if self._listing:
            return
        self._listing = True
        self._refresh_button.setEnabled(False)
        answer = _AdapterAnswer()
        answer.ready.connect(self._networks_read)
        threading.Thread(
            target=_read_networks,
            args=(answer, self._list_adapters, self._route_source_ip),
            name="wer-networks", daemon=True,
        ).start()

    def _networks_read(self, answered) -> None:
        self._adapters, self._route_ip = answered
        self._listed = True
        self._listing = False
        self._shown_choice = None
        self._refresh_button.setEnabled(True)
        was = self._network.blockSignals(True)
        try:
            self._fill_networks()
        finally:
            self._network.blockSignals(was)
        self._update_warning()

    def _fill_networks(self) -> None:
        combo = self._network
        combo.clear()
        automatic = choose_adapter(self._adapters, "", route_ip=self._route_ip)
        combo.addItem(
            f"Automatic: {automatic.label}" if automatic.ip else "Automatic", ""
        )
        for adapter in self._adapters:
            if adapter.kind != "loopback":
                combo.addItem(adapter.label, adapter.key)
        key = self.config.adapter
        if key and combo.findData(key) < 0:
            # Shown as missing, never quietly swapped for Automatic: sending on
            # a different network than the one chosen is the failure this
            # setting exists to prevent.
            name = self.config.adapter_name or "The chosen network"
            combo.addItem(f"{name} (not on this computer now)", key)
        combo.setCurrentIndex(max(0, combo.findData(key)) if key else 0)

    # ---------------------------------------------------------------- status

    def _refresh_status(self) -> None:
        sender = self.sender
        sender.refresh_staleness()
        status = sender.status
        state = status.state if sender.is_running else ConnectionState.DISCONNECTED
        colour = STATE_COLOURS.get(state, "#7f8c8d")
        self._lamp.setStyleSheet(f"color: {colour};")
        self._state.setText(_STATE_WORDS.get(state, state.value))
        self._state.setStyleSheet(f"color: {colour};")
        detail = status.detail if sender.is_running else ""
        self._detail.setText(detail)
        self._detail.setVisible(bool(detail))

        choice = getattr(sender, "choice", None) if sender.is_running else None
        if choice is not None and choice != self._shown_choice:
            self._shown_choice = choice
            if choice.automatic and choice.ip and self._network.count():
                self._network.setItemText(0, f"Automatic: {choice.label}")
            self._update_warning(choice)
