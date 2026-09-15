"""The Help window: a topic list, a search box and a reading pane.

Deliberately a normal window rather than a modal dialog, so it can be left open
on a second screen while you work through setting something up -- which is when
help is actually read.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from wer import APP_DISPLAY_NAME, __version__
from wer.ui.help_content import HelpTopic, topics

log = logging.getLogger(__name__)

__all__ = ["HelpWindow"]


class HelpWindow(QWidget):
    """Browsable help for everything Wer does."""

    def __init__(self, parent: QWidget | None = None) -> None:
        # Qt.Window rather than a child widget, so it gets its own taskbar
        # entry and can sit beside the main window instead of on top of it.
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(f"{APP_DISPLAY_NAME} Help")
        self.resize(1020, 720)

        self._topics = topics()
        self._build_ui()
        self._populate()
        if self._list.count():
            self._list.setCurrentRow(0)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        top = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText(
            "Search help — try 'osc', 'marker', 'dropped frames', 'mjpg'"
        )
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter)
        top.addWidget(self._search, 1)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._list = QListWidget()
        self._list.setMinimumWidth(230)
        self._list.currentItemChanged.connect(self._show_topic)
        splitter.addWidget(self._list)

        self._body = QTextBrowser()
        self._body.setOpenExternalLinks(True)
        # Comfortable reading measure; the default is cramped for prose.
        self._body.document().setDefaultStyleSheet(
            "h2 { margin-top: 2px; }"
            "h3 { margin-top: 16px; }"
            "p, td { line-height: 140%; }"
            "code { font-family: Consolas, monospace; }"
            "table { margin: 6px 0; }"
        )
        splitter.addWidget(self._body)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([250, 770])
        layout.addWidget(splitter, 1)

        footer = QHBoxLayout()
        version = QLabel(f"{APP_DISPLAY_NAME} {__version__}")
        version.setStyleSheet("color: palette(mid);")
        footer.addWidget(version)
        footer.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        layout.addLayout(footer)

    # ------------------------------------------------------------------ list

    def _populate(self, matches: list[HelpTopic] | None = None) -> None:
        shown = self._topics if matches is None else matches
        self._list.clear()

        section = ""
        for topic in shown:
            if topic.section and topic.section != section:
                section = topic.section
                header = QListWidgetItem(section.upper())
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                font = QFont()
                font.setPointSize(8)
                font.setBold(True)
                header.setFont(font)
                self._list.addItem(header)

            item = QListWidgetItem("   " + topic.title)
            item.setData(Qt.ItemDataRole.UserRole, topic.key)
            self._list.addItem(item)

        if not shown:
            empty = QListWidgetItem("Nothing found")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(empty)

    def _filter(self, text: str) -> None:
        query = text.strip().lower()
        if not query:
            self._populate()
            if self._list.count():
                self._select_first_topic()
            return

        matches = [
            topic for topic in self._topics
            if query in topic.title.lower()
            or query in topic.body.lower()
            or any(query in keyword for keyword in topic.keywords)
        ]
        self._populate(matches)
        self._select_first_topic()

    def _select_first_topic(self) -> None:
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item.data(Qt.ItemDataRole.UserRole):
                self._list.setCurrentItem(item)
                return

    def _show_topic(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if not key:
            return
        topic = next((t for t in self._topics if t.key == key), None)
        if topic is None:
            return
        self._body.setHtml(topic.body)
        self._body.verticalScrollBar().setValue(0)

    # --------------------------------------------------------------- opening

    def goto(self, key: str) -> None:
        """Select a topic without showing the window."""
        self._search.clear()
        self._populate()
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == key:
                self._list.setCurrentItem(item)
                return
        log.warning("No help topic called %r", key)

    def show_topic(self, key: str) -> None:
        """Open the window at a particular topic.

        Used by the context-sensitive Help entries, so "Help with recording"
        lands on recording rather than on a contents page.
        """
        self.goto(key)
        self.show()
        self.raise_()
        self.activateWindow()
