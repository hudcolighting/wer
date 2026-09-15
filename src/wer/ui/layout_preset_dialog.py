"""New layout: start from a blank page or from one of the ready-made layouts.

"New..." used to ask for a name and hand back an empty layout. That suits
someone who already knows what they want on screen and where, and is a blank
wall for everyone else. This dialog puts the catalogue in
wer.overlay.layout_presets beside the blank option, each with a picture of it
drawn over a stand-in stage, so the choice is made by eye rather than from a
list of names.

The pictures come from the real compositor, fed the sample bus and drawn onto
the sample frame -- the same code that draws on a recording, so a thumbnail
shows what the layout will actually do, down to where a long label gets cut.

Nothing here opens a camera, a console or an audio device. A layout is as
likely to be set up at a desk in the afternoon as in the booth, and a dialog
that went looking for the capture card could take it from a recording that is
already running.

Main thread only.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wer.overlay.compositor import Compositor
from wer.overlay.layout import Layout
from wer.overlay.layout_presets import (
    LAYOUT_PRESETS,
    get_layout_preset,
    unique_layout_name,
)
from wer.overlay.sample import sample_bus, sample_frame
from wer.ui.widget_preview import frame_to_image

__all__ = ["LayoutPresetDialog", "choose_new_layout"]

#: The size each picture is drawn at: half 1080p. Every size in a layout is a
#: fraction of the frame, so this is the same arrangement a recording gets,
#: and the smallest text in the catalogue (Minimal's, 3% of the height) still
#: comes out at a legible 16 px.
THUMBNAIL_SIZE = (960, 540)

#: How big the picture is on screen, in logical pixels. Scaled down from the
#: render with smoothing, and drawn at the screen's own pixel density, so it
#: stays sharp on a high-DPI laptop instead of being blown up and blurred.
THUMBNAIL_SHOWN = QSize(640, 360)

_BLANK_LABEL = "Blank layout"
#: What "New..." offered before there were presets to choose from.
_BLANK_NAME = "My layout"
_BLANK_SUMMARY = "An empty layout, to fill from Add widget."
_BLANK_DESCRIPTION = (
    "Nothing on it yet. Add widgets from the Add widget menu and drag them "
    "into place on the preview."
)


def _render(key: str | None) -> np.ndarray:
    """The sample stage with this preset drawn on it, or bare for a blank layout."""
    frame = sample_frame(*THUMBNAIL_SIZE)
    preset = get_layout_preset(key) if key else None
    if preset is None:
        return frame
    # A bus of its own, never the live one: a thumbnail must have no way to put
    # a made-up cue number in front of a recording (see wer.overlay.sample).
    compositor = Compositor(sample_bus())
    compositor.apply_layout(preset.build())
    compositor.composite(frame, in_place=True)
    return frame


def _to_pixmap(frame: np.ndarray) -> QPixmap:
    """A BGR frame as a QPixmap. The copy that makes it safe is in
    frame_to_image, which the Overlay tab's live preview shares."""
    return QPixmap.fromImage(frame_to_image(frame))


