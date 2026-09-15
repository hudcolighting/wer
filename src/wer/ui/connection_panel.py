"""Connection status, settings, and the raw traffic monitor.

Every connection gets a visible state, time since last packet, a packets-per-
second counter, and a capped, pausable monitor of raw inbound traffic.

Main thread only. Connection callbacks arrive from worker threads, so nothing
here is wired to them directly -- the panel polls on a timer instead, which for a
status readout is simpler and just as timely as a signal.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QInputDialog,
    QMessageBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from wer.connections.base import Connection, ConnectionState, MonitorEntry
from wer.connections.eos import EosConnection, EosFraming, EosSettings, EosTransport
from wer.ui.monitor_feed import MONITOR_PANE_LINES, MonitorFeed

log = logging.getLogger(__name__)

__all__ = ["ConnectionPanel", "EosConnectionPanel", "STATE_COLOURS"]

#: Colours chosen to read in a dark room at a tech table. Green/amber/red is the
#: convention every lighting person already knows from a console's own status.
STATE_COLOURS = {
    ConnectionState.DISCONNECTED: "#7f8c8d",
    ConnectionState.CONNECTING: "#f39c12",
    ConnectionState.WAITING: "#f39c12",
    ConnectionState.LIVE: "#27ae60",
    ConnectionState.STALE: "#e67e22",
    ConnectionState.ERROR: "#c0392b",
}


def _monitor_line(entry: MonitorEntry) -> str:
    """One line of the raw monitor: when, which way, and what."""
    arrow = "<-" if entry.direction == "rx" else "->"
    return f"{entry.timestamp % 10000:9.3f} {arrow} {entry.summary}"


class ConnectionPanel(QWidget):
    """Status, start/stop, and a raw monitor for one connection."""

    #: Emitted when the user changes settings, so they can be persisted.
    settings_changed = Signal()

    def __init__(self, connection: Connection, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.connection = connection

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        settings = self._build_settings()
        if settings is not None:
            layout.addWidget(settings)

        layout.addWidget(self._build_status())
        layout.addWidget(self._build_monitor(), 1)

        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    # ---------------------------------------------------------------- pieces

    def _build_settings(self) -> QWidget | None:
        """Subclasses override to offer connection-specific settings."""
        return None

    def _build_status(self) -> QWidget:
        box = QGroupBox("Status")
        outer = QVBoxLayout(box)

        row = QHBoxLayout()
        self._lamp = QLabel("●")
        lamp_font = QFont()
        lamp_font.setPointSize(18)
        self._lamp.setFont(lamp_font)
        row.addWidget(self._lamp)

        self._state_label = QLabel("Disconnected")
        state_font = QFont()
        state_font.setBold(True)
        self._state_label.setFont(state_font)
        row.addWidget(self._state_label)
        row.addStretch(1)

        self._toggle = QPushButton("Connect")
        self._toggle.clicked.connect(self._toggle_connection)
        row.addWidget(self._toggle)
        outer.addLayout(row)

        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        outer.addWidget(self._detail)

        self._counters = QLabel("")
        self._counters.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        outer.addWidget(self._counters)
        return box

    def _build_monitor(self) -> QWidget:
        box = QGroupBox("Monitor — raw inbound traffic")
        outer = QVBoxLayout(box)

        controls = QHBoxLayout()
        self._pause = QCheckBox("Pause")
        self._pause.toggled.connect(self._set_paused)
        controls.addWidget(self._pause)

        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_monitor)
        controls.addWidget(clear)
        controls.addStretch(1)
        self._monitor_count = QLabel("")
        controls.addWidget(self._monitor_count)
        outer.addLayout(controls)

        self._monitor = QPlainTextEdit()
        self._monitor.setReadOnly(True)
        self._monitor.setMaximumBlockCount(MONITOR_PANE_LINES)
        self._monitor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._monitor.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        outer.addWidget(self._monitor, 1)
        self._monitor_feed = MonitorFeed(
            self.connection, self._monitor, self._monitor_count, _monitor_line
        )
        return box

    # --------------------------------------------------------------- actions

    def _toggle_connection(self) -> None:
        if self.connection.is_running:
            self.connection.stop()
        else:
            self.apply_settings()
            self.connection.start()
        self._refresh()

    def apply_settings(self) -> None:
        """Subclasses push UI values into the connection before it starts."""

    def load_settings(self) -> None:
        """Repopulate the widgets from the connection's current settings.

        The inverse of apply_settings, and needed the moment settings can be
        changed from anywhere but this panel -- loading a show file does exactly
        that, and without this the fields would keep showing the old values
        while the connection used the new ones.
        """

    def _set_paused(self, paused: bool) -> None:
        self.connection.monitor_paused = paused

    def _clear_monitor(self) -> None:
        self._monitor_feed.clear()

    # -------------------------------------------------------------- refresh

    def _refresh(self) -> None:
        self.connection.refresh_staleness()
        status = self.connection.status

        colour = STATE_COLOURS.get(status.state, "#7f8c8d")
        self._lamp.setStyleSheet(f"color: {colour};")
        self._state_label.setText(status.state.value)
        self._state_label.setStyleSheet(f"color: {colour};")
        self._detail.setText(status.detail)
        self._detail.setVisible(bool(status.detail))

        since = status.seconds_since_packet
        bits = [f"{status.packets} packets"]
        if status.packets:
            bits.append(f"{status.packets_per_second:.1f}/s")
        if since is not None:
            bits.append(f"last {since:.1f}s ago")
        if status.bytes_received:
            bits.append(f"{status.bytes_received / 1024:.1f} KiB")
        if status.reconnect_attempts:
            bits.append(f"{status.reconnect_attempts} reconnect attempts")
        self._counters.setText("   ".join(bits))

        self._toggle.setText(
            "Disconnect" if self.connection.is_running else "Connect"
        )
        self._monitor_feed.update()


class EosConnectionPanel(ConnectionPanel):
    """Saved consoles, plus the settings for whichever is selected."""

    #: The profile list changed and should be saved.
    profiles_changed = Signal()
    #: A different console was selected.
    profile_selected = Signal(str)
    #: The user asked Wer to go and find a console.
    find_requested = Signal()
    #: (connect on startup, try every saved console) changed.
    startup_options_changed = Signal(bool, bool)

    def __init__(self, connection: EosConnection, parent: QWidget | None = None) -> None:
        self._eos = connection
        self._profiles: list = []
        self._loading_profiles = False
        super().__init__(connection, parent)

    def _build_settings(self) -> QWidget:
        # Two groups stacked: the saved consoles, then the settings for the
        # selected one. Built as one widget because the base class expects a
        # single settings block.
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._build_console_form())
        # The profile chooser is built second because it wires itself to the
        # form's widgets, which have to exist first.
        layout.insertWidget(0, self._build_profiles())
        return container

    def _build_profiles(self) -> QWidget:
        box = QGroupBox("Saved consoles")
        outer = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("Console:"))
        self._profile_box = QComboBox()
        self._profile_box.setMinimumWidth(220)
        self._profile_box.setToolTip(
            "Saved consoles. The one used most recently is tried first when "
            "Wer opens."
        )
        self._profile_box.currentIndexChanged.connect(self._profile_selected)
        row.addWidget(self._profile_box, 1)

        for label, slot, tip in (
            ("New...", self._new_profile, "Save another console."),
            ("Duplicate...", self._duplicate_profile,
             "Copy this one under a new name — useful for a second desk on the "
             "same rig."),
            ("Rename...", self._rename_profile, ""),
            ("Delete", self._delete_profile, ""),
        ):
            button = QPushButton(label)
            if tip:
                button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
            if label == "Delete":
                self._delete_profile_button = button
        outer.addLayout(row)

        find_row = QHBoxLayout()
        self._find_button = QPushButton("Find a console")
        self._find_button.setToolTip(
            "Try every saved console in turn, most recently used first, and "
            "connect to the first one that is actually sending.\n\n"
            "A console that accepts the connection but has OSC output switched "
            "off does not count as found — that is the usual reason a desk "
            "looks connected but nothing arrives."
        )
        self._find_button.clicked.connect(self.find_requested.emit)
        find_row.addWidget(self._find_button)

        self._find_status = QLabel()
        self._find_status.setWordWrap(True)
        find_row.addWidget(self._find_status, 1)
        outer.addLayout(find_row)

        notes_row = QHBoxLayout()
        notes_row.addWidget(QLabel('Notes:'))
        self._notes = QLineEdit()
        self._notes.setPlaceholderText(
            "'booth, house left', 'needs OSC TX turning on', 'the one with the "
            "broken fader'"
        )
        self._notes.setToolTip('Free text, for you. Nothing reads it.')
        self._notes.editingFinished.connect(self._notes_changed)
        notes_row.addWidget(self._notes, 1)
        outer.addLayout(notes_row)

        self._auto_connect = QCheckBox("Connect to a console when Wer opens")
        self._auto_connect.setToolTip(
            "Walking into a tech and finding Wer already talking to the desk "
            "is the point.\n\n"
            "Nothing is dialled until a console has connected successfully at "
            "least once, so a fresh install never goes looking for an address "
            "nobody chose."
        )
        self._auto_connect.toggled.connect(self._startup_options_changed)
        outer.addWidget(self._auto_connect)

        self._try_all = QCheckBox("...and try every saved console, not just the last one")
        self._try_all.setToolTip(
            "On for a laptop that moves between venues: it finds the right desk "
            "on arrival without anyone editing the settings.\n\n"
            "Off if you only ever use one console and would rather Wer did not "
            "spend a few seconds on the others."
        )
        self._try_all.toggled.connect(self._startup_options_changed)
        outer.addWidget(self._try_all)
        return box

    def set_startup_options(self, auto_connect: bool, try_all: bool) -> None:
        for box, value in ((self._auto_connect, auto_connect),
                           (self._try_all, try_all)):
            box.blockSignals(True)
            box.setChecked(value)
            box.blockSignals(False)
        self._try_all.setEnabled(auto_connect)

    def _startup_options_changed(self) -> None:
        # Trying every console is a refinement of connecting at all, so it
        # greys out rather than sitting there looking as though it still does
        # something.
        self._try_all.setEnabled(self._auto_connect.isChecked())
        self.startup_options_changed.emit(
            self._auto_connect.isChecked(), self._try_all.isChecked()
        )

    # ------------------------------------------------------------- profiles

    def set_profiles(self, profiles: list, active_name: str) -> None:
        """Hand the panel the saved consoles from the show file."""
        self._profiles = profiles
        self._loading_profiles = True
        self._profile_box.clear()
        for profile in profiles:
            label = profile.name
            if not profile.has_connected:
                label += "  (never connected)"
            self._profile_box.addItem(label, profile.name)
        index = self._profile_box.findData(active_name)
        self._profile_box.setCurrentIndex(max(0, index))
        self._loading_profiles = False

        self._delete_profile_button.setEnabled(len(profiles) > 1)
        self._load_active_profile()

    def select_profile(self, name: str) -> None:
        """Move the dropdown to a console that was chosen elsewhere.

        Only moves the selection; it deliberately does not rebuild the list,
        because this is called from inside the dropdown's own change handler
        when the user picks one, and clearing a combo box from within that is
        asking for trouble.
        """
        index = self._profile_box.findData(name)
        if index < 0 or index == self._profile_box.currentIndex():
            self._load_active_profile()
            return
        self._loading_profiles = True
        try:
            self._profile_box.setCurrentIndex(index)
        finally:
            self._loading_profiles = False
        self._load_active_profile()

    @property
    def active_profile(self):
        name = self._profile_box.currentData()
        return next((p for p in self._profiles if p.name == name), None)

    def _load_active_profile(self) -> None:
        profile = self.active_profile
        if profile is None:
            return
        for widget in (self._host, self._transport, self._port, self._framing,
                       self._user, self._subscribe, self._notes):
            widget.blockSignals(True)
        try:
            self._host.setText(profile.host)
            index = list(EosTransport).index(EosTransport(profile.transport))
            self._transport.setCurrentIndex(index)
            self._port.setValue(profile.port)
            self._framing.setCurrentIndex(
                list(EosFraming).index(EosFraming(profile.framing))
            )
            self._user.setValue(profile.user)
            self._subscribe.setChecked(profile.subscribe)
            self._notes.setText(profile.notes)
        finally:
            for widget in (self._host, self._transport, self._port, self._framing,
                           self._user, self._subscribe, self._notes):
                widget.blockSignals(False)

    def _profile_selected(self) -> None:
        if self._loading_profiles:
            return
        self._load_active_profile()
        profile = self.active_profile
        if profile is not None:
            self.profile_selected.emit(profile.name)

    def _store_into_active(self) -> None:
        """Write the form back into the selected profile."""
        profile = self.active_profile
        if profile is None:
            return
        profile.host = self._host.text().strip() or "127.0.0.1"
        profile.transport = EosTransport(self._transport.currentData()).value
        profile.port = self._port.value()
        profile.framing = EosFraming(self._framing.currentData()).value
        profile.user = self._user.value()
        profile.subscribe = self._subscribe.isChecked()
        profile.notes = self._notes.text().strip()

    def _notes_changed(self) -> None:
        self._store_into_active()
        self.profiles_changed.emit()

    def _ask_name(self, title: str, default: str) -> str:
        name, accepted = QInputDialog.getText(self, title, "Name:", text=default)
        name = name.strip()
        if not accepted or not name:
            return ""
        if any(p.name == name for p in self._profiles):
            QMessageBox.warning(
                self, "Name already used",
                f"There is already a console called {name!r}.",
            )
            return ""
        return name

    def _new_profile(self) -> None:
        from wer.core.showfile import ConsoleProfile

        name = self._ask_name("New console", "Venue console")
        if not name:
            return
        self._profiles.append(ConsoleProfile(name=name))
        self.profiles_changed.emit()
        self.profile_selected.emit(name)

    def _duplicate_profile(self) -> None:
        from dataclasses import replace

        current = self.active_profile
        if current is None:
            return
        name = self._ask_name("Duplicate console", f"{current.name} copy")
        if not name:
            return
        # A copy has not connected under its own name yet, so it must not
        # inherit the original's place in the running order.
        self._profiles.append(replace(current, name=name, last_connected=0.0))
        self.profiles_changed.emit()
        self.profile_selected.emit(name)

    def _rename_profile(self) -> None:
        current = self.active_profile
        if current is None:
            return
        name = self._ask_name("Rename console", current.name)
        if not name:
            return
        current.name = name
        self.profiles_changed.emit()
        self.profile_selected.emit(name)

    def _delete_profile(self) -> None:
        current = self.active_profile
        if current is None or len(self._profiles) < 2:
            return
        answer = QMessageBox.question(
            self, "Delete console",
            f"Forget the console {current.name!r}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._profiles.remove(current)
        self.profiles_changed.emit()
        self.profile_selected.emit(self._profiles[0].name)

    def show_search_progress(self, message: str) -> None:
        self._find_status.setText(message)

    def set_searching(self, searching: bool) -> None:
        self._find_button.setEnabled(not searching)
        self._find_button.setText(
            "Searching..." if searching else "Find a console"
        )

    def _build_console_form(self) -> QWidget:
        box = QGroupBox("Settings for this console")
        form = QFormLayout(box)
        settings = self._eos.settings

        self._host = QLineEdit(settings.host)
        self._host.setPlaceholderText("Console IP, e.g. 10.101.90.101")
        form.addRow("Host:", self._host)

        self._transport = QComboBox()
        for transport in EosTransport:
            self._transport.addItem(transport.value, transport)
        self._transport.setCurrentIndex(
            list(EosTransport).index(settings.transport)
        )
        self._transport.currentIndexChanged.connect(self._transport_changed)
        form.addRow("Transport:", self._transport)

        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._port.setValue(settings.port)
        form.addRow("Port:", self._port)

        self._framing = QComboBox()
        for framing in EosFraming:
            self._framing.addItem(framing.value, framing)
        self._framing.setCurrentIndex(list(EosFraming).index(settings.framing))
        form.addRow("TCP framing:", self._framing)

        self._user = QSpinBox()
        self._user.setRange(0, 99)
        self._user.setValue(settings.user)
        self._user.setToolTip(
            "The OSC user Wer announces when it connects. Eos sends the "
            "ongoing cue state only to that user, so 0 (any user) is what a "
            "recording wants; a specific number silences the cue state "
            "whenever someone else is driving the desk. Cue fires still "
            "arrive, so the cue number keeps moving while the next cue and "
            "the fade time do not.\n\n"
            "Whose command line a widget shows is set per widget, with "
            "\"Command line from\" on the Overlay tab."
        )
        form.addRow("OSC user:", self._user)

        self._subscribe = QCheckBox("Send /eos/subscribe on connect")
        self._subscribe.setChecked(settings.subscribe)
        form.addRow("", self._subscribe)

        hint = QLabel(
            "Defaults are TCP 3032 with OSC 1.0 framing, which is what a "
            "default-configured Eos actually sends. OSC TX must be enabled on "
            "the console: Setup → System → Show Control → OSC."
        )
        hint.setWordWrap(True)
        form.addRow("", hint)
        return box

    def _transport_changed(self) -> None:
        """Move the port to the sensible default for the chosen transport."""
        transport = self._transport.currentData()
        self._framing.setEnabled(transport is EosTransport.TCP)
        if transport is EosTransport.UDP and self._port.value() in (3032, 3033):
            self._port.setValue(8000)
        elif transport is EosTransport.TCP and self._port.value() == 8000:
            self._port.setValue(3032)

    def load_settings(self) -> None:
        settings = self._eos.settings
        self._host.setText(settings.host)
        self._transport.setCurrentIndex(list(EosTransport).index(settings.transport))
        self._port.setValue(settings.port)
        self._framing.setCurrentIndex(list(EosFraming).index(settings.framing))
        self._user.setValue(settings.user)
        self._subscribe.setChecked(settings.subscribe)

    def apply_settings(self) -> None:
        self._store_into_active()
        self.profiles_changed.emit()
        self._eos.settings = EosSettings(
            host=self._host.text().strip() or "127.0.0.1",
            transport=self._transport.currentData(),
            port=self._port.value(),
            framing=self._framing.currentData(),
            user=self._user.value(),
            subscribe=self._subscribe.isChecked(),
        )
        log.info("Eos settings applied: %s", self._eos.settings)
        self.settings_changed.emit()
