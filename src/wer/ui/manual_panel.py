"""Operator-typed fields: the Manual connection.

These have to stay editable while recording, so nothing here locks during a
take, and every keystroke publishes straight to the bus.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from wer.connections.builtin import ManualConnection

log = logging.getLogger(__name__)

__all__ = ["ManualPanel"]


class ManualPanel(QWidget):
    """A text box per field, publishing to ``manual.*`` as you type."""

    def __init__(self, connection: ManualConnection, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.connection = connection
        self._editors: dict[str, QLineEdit] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        intro = QLabel(
            "Typed fields, published to the bus as <code>manual.&lt;name&gt;</code>. "
            "Bind a text widget to one with <code>{manual.act}</code>. "
            "Editable at any time, including while recording."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(intro)

        box = QGroupBox("Fields")
        self._form = QFormLayout(box)
        for name in ManualConnection.DEFAULT_FIELDS:
            self._add_field(name)
        layout.addWidget(box)

        adder = QHBoxLayout()
        self._new_field = QLineEdit()
        self._new_field.setPlaceholderText("Add another field, e.g. 'director'")
        self._new_field.returnPressed.connect(self._add_custom_field)
        adder.addWidget(self._new_field, 1)
        add_button = QPushButton("Add")
        add_button.clicked.connect(self._add_custom_field)
        adder.addWidget(add_button)
        layout.addLayout(adder)

        layout.addStretch(1)

    def _add_field(self, name: str) -> QLineEdit:
        editor = QLineEdit()
        editor.setPlaceholderText(f"{{manual.{name}}}")
        # textChanged, not editingFinished: a designer typing a note mid-tech
        # should see it on the overlay immediately, not when focus happens to
        # move somewhere else.
        editor.textChanged.connect(
            lambda text, key=name: self.connection.set_field(key, text)
        )
        self._form.addRow(f"{name.replace('_', ' ').title()}:", editor)
        self._editors[name] = editor
        return editor

    def _add_custom_field(self) -> None:
        name = self._new_field.text().strip().lower().replace(" ", "_")
        if not name or name in self._editors:
            self._new_field.clear()
            return
        self._add_field(name).setFocus()
        self._new_field.clear()
        log.info("Manual field added: %s", name)

    def set_field(self, name: str, value: str) -> None:
        """Set a field from elsewhere, e.g. when loading a show file."""
        editor = self._editors.get(name) or self._add_field(name)
        editor.setText(value)