class LayoutPresetDialog(QDialog):
    """Choose what a new layout starts with, and what it is called."""

    def __init__(self, existing_names: Iterable[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New layout")
        #: Taken once, as a set: the caller may hand over a generator, and the
        #: name is checked against these on every keystroke.
        self._existing = frozenset(existing_names)
        #: One picture per preset, drawn the first time it is selected.
        self._thumbnails: dict[str, QPixmap] = {}
        #: The name this dialog last filled in. While the box still says
        #: exactly that, the name is ours to change; once it says anything
        #: else, it is the operator's and a new selection leaves it alone.
        self._prefilled = ""

        self._list = QListWidget()
        self._list.setMinimumWidth(200)
        blank = QListWidgetItem(_BLANK_LABEL)
        blank.setData(Qt.ItemDataRole.UserRole, "")
        blank.setToolTip(_BLANK_SUMMARY)
        self._list.addItem(blank)
        for preset in LAYOUT_PRESETS:
            item = QListWidgetItem(preset.name)
            item.setData(Qt.ItemDataRole.UserRole, preset.key)
            item.setToolTip(preset.summary)
            self._list.addItem(item)

        self._preview = QLabel()
        self._preview.setFixedSize(THUMBNAIL_SHOWN)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet("background: #111; border: 1px solid #444;")

        self._description = QLabel()
        self._description.setWordWrap(True)
        # Room for the longest description, so choosing a different layout
        # does not make the whole dialog jump in height under the pointer.
        self._description.setMinimumHeight(self.fontMetrics().lineSpacing() * 4)
        self._description.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        self._name = QLineEdit()
        self._name.setToolTip(
            "What the layout is called on the switcher and in the show file. "
            "It has to be different from every layout already in the show."
        )
        self._problem = QLabel()
        self._problem.setStyleSheet("color:#e0a03a;")

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        # As wide as the picture and no wider. A word-wrapped label asks for
        # the width of its whole text on one line, and left to it the dialog
        # opened half as wide again as the picture, description trailing off
        # beside empty space.
        right_column = QWidget()
        right_column.setFixedWidth(THUMBNAIL_SHOWN.width())
        right = QVBoxLayout(right_column)
        right.setContentsMargins(0, 0, 0, 0)
        right.addWidget(self._preview)
        right.addWidget(self._description)
        form = QFormLayout()
        form.addRow("Name:", self._name)
        form.addRow("", self._problem)
        right.addLayout(form)
        right.addStretch(1)

        columns = QHBoxLayout()
        columns.addWidget(self._list, 1)
        columns.addWidget(right_column)

        outer = QVBoxLayout(self)
        outer.addLayout(columns, 1)
        outer.addWidget(self._buttons)

        # Connected only once everything they touch exists.
        self._list.currentItemChanged.connect(self._selection_changed)
        # Deliberately no itemDoubleClicked -> accept. Picking a preset from a
        # list is a two-click gesture for most people, and the second click
        # landing on the same row used to create the layout before they had
        # looked at the name field. OK and Enter are the ways to confirm.
        self._name.textChanged.connect(self._validate)

        self._list.setCurrentRow(0)
        self._list.setFocus()

    # ------------------------------------------------------------------ API

    def chosen_key(self) -> str | None:
        """The selected preset's key, or None for "Blank layout"."""
        item = self._list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole) or None

    def layout_name(self) -> str:
        """The name typed or offered, without the spaces either side of it."""
        return self._name.text().strip()

    def build_layout(self) -> Layout:
        """A new Layout from the chosen preset, or an empty one, called layout_name()."""
        key = self.chosen_key()
        preset = get_layout_preset(key) if key else None
        if preset is None:
            return Layout(self.layout_name(), [])
        return preset.layout(self.layout_name())

    def select(self, key: str | None) -> None:
        """Select the preset with this key, or "Blank layout" for None.

        Raises KeyError for a key the catalogue does not have, rather than
        leaving the previous choice selected and letting the caller build a
        layout it did not ask for.
        """
        wanted = key or ""
        for row in range(self._list.count()):
            if self._list.item(row).data(Qt.ItemDataRole.UserRole) == wanted:
                self._list.setCurrentRow(row)
                return
        raise KeyError(key)

    def accept(self) -> None:
        """Close with the choice made -- but never with a name that cannot be used.

        OK is disabled while the name is empty or taken, but a double-click in
        the list arrives here too, and LayoutSet.add replaces a layout of the
        same name without a word.
        """
        if self._name_problem():
            self._name.setFocus()
            return
        super().accept()

    # ------------------------------------------------------------ internals

    def _selection_changed(self, *_items: QListWidgetItem | None) -> None:
        key = self.chosen_key()
        preset = get_layout_preset(key) if key else None
        self._description.setText(preset.description if preset else _BLANK_DESCRIPTION)
        self._show_thumbnail(self._thumbnail(key))

        # Offer the preset's own name -- unless the operator has typed one, in
        # which case browsing on to look at another picture must not wipe it.
        if self.layout_name() in ("", self._prefilled):
            base = preset.name if preset else _BLANK_NAME
            self._prefilled = unique_layout_name(base, self._existing)
            self._name.setText(self._prefilled)
        self._validate()

    def _thumbnail(self, key: str | None) -> QPixmap:
        """The picture for a preset, drawn the first time it is asked for."""
        cache_key = key or ""
        pixmap = self._thumbnails.get(cache_key)
        if pixmap is None:
            pixmap = _to_pixmap(_render(key))
            self._thumbnails[cache_key] = pixmap
        return pixmap

    def _show_thumbnail(self, pixmap: QPixmap) -> None:
        ratio = self.devicePixelRatioF()
        scaled = pixmap.scaled(
            THUMBNAIL_SHOWN * ratio,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(ratio)
        self._preview.setPixmap(scaled)

    def _name_problem(self) -> str:
        """Why the name cannot be used, or "" when it can."""
        name = self.layout_name()
        if not name:
            return "Give the layout a name."
        if name in self._existing:
            return f"There is already a layout called {name!r}."
        return ""

    def _validate(self) -> None:
        problem = self._name_problem()
        self._problem.setText(problem)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not problem)


def choose_new_layout(
    parent: QWidget | None, existing_names: Iterable[str]
) -> Layout | None:
    """Ask what a new layout should start from. None if the dialog is cancelled.

    The Layout returned is new and not yet in any LayoutSet; adding it and
    switching to it is the caller's job, as it was for the old "New...".
    """
    dialog = LayoutPresetDialog(existing_names, parent)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted.value:
            return None
        return dialog.build_layout()
    finally:
        dialog.deleteLater()
