"""Layout editor: add, remove, arrange and restyle overlay widgets.

Placement, formatting and visibility are all editable, without a restart.
Everything here edits the live compositor, so a change is on the preview the
moment it is made -- which is the only way to judge whether a cue readout is
legible over an actual stage.

Main thread only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QInputDialog,
    QMessageBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from wer.overlay.geometry import (
    Anchor,
    Placement,
    Rect,
    clamped_placement,
    drag_placement,
)
from wer.overlay.presets import make_widget, preset_groups, unique_id
from wer.overlay.style import Align, Colour, TextStyle
from wer.overlay.widgets import (
    CUE_TIMER_STARTS,
    CueTimerWidget,
    CueWidget,
    FadeBarWidget,
    ImageWidget,
    OverlayWidget,
    PanelWidget,
    StatusWidget,
    TextWidget,
)
from wer.ui.help_content import BUS_KEY_GROUPS
from wer.ui.widget_preview import LayoutPreview

log = logging.getLogger(__name__)

__all__ = ["LayoutEditor", "GRID_SIZES"]

#: The property sheet's caption, and the same caption when it is warning.
_CAPTION = "color:#9aa0aa; font-style: italic;"
_WARNING = "color:#e0a03a; font-style: italic;"

#: Weights offered for text: (label, QFont weight).
_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("Regular", 400),
    ("Semibold", 600),
    ("Bold", 700),
    ("Black", 900),
)

#: When a widget appears: (label, the VisibilityRule field it sets).
_CONDITIONS: tuple[tuple[str, str | None], ...] = (
    ("Always", None),
    ("While a key has a value", "only_when_present"),
    ("While a key is true", "only_when"),
)

#: Style rows that mean something for anything drawn as text.
_TEXT_STYLE_ROWS = frozenset({
    "font", "size", "weight", "align", "box", "box_colour", "box_padding",
    "box_radius", "outline", "shadow",
})

#: Which optional rows of the property sheet each kind of widget shows. A row
#: not named here for a kind is hidden for it: a lamp's Off text beside a
#: picture is a question nobody should be asked. Rows every widget has --
#: corner, nudges, layer, opacity, visibility -- are not listed.
_ROWS_FOR_KIND: dict[str, frozenset[str]] = {
    "text": _TEXT_STYLE_ROWS | {"content", "emphasise", "colour", "max_width"},
    "cue": _TEXT_STYLE_ROWS | {"cue_options", "colour", "max_width"},
    "lamp": _TEXT_STYLE_ROWS | {
        "content", "lamp_key", "off_text", "lamp_colours", "lamp_dot",
        "lamp_hide", "max_width",
    },
    "picture": frozenset({"image_file", "image_height", "image_opacity"}),
    "panel": frozenset({"panel_width", "panel_height", "box_colour", "box_radius"}),
    "fadebar": frozenset({"bar_width", "bar_height", "bar_idle", "colour"}),
    "cuetimer": _TEXT_STYLE_ROWS | {"timer_from", "timer_prefix", "colour", "max_width"},
}

#: The grids offered while moving widgets: (label, (columns, rows)). Each one
#: has square cells on a 16:9 picture -- 120, 60 and 30 px at 1080p.
GRID_SIZES: tuple[tuple[str, tuple[int, int]], ...] = (
    ("Coarse (16 × 9)", (16, 9)),
    ("Medium (32 × 18)", (32, 18)),
    ("Fine (64 × 36)", (64, 36)),
)



#: Both shapes of command-line key: the any-user one, and one user's own.
_ANY_CMDLINE = "eos.cmdline."
_USER_CMDLINE = re.compile(r"eos\.cmdline\.user\.(\d+)\.")


def command_line_user(template: str) -> int | None:
    """Which user a template's command line follows, or None if it has none.

    0 means "any user" -- the generic eos.cmdline.* keys, which carry whichever
    command line changed most recently.
    """
    match = _USER_CMDLINE.search(template)
    if match is not None:
        return int(match.group(1))
    if _ANY_CMDLINE in template:
        return 0
    return None


def retarget_command_line(template: str, user: int) -> str:
    """Point every command-line key in a template at a different user."""
    generic = _USER_CMDLINE.sub(_ANY_CMDLINE, template)
    if user == 0:
        return generic
    return generic.replace(_ANY_CMDLINE, f"eos.cmdline.user.{user}.")


class LayoutEditor(QWidget):
    """The widget list plus a property sheet for whatever is selected."""

    #: The layout changed in some way worth saving.
    layout_changed = Signal()
    #: Selection changed, so the preview can highlight it.
    selection_changed = Signal(str)
    #: Edit mode was switched on or off.
    edit_mode_changed = Signal(bool)
    #: A different layout should be made live.
    layout_switch_requested = Signal(str)
    #: Layouts were added, renamed or removed.
    layout_set_changed = Signal()
    #: The guides a dragged widget snapped to, and the anchor it would take,
    #: for the preview to draw: (tuple of geometry.Guide, anchor label).
    snap_feedback = Signal(object, str)
    #: The grid drawn while widgets move: (columns, rows).
    grid_changed = Signal(int, int)
    #: Snapping or grid preferences changed, so the show file wants saving.
    editor_settings_changed = Signal()

    def __init__(self, compositor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.compositor = compositor
        self._current: OverlayWidget | None = None
        self._layouts = None
        self._loading = False
        #: Which VisibilityRule field the "Only when:" box is editing for the
        #: selected widget. Set when its properties are loaded.
        self._only_when_field = "only_when_present"
        #: The widget being dragged on the preview and the placement it had
        #: when the button went down, which every drag event is measured from.
        self._drag: tuple[OverlayWidget, Placement] | None = None
        #: The show file's EditorConfig, once the main window hands it over.
        self._editor_config = None
        #: Optional property-sheet rows by name, as (label, field), so a whole
        #: row can be shown or hidden for the kind of widget selected.
        self._rows: dict[str, tuple[QWidget, QWidget]] = {}
        #: The look copied with Copy style, waiting to be pasted.
        self._style_clipboard: TextStyle | None = None
        #: How far _clear_of_others pushed a widget to keep it off the others,
        #: by widget id, as (the widget itself, the addition to offset_y). The
        #: widget is held with it so a push can never be taken off a different
        #: layout's widget that happens to share an id. In memory only, and
        #: never saved: after a reload every offset in the show file is the
        #: user's own, and Wer has nothing of its own to take back.
        self._pushes: dict[str, tuple[OverlayWidget, float]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self._build_layout_bar())
        layout.addWidget(self._build_toolbar())

        columns = QHBoxLayout()
        columns.addWidget(self._build_list(), 1)
        columns.addWidget(self._build_properties(), 2)
        layout.addLayout(columns, 1)

        self.refresh()

    def _build_layout_bar(self) -> QWidget:
        box = QGroupBox("Layout")
        row = QHBoxLayout(box)

        row.addWidget(QLabel("Editing:"))
        self._layout_box = QComboBox()
        self._layout_box.setMinimumWidth(160)
        self._layout_box.setToolTip(
            "The layout being edited, which is also the one on screen.\n"
            "Switch with Ctrl+1..9, or cycle with Ctrl+L."
        )
        self._layout_box.currentIndexChanged.connect(self._layout_box_changed)
        row.addWidget(self._layout_box)

        new = QPushButton("New...")
        new.setToolTip(
            "Start a layout from one of the ready-made ones, with a picture "
            "of each, or from nothing."
        )
        new.clicked.connect(self._new_layout)
        row.addWidget(new)

        duplicate = QPushButton("Duplicate...")
        duplicate.setToolTip(
            "Copy this layout under a new name. The usual way to make your own: "
            "start from Tech and change what you do not want."
        )
        duplicate.clicked.connect(self._duplicate_layout)
        row.addWidget(duplicate)

        rename = QPushButton("Rename...")
        rename.clicked.connect(self._rename_layout)
        row.addWidget(rename)

        self._delete_layout_button = QPushButton("Delete")
        self._delete_layout_button.clicked.connect(self._delete_layout)
        row.addWidget(self._delete_layout_button)

        reset = QPushButton("Reset to default")
        reset.setToolTip(
            "Put a ready-made layout back the way it ships. Offered for any "
            "layout named after one: Tech, Minimal, Programming and the rest."
        )
        reset.clicked.connect(self._reset_layout)
        self._reset_button = reset
        row.addWidget(reset)

        row.addStretch(1)
        return box

    # ------------------------------------------------------------- layout set

    def set_layouts(self, layouts) -> None:
        """Give the editor the show's LayoutSet so it can manage it."""
        self._layouts = layouts
        self.refresh_layouts()

    def refresh_layouts(self) -> None:
        if self._layouts is None:
            return
        self._loading = True
        self._layout_box.clear()
        for name in self._layouts.names:
            self._layout_box.addItem(name)
        index = self._layout_box.findText(self._layouts.active_name)
        self._layout_box.setCurrentIndex(max(0, index))
        self._loading = False

        many = len(self._layouts) > 1
        self._delete_layout_button.setEnabled(many)
        from wer.overlay.layout_presets import layout_preset_named

        self._reset_button.setEnabled(
            layout_preset_named(self._layouts.active_name) is not None
        )

    def _layout_box_changed(self) -> None:
        if self._loading or self._layouts is None:
            return
        name = self._layout_box.currentText()
        if name and name != self._layouts.active_name:
            self.layout_switch_requested.emit(name)

    def _ask_name(self, title: str, default: str) -> str:
        name, accepted = QInputDialog.getText(self, title, "Name:", text=default)
        name = name.strip()
        if not accepted or not name:
            return ""
        if self._layouts is not None and self._layouts.get(name) is not None:
            QMessageBox.warning(
                self, "Name already used",
                f"There is already a layout called {name!r}.",
            )
            return ""
        return name

    def _new_layout(self) -> None:
        """Start a layout from a ready-made one, or from nothing.

        New used to ask for a name and hand back an empty layout, so the way to
        anything useful was Duplicate on Tech and then taking things away.
        """
        if self._layouts is None:
            return
        from wer.ui.layout_preset_dialog import choose_new_layout

        layout = choose_new_layout(self, self._layouts.names)
        if layout is None:
            return
        self._layouts.add(layout)
        self.layout_set_changed.emit()
        self.layout_switch_requested.emit(layout.name)

    def _duplicate_layout(self) -> None:
        if self._layouts is None:
            return
        current = self._layouts.active
        if current is None:
            return
        name = self._ask_name("Duplicate layout", f"{current.name} copy")
        if not name:
            return
        from wer.overlay.layout import Layout, widget_from_dict, widget_to_dict

        # Round-tripped through the serialiser so the copy shares no objects
        # with the original -- otherwise editing one would edit both, which is
        # a memorable way to ruin a layout you were happy with.
        copies = []
        for widget in current.widgets:
            rebuilt = widget_from_dict(widget_to_dict(widget))
            if rebuilt is not None:
                copies.append(rebuilt)
        self._layouts.add(Layout(name, copies))
        self.layout_set_changed.emit()
        self.layout_switch_requested.emit(name)

    def _rename_layout(self) -> None:
        if self._layouts is None:
            return
        current = self._layouts.active
        if current is None:
            return
        name = self._ask_name("Rename layout", current.name)
        if not name:
            return
        current.name = name
        self._layouts._active = name
        self.layout_set_changed.emit()
        self.refresh_layouts()

    def _delete_layout(self) -> None:
        if self._layouts is None or len(self._layouts) < 2:
            return
        name = self._layouts.active_name
        answer = QMessageBox.question(
            self, "Delete layout",
            f"Delete the layout {name!r}? The widgets in it are lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        # By value, never by identity: the dialog answers with a plain int in
        # this PySide6, not the enum member, and "is not Yes" was always true.
        # Delete showed its confirmation, took the Yes, and did nothing, with
        # nothing in the log to say so.
        if answer != QMessageBox.StandardButton.Yes:
            log.info("Delete of layout %r declined", name)
            return
        was_active = self._layouts.active_name
        self._layouts.remove(name)
        log.info("Deleted layout %r; left with %s", name, ", ".join(self._layouts.names))
        # Redrawn here, not left to the switch below. The box, the Delete
        # button and Reset all describe a set that has just changed, and a
        # delete that leaves the active layout where it was emits no switch at
        # all -- so nothing else would come back to redraw them, and the box
        # went on offering a layout that had gone.
        self.refresh_layouts()
        self.layout_set_changed.emit()
        if self._layouts.active_name != was_active:
            self.layout_switch_requested.emit(self._layouts.active_name)

    def _reset_layout(self) -> None:
        if self._layouts is None:
            return
        name = self._layouts.active_name
        from wer.overlay.layout_presets import layout_preset_named

        preset = layout_preset_named(name)
        if preset is None:
            QMessageBox.information(
                self, "No default",
                f"{name!r} is your own layout, so there is nothing to reset it to.",
            )
            return
        answer = QMessageBox.question(
            self, "Reset layout",
            f"Put {name!r} back the way it ships? Your changes to it are lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._layouts.add(preset.layout(name))
        self.layout_set_changed.emit()
        self.layout_switch_requested.emit(name)

    # ---------------------------------------------------------------- toolbar

    def _build_toolbar(self) -> QWidget:
        box = QGroupBox("Editing")
        row = QHBoxLayout(box)

        self._edit_mode = QCheckBox("Edit on preview")
        self._edit_mode.setToolTip(
            "Outlines every widget on the Preview tab and lets you drag them "
            "into place. The overlay keeps working while you do."
        )
        self._edit_mode.toggled.connect(self.edit_mode_changed.emit)
        row.addWidget(self._edit_mode)

        self._snap = QCheckBox("Snap")
        self._snap.setChecked(True)
        self._snap.setToolTip(
            "While dragging, pull a widget onto the frame's margins and centre "
            "lines and into line with the other widgets -- including just "
            "below or beside one, so a date can sit under a clock.\n\n"
            "Hold Alt while dragging to place a widget freely."
        )
        row.addWidget(self._snap)

        self._snap_grid = QCheckBox("Snap to grid")
        self._snap_grid.setToolTip(
            "Also pull widgets onto the grid lines drawn while one is moving.\n\n"
            "Off by default: with it on every widget lands on a line, which is "
            "tidy but fights you when you want something just so."
        )
        row.addWidget(self._snap_grid)

        row.addWidget(QLabel("Grid:"))
        self._grid_size = QComboBox()
        for label, _size in GRID_SIZES:
            self._grid_size.addItem(label)
        self._grid_size.setCurrentIndex(1)
        self._grid_size.setToolTip(
            "How fine the grid drawn while moving widgets is. Shift with an "
            "arrow key moves the selected widget one square."
        )
        row.addWidget(self._grid_size)

        # Connected only once all three exist, because each handler reads the
        # other two.
        self._snap.toggled.connect(self._snap_settings_changed)
        self._snap_grid.toggled.connect(self._snap_settings_changed)
        self._grid_size.currentIndexChanged.connect(self._snap_settings_changed)

        add = QPushButton("Add widget...")
        add.clicked.connect(self._show_add_menu)
        row.addWidget(add)

        self._duplicate = QPushButton("Duplicate")
        self._duplicate.clicked.connect(self._duplicate_selected)
        row.addWidget(self._duplicate)

        self._remove = QPushButton("Remove")
        self._remove.clicked.connect(self._remove_selected)
        row.addWidget(self._remove)
        row.addStretch(1)
        return box

    # ------------------------------------------------------------- snapping

    @property
    def grid(self) -> tuple[int, int]:
        """The grid chosen for moving widgets, as (columns, rows)."""
        return GRID_SIZES[max(0, self._grid_size.currentIndex())][1]

    def set_editor_config(self, config) -> None:
        """Take the show file's editor preferences, and keep them up to date."""
        self._editor_config = None
        self._snap.setChecked(bool(config.snap))
        self._snap_grid.setChecked(bool(config.snap_to_grid))
        sizes = [size for _label, size in GRID_SIZES]
        wanted = (config.grid_columns, config.grid_rows)
        # A hand-edited grid Wer does not offer falls back to the default,
        # rather than the box naming one grid while another is drawn.
        self._grid_size.setCurrentIndex(sizes.index(wanted) if wanted in sizes else 1)
        self._editor_config = config
        self._snap_settings_changed()

    def _snap_settings_changed(self) -> None:
        # Grid snapping is part of snapping: with Snap off nothing snaps, and a
        # ticked box that did nothing would say otherwise.
        self._snap_grid.setEnabled(self._snap.isChecked())
        self.grid_changed.emit(*self.grid)
        config = self._editor_config
        if config is None:
            return
        wanted = (self._snap.isChecked(), self._snap_grid.isChecked(), self.grid)
        if wanted == (
            config.snap, config.snap_to_grid, (config.grid_columns, config.grid_rows)
        ):
            return
        config.snap, config.snap_to_grid, (config.grid_columns, config.grid_rows) = wanted
        self.editor_settings_changed.emit()

    def _show_add_menu(self) -> None:
        menu = QMenu(self)
        for group, presets in preset_groups().items():
            submenu = menu.addMenu(group)
            for preset in presets:
                action = submenu.addAction(preset.label)
                tip = preset.description
                if preset.reads:
                    tip += "\n\nReads: " + ", ".join(preset.reads)
                action.setToolTip(tip)
                action.triggered.connect(
                    lambda _=False, key=preset.key: self._add(key)
                )
        menu.setToolTipsVisible(True)
        menu.exec(self.cursor().pos())

    def _add(self, preset_key: str) -> None:
        existing = [w.id for w in self.compositor.widgets]
        widget = make_widget(preset_key, existing)
        if widget is None:
            return
        layers = [w.z_order for w in self.compositor.widgets]
        if isinstance(widget, PanelWidget):
            # A shape is for putting other widgets on, so it goes under them.
            # Put on top like everything else, a new backing strip covered the
            # very widgets it was added to sit behind.
            widget.z_order = min(layers, default=0) - 10
        else:
            # On top, so a newly added widget is not hidden behind something.
            widget.z_order = max(layers, default=0) + 10
            self._clear_of_others(widget)
        self.compositor.add(widget)
        self.refresh()
        self._select_by_id(widget.id)
        self.layout_changed.emit()
        log.info("Added widget %s from preset %s", widget.id, preset_key)

    def _clear_of_others(self, widget: OverlayWidget) -> None:
        """Move a widget off whatever is already in the corner it is going to.

        Every catalogue entry comes with a corner, and corners fill up: add a
        date to a layout with a clock and both want the top right. Put down
        where it came, the new widget sat exactly on the old one. The tech
        layout it was reported from had its date exactly over its clock and
        its REC lamp exactly over the elapsed time, offsets zero, which is
        where the catalogue put them and where the old snapping put back
        anything dragged near. So it is stacked away from the edge it hangs
        from instead, just past what it would cover, with the gap a drag
        snaps to.

        Run when a widget is added and again when its Corner is changed, which
        are the same act: the widget is being sent to that corner, and it
        belongs in it rather than on top of its occupant. However far the
        widget is moved is remembered in ``_pushes``, so the next corner change
        can take it off again -- an automatic push is Wer's, not the user's,
        and carrying it to an empty corner leaves the widget hanging in mid-air.

        Only while the picture is running, because without a frame there are
        no sizes to work from. Shapes are left out: a widget added over a
        backing strip is meant to be on it.

        A shape is never the widget being moved either. _add does not clear a
        new backing strip off the readouts it is added behind, and a corner
        change is the same act: cleared, a strip sent to the corner its
        readouts are in slid out from under the very things it is there to
        back, and the operator would have had to drag it home by hand.
        """
        from wer.overlay.geometry import STACK_GAP

        # A shape is put where it is put, and the widgets on it stay on it.
        if isinstance(widget, PanelWidget):
            return
        frame = self.compositor.frame_size
        if frame is None:
            return
        others = self._other_rects(widget.id, frame, shapes=False)
        if not others:
            return
        width, height = frame
        if self.compositor.get(widget.id) is widget:
            # Already on the picture -- a corner change, not an add -- so its
            # size is the one the compositor last drew. Painting it here would
            # be the main thread painting a widget the capture thread may be
            # painting at the same moment, which is the very thing _other_rects
            # avoids; and a widget that has not drawn yet has no size to work
            # from, so it is left where it is until it has one.
            size = self.compositor.widget_size(widget.id)
            if size is None:
                return
        else:
            # Drawn here to learn its size. It is not in the compositor yet, so
            # the capture thread cannot be drawing it at the same moment.
            image = widget.image(self.compositor.bus, height, width)
            if image is None or image.isNull():
                return
            size = (image.width(), image.height())
        started_at = widget.placement.offset_y
        gap = STACK_GAP * height
        upwards = widget.placement.anchor.vertical >= 1.0

        placement = widget.placement
        for _attempt in range(len(others) + 1):
            rect = placement.resolve(width, height, *size)
            covered = [other for other in others if other.intersects(rect)]
            if not covered:
                break
            if upwards:
                target = min(other.y for other in covered) - gap - size[1]
            else:
                target = max(other.bottom for other in covered) + gap
            placement = replace(
                placement, offset_y=placement.offset_y + (target - rect.y) / height
            )
        widget.placement = clamped_placement(placement, width, height, *size)
        # The clamp is part of the push: undoing the one has to undo the other,
        # or a widget pushed against the bottom of the frame comes back short.
        pushed = widget.placement.offset_y - started_at
        if pushed:
            self._pushes[widget.id] = (widget, pushed)
        else:
            self._forget_push(widget.id)

    def _remembered_push(self, widget: OverlayWidget) -> float:
        """How far _clear_of_others last pushed this very widget, or 0.0."""
        held = self._pushes.get(widget.id)
        return held[1] if held is not None and held[0] is widget else 0.0

    def _forget_push(self, widget_id: str) -> None:
        """Stop treating a widget's offset as Wer's doing.

        Called wherever the operator sets the position themselves -- typing a
        nudge, dragging, the arrow keys, duplicating. From then on the number
        is theirs, and no corner change may quietly take a slice off it.
        """
        self._pushes.pop(widget_id, None)

    def _duplicate_selected(self) -> None:
        if self._current is None:
            return
        from wer.overlay.layout import widget_from_dict, widget_to_dict

        data = widget_to_dict(self._current)
        data["id"] = unique_id(
            self._current.id, [w.id for w in self.compositor.widgets]
        )
        # Nudged, so the copy is visible rather than exactly behind the original.
        placement = data.setdefault("placement", {})
        placement["offset_x"] = placement.get("offset_x", 0.0) + 0.02
        placement["offset_y"] = placement.get("offset_y", 0.0) + 0.03
        copy = widget_from_dict(data)
        if copy is None:
            return
        # The copy is placed deliberately, by the nudge just above, so its
        # offsets are the user's from the moment it exists -- nothing here for
        # a later corner change to take back off it.
        self._forget_push(copy.id)
        self.compositor.add(copy)
        self.refresh()
        self._select_by_id(copy.id)
        self.layout_changed.emit()

    def _remove_selected(self) -> None:
        if self._current is None:
            return
        removed = self._current.id
        self.compositor.remove(removed)
        # _current is left for refresh() to drop, because that is also what
        # tells the preview to stop outlining it. Cleared here instead, the
        # selection looked empty to refresh(), nothing was emitted, and the
        # edit-mode outline stayed on the picture over a widget that had gone.
        self.refresh()
        self.layout_changed.emit()
        log.info("Removed widget %s", removed)

    # ------------------------------------------------------------------- list

    def _build_list(self) -> QWidget:
        box = QGroupBox("Widgets in this layout")
        column = QVBoxLayout(box)

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._selection_changed)
        column.addWidget(self._list)

        row = QHBoxLayout()
        up = QPushButton("Bring forward")
        up.clicked.connect(lambda: self._reorder(+1))
        row.addWidget(up)
        down = QPushButton("Send back")
        down.clicked.connect(lambda: self._reorder(-1))
        row.addWidget(down)
        column.addLayout(row)
        return box

    def _reorder(self, direction: int) -> None:
        if self._current is None:
            return
        self._current.z_order += direction * 15
        # Re-sort, not re-add: adding it again used to leave a second copy of
        # the widget in the layout, blended twice and autosaved into the show.
        self.compositor.resort()
        self.refresh()
        self._select_by_id(self._current.id)
        self.layout_changed.emit()

    def refresh(self) -> None:
        """Rebuild the list from the compositor."""
        selected = self._current.id if self._current else ""
        # Remembered pushes belong to the widgets on the picture. A widget
        # removed, or a whole layout made live, leaves entries behind that
        # nothing will ever look at again -- and every layout has a "cue".
        self._pushes = {
            widget_id: held
            for widget_id, held in self._pushes.items()
            if self.compositor.get(widget_id) is held[0]
        }
        self._loading = True
        self._list.clear()
        for widget in self.compositor.widgets:
            locked = "  - locked" if widget.locked else ""
            item = QListWidgetItem(f"{widget.id}    ({_kind(widget)}){locked}")
            item.setData(Qt.ItemDataRole.UserRole, widget.id)
            if not widget.visible:
                item.setForeground(QColor(127, 140, 141))
            self._list.addItem(item)
        self._loading = False
        if selected and self.compositor.get(selected) is not None:
            self._select_by_id(selected)
        elif selected:
            # The selected widget is not in this compositor any more -- it was
            # removed, or a different layout has just been made live. Clearing
            # the list above does not say so, because _selection_changed is
            # muted while the list is rebuilt, so _current went on pointing at
            # a widget nothing was drawing: Remove looked available and did
            # nothing at all, and the property sheet quietly edited a widget in
            # the layout you had just left. The preview is told too, or it goes
            # on outlining something that is no longer on the picture.
            self._current = None
            self._load_properties()
            self.selection_changed.emit("")
        self._update_enabled()
        # The widget set itself may have changed -- a layout made live, a
        # widget added or removed -- and that never goes through
        # layout_changed on its own.
        self.request_preview()

    def _select_by_id(self, widget_id: str) -> None:
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == widget_id:
                self._list.setCurrentItem(item)
                return

    def _selection_changed(self, item: QListWidgetItem | None) -> None:
        if self._loading:
            return
        widget_id = item.data(Qt.ItemDataRole.UserRole) if item else ""
        self._current = self.compositor.get(widget_id) if widget_id else None
        self._load_properties()
        self._update_enabled()
        self.selection_changed.emit(widget_id or "")

    def select(self, widget_id: str) -> None:
        """Called when something is clicked on the preview."""
        if widget_id:
            self._select_by_id(widget_id)
        else:
            self._list.setCurrentItem(None)
            self._current = None
            self._load_properties()
            self._update_enabled()

    def _update_enabled(self) -> None:
        has = self._current is not None
        self._remove.setEnabled(has)
        self._duplicate.setEnabled(has)
        self._properties.setEnabled(has)

    def request_preview(self) -> None:
        """Redraw the little picture of the layout, soon.

        Coalesced and free while the Overlay tab is away, so it is safe to call
        from anywhere that might have changed what the layout looks like.
        """
        self.preview.request_render()

    # ------------------------------------------------------------- properties

    def _build_properties(self) -> QWidget:
        """What the selected widget says, where it sits, how it looks, and when.

        Four tabs, because a widget now has getting on for forty settings and
        one long column of them was a scroll past the one you wanted. Within
        each tab only the rows that mean something for the selected kind of
        widget are shown.

        The preview sits above the tabs and outside the scroll area, for two
        reasons. It has to stay put while any of the four tabs is open --
        Content and Style are both judged by looking -- and it must not be
        greyed out with the sheet when nothing is selected, because a layout is
        still worth looking at then.
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._properties = QWidget()
        column = QVBoxLayout(self._properties)
        column.setContentsMargins(4, 4, 4, 4)

        self._what = QLabel()
        self._what.setWordWrap(True)
        self._what.setStyleSheet(_CAPTION)
        column.addWidget(self._what)

        self._property_tabs = QTabWidget()
        self._property_tabs.addTab(self._build_content_tab(), "Content")
        self._property_tabs.addTab(self._build_position_tab(), "Position")
        self._property_tabs.addTab(self._build_style_tab(), "Style")
        self._property_tabs.addTab(self._build_visibility_tab(), "Visibility")
        column.addWidget(self._property_tabs, 1)

        scroll.setWidget(self._properties)

        holder = QWidget()
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        self.preview = LayoutPreview()
        self.preview.set_source(self.compositor)
        outer.addWidget(self.preview)
        outer.addWidget(scroll, 1)

        # Through the signals the editor already emits rather than a call at
        # every edit site: _apply, _place, _add and the rest all end in
        # layout_changed, so anything that changes the picture redraws it, and
        # nothing new has to remember to.
        self.layout_changed.connect(self.preview.request_render)
        self.selection_changed.connect(self.preview.set_selected)
        return holder

    def _row(self, form: QFormLayout, name: str, label: str, field: QWidget) -> QWidget:
        """Add an optional row, registered so it can be hidden whole."""
        caption = QLabel(label)
        form.addRow(caption, field)
        self._rows[name] = (caption, field)
        return field

    def _build_content_tab(self) -> QWidget:
        page, form = _form_page()

        self._content_label = QLabel("Content:")
        self._content = _TemplateEdit()
        self._content.setToolTip(
            "Anything in braces is a bus key, resolved live: "
            "Cue {eos.cue.active.number}\n"
            "Press Enter for a second line -- a clock with the date under it "
            "is one widget, so the two can never drift apart or overlap.\n"
            "Insert key lists every key, including what is on the bus now."
        )
        self._content.textChanged.connect(
            lambda: self._content_changed(self._content.text())
        )
        content = _column_widget(
            self._content, _row_widget(self._key_button(self._insert_into_content))
        )
        form.addRow(self._content_label, content)
        self._rows["content"] = (self._content_label, content)

        # Only meaningful for a command-line widget, so it hides itself for
        # everything else rather than sitting there greyed out.
        self._cmd_user_label = QLabel("Command line from:")
        self._cmd_user = QComboBox()
        self._cmd_user.addItem("Any user", 0)
        for number in range(1, 13):
            self._cmd_user.addItem(f"User {number}", number)
        self._cmd_user.setToolTip(
            "Whose command line this widget shows.\n\n"
            "Any user follows whichever operator typed most recently, which is "
            "usually what a tech recording wants. Pick a number to pin it to "
            "one of them -- useful when a programmer and a designer are both "
            "on the desk and you only want to see one."
        )
        self._cmd_user.currentIndexChanged.connect(self._cmd_user_changed)
        form.addRow(self._cmd_user_label, self._cmd_user)

        self._emphasise = QCheckBox("Lines after the first are smaller")
        self._emphasise.setToolTip(
            "For a widget of several lines, so the first stays the one the eye "
            "lands on: the time large, the date beneath it small."
        )
        self._emphasise.toggled.connect(self._emphasise_changed)
        self._row(form, "emphasise", "", self._emphasise)

        # -- lamp
        self._lamp_key = QLineEdit()
        self._lamp_key.setPlaceholderText("e.g. eos.connected")
        self._lamp_key.setToolTip(
            "The bus key the lamp follows. On while it is true -- or, for "
            "text, anything but empty, 0, false, no or off."
        )
        self._lamp_key.editingFinished.connect(self._lamp_changed)
        self._row(form, "lamp_key", "Lamp follows:", _row_widget(
            self._lamp_key, self._key_button(self._insert_lamp_key), stretch=False
        ))
        self._off_text = QLineEdit()
        self._off_text.setToolTip("What the lamp says while the key is off. A template.")
        self._off_text.textChanged.connect(lambda _text: self._lamp_changed())
        self._row(form, "off_text", "Off text:", self._off_text)
        self._on_swatch = _swatch()
        self._off_swatch = _swatch()
        on_button = QPushButton("On colour...")
        on_button.clicked.connect(lambda: self._choose_lamp_colour("on"))
        off_button = QPushButton("Off colour...")
        off_button.clicked.connect(lambda: self._choose_lamp_colour("off"))
        self._row(form, "lamp_colours", "Colours:", _row_widget(
            self._on_swatch, on_button, self._off_swatch, off_button
        ))
        self._show_dot = QCheckBox("A dot before the text")
        self._show_dot.toggled.connect(lambda _on: self._lamp_changed())
        self._row(form, "lamp_dot", "", self._show_dot)
        self._hide_when_off = QCheckBox("Draw nothing at all while off")
        self._hide_when_off.setToolTip(
            "Right for a REC lamp, which should simply not be there when you "
            "are not recording. Wrong for a console lamp: the point of that one "
            "is to be seen when it goes off."
        )
        self._hide_when_off.toggled.connect(lambda _on: self._lamp_changed())
        self._row(form, "lamp_hide", "", self._hide_when_off)

        # -- cue block
        self._cue_list = QCheckBox("The cue list, as in 1/58")
        self._cue_list.setToolTip("Leave off on a show with one cue list, where \"1/\" is noise.")
        self._cue_label = QCheckBox("The cue's label")
        self._cue_next = QCheckBox("The next cue")
        self._cue_previous = QCheckBox("The cue it came from, above")
        self._cue_bar = QCheckBox("A bar that fills while it fades")
        for box in (self._cue_list, self._cue_label, self._cue_next,
                    self._cue_previous, self._cue_bar):
            box.toggled.connect(lambda _on: self._cue_options_changed())
        self._row(form, "cue_options", "Show:", _column_widget(
            self._cue_list, self._cue_label, self._cue_previous,
            self._cue_next, self._cue_bar,
        ))

        # -- picture
        self._image_label = QLabel("Image file:")
        image_row = QHBoxLayout()
        self._image_path = QLineEdit()
        self._image_path.setPlaceholderText("No image chosen")
        self._image_path.editingFinished.connect(self._image_path_typed)
        image_row.addWidget(self._image_path, 1)
        self._image_browse = QPushButton("Choose...")
        self._image_browse.clicked.connect(self._choose_image)
        image_row.addWidget(self._image_browse)
        self._image_row = QWidget()
        self._image_row.setLayout(image_row)
        image_row.setContentsMargins(0, 0, 0, 0)
        form.addRow(self._image_label, self._image_row)
        self._rows["image_file"] = (self._image_label, self._image_row)

        self._image_height_label = QLabel("Image height:")
        self._image_height = QDoubleSpinBox()
        self._image_height.setRange(0.01, 1.0)
        self._image_height.setSingleStep(0.01)
        self._image_height.setDecimals(3)
        self._image_height.setToolTip(
            "As a fraction of the frame height, so the same layout looks right "
            "at 1080p and at 4K."
        )
        self._image_height.valueChanged.connect(self._image_height_changed)
        form.addRow(self._image_height_label, self._image_height)
        self._rows["image_height"] = (self._image_height_label, self._image_height)

        self._image_opacity = _percent_spin(0, 100, 5)
        self._image_opacity.valueChanged.connect(self._image_opacity_changed)
        self._row(form, "image_opacity", "Image opacity:", self._image_opacity)

        # -- shapes and bars
        self._panel_width = _percent_spin(1, 100, 1)
        self._panel_width.setToolTip("Of the frame width. 100% with an edge margin of 0 runs edge to edge.")
        self._panel_width.valueChanged.connect(lambda _v: self._panel_changed())
        self._row(form, "panel_width", "Width:", self._panel_width)
        self._panel_height = _percent_spin(1, 100, 1)
        self._panel_height.setToolTip("Of the frame height.")
        self._panel_height.valueChanged.connect(lambda _v: self._panel_changed())
        self._row(form, "panel_height", "Height:", self._panel_height)

        self._bar_width = _percent_spin(1, 100, 1)
        self._bar_width.setToolTip("Of the frame width.")
        self._bar_width.valueChanged.connect(lambda _v: self._bar_changed())
        self._row(form, "bar_width", "Bar length:", self._bar_width)
        self._bar_height = _percent_spin(0.2, 10, 0.1, decimals=1)
        self._bar_height.setToolTip("Of the frame height.")
        self._bar_height.valueChanged.connect(lambda _v: self._bar_changed())
        self._row(form, "bar_height", "Bar thickness:", self._bar_height)
        self._bar_idle = QCheckBox("Only while a fade is running")
        self._bar_idle.setToolTip(
            "Unticked, an empty track stays on screen between fades, so the "
            "bar's position is always visible."
        )
        self._bar_idle.toggled.connect(lambda _on: self._bar_changed())
        self._row(form, "bar_idle", "", self._bar_idle)

        # -- time in cue
        self._timer_from = QComboBox()
        for value, label in CUE_TIMER_STARTS:
            self._timer_from.addItem(label, value)
        self._timer_from.setToolTip(
            "When the clock starts for each cue: as soon as it is fired, or once "
            "its fade into the cue has finished. Counting from the end shows 0:00 "
            "while the fade is still running."
        )
        self._timer_from.currentIndexChanged.connect(lambda _i: self._timer_changed())
        self._row(form, "timer_from", "Count from:", self._timer_from)
        self._timer_prefix = QLineEdit()
        self._timer_prefix.setToolTip(
            "Text in front of the time, such as \"In cue\". Leave it empty for the time alone."
        )
        self._timer_prefix.textChanged.connect(lambda _t: self._timer_changed())
        self._row(form, "timer_prefix", "Label:", self._timer_prefix)
        return page

    def _build_position_tab(self) -> QWidget:
        page, form = _form_page()

        self._anchor = QComboBox()
        for anchor in Anchor:
            self._anchor.addItem(anchor.label, anchor)
        self._anchor.setToolTip(
            "The point of the picture the widget hangs from, which is also the "
            "way it grows: a widget on the right grows leftwards as its text "
            "gets longer. Dragging onto a margin or a centre line sets it."
        )
        self._anchor.currentIndexChanged.connect(self._anchor_changed)
        form.addRow("Corner:", self._anchor)

        self._offset_x = self._spin(-1.0, 1.0, 0.005, self._placement_changed)
        form.addRow("Nudge across:", self._offset_x)
        self._offset_y = self._spin(-1.0, 1.0, 0.005, self._placement_changed)
        form.addRow("Nudge down:", self._offset_y)

        self._margin = self._spin(0.0, 0.2, 0.005, self._placement_changed)
        self._margin.setToolTip(
            "How far in from the edge of the picture a corner widget sits, as a "
            "fraction of the frame. 0 is flush to the edge: right for a strip "
            "across the bottom, wrong for text, which a TV's overscan can cut."
        )
        form.addRow("Edge margin:", self._margin)

        self._max_width = self._spin(0.0, 1.0, 0.05, self._max_width_changed, decimals=2)
        self._max_width.setSpecialValueText("No limit")
        self._max_width.setToolTip(
            "The widest this widget may draw, as a fraction of the frame width. "
            "Longer text is cut with an ellipsis at the end away from its "
            "corner. Keeps a command line from running into the show name on "
            "the same edge."
        )
        self._row(form, "max_width", "Widest:", self._max_width)

        self._z_order = QSpinBox()
        self._z_order.setRange(-999, 999)
        self._z_order.setToolTip("Higher numbers draw on top.")
        self._z_order.valueChanged.connect(self._z_changed)
        form.addRow("Layer:", self._z_order)

        self._locked = QCheckBox("Lock in place")
        self._locked.setToolTip(
            "A locked widget cannot be dragged on the preview or moved with "
            "the arrow keys. These settings still move it."
        )
        self._locked.toggled.connect(self._locked_changed)
        form.addRow("", self._locked)
        return page

    def _build_style_tab(self) -> QWidget:
        page, form = _form_page()

        self._font = QFontComboBox()
        self._font.currentFontChanged.connect(self._font_changed)
        self._row(form, "font", "Font:", self._font)

        self._size = self._spin(0.005, 0.3, 0.002, self._style_changed, decimals=3)
        self._size.setToolTip(
            "Text height as a fraction of the picture height, so a layout built "
            "here stays the same apparent size at any recording resolution.\n"
            "0.045 is about 49 px at 1080p and 97 px at 4K."
        )
        self._row(form, "size", "Text size:", self._size)

        self._weight = QComboBox()
        for label, weight in _WEIGHTS:
            self._weight.addItem(label, weight)
        self._weight.currentIndexChanged.connect(self._style_changed)
        self._italic = QCheckBox("Italic")
        self._italic.toggled.connect(self._style_changed)
        self._row(form, "weight", "Weight:", _row_widget(self._weight, self._italic))

        self._colour_swatch = _swatch()
        self._colour_button = QPushButton("Choose...")
        self._colour_button.clicked.connect(self._choose_colour)
        self._row(form, "colour", "Colour:", _row_widget(self._colour_swatch, self._colour_button))

        self._align = QComboBox()
        for align in Align:
            self._align.addItem(align.value.title(), align)
        self._align.setToolTip("How the lines of a widget of several lines line up with each other.")
        self._align.currentIndexChanged.connect(self._style_changed)
        self._row(form, "align", "Align:", self._align)

        self._opacity = _percent_spin(0, 100, 5)
        self._opacity.setToolTip(
            "How solid the whole widget is. Conditions and fades still work on "
            "top of this."
        )
        self._opacity.valueChanged.connect(self._style_changed)
        form.addRow("Opacity:", self._opacity)

        self._box = QCheckBox("Dark box behind the text")
        self._box.setToolTip(
            "Strongly recommended. Text alone over a lit stage is unreadable "
            "the moment anything bright happens behind it."
        )
        self._box.toggled.connect(self._style_changed)
        self._row(form, "box", "", self._box)

        self._box_swatch = _swatch()
        box_button = QPushButton("Choose...")
        box_button.clicked.connect(self._choose_box_colour)
        self._box_opacity = _percent_spin(0, 100, 5)
        self._box_opacity.valueChanged.connect(self._style_changed)
        self._row(form, "box_colour", "Box colour:", _row_widget(
            self._box_swatch, box_button, QLabel("opacity"), self._box_opacity
        ))
        self._box_colour_label = self._rows["box_colour"][0]

        self._box_padding = self._spin(0.0, 0.05, 0.001, self._style_changed, decimals=3)
        self._box_padding.setToolTip("Space between the text and the edge of its box, as a fraction of the frame height.")
        self._row(form, "box_padding", "Box padding:", self._box_padding)
        self._box_radius = self._spin(0.0, 0.05, 0.001, self._style_changed, decimals=3)
        self._box_radius.setToolTip("Rounding of the corners, as a fraction of the frame height. 0 is square.")
        self._row(form, "box_radius", "Corner radius:", self._box_radius)

        self._outline = QCheckBox("Outline")
        self._outline.toggled.connect(self._style_changed)
        self._row(form, "outline", "", self._outline)

        self._shadow = QCheckBox("Drop shadow")
        self._shadow.toggled.connect(self._style_changed)
        self._row(form, "shadow", "", self._shadow)

        self._copy_style_button = QPushButton("Copy style")
        self._copy_style_button.setToolTip("Remember this widget's look, to give to another.")
        self._copy_style_button.clicked.connect(self._copy_style)
        self._paste_style_button = QPushButton("Paste style")
        self._paste_style_button.setEnabled(False)
        self._paste_style_button.setToolTip("Copy a style from another widget first.")
        self._paste_style_button.clicked.connect(self._paste_style)
        form.addRow("", _row_widget(self._copy_style_button, self._paste_style_button))
        return page

    def _build_visibility_tab(self) -> QWidget:
        page, form = _form_page()

        self._visible = QCheckBox("Show this widget")
        self._visible.toggled.connect(self._visible_changed)
        form.addRow("", self._visible)

        self._only_when_mode = QComboBox()
        for label, field in _CONDITIONS:
            self._only_when_mode.addItem(label, field)
        self._only_when_mode.setToolTip(
            "Has a value: appears while the key holds anything at all -- a "
            "note that has been typed, a command line being written.\n"
            "Is true: appears while the key is true -- a fade running, a take "
            "being recorded."
        )
        self._only_when_mode.currentIndexChanged.connect(self._only_when_mode_changed)
        form.addRow("Appear:", self._only_when_mode)

        self._only_when = QLineEdit()
        self._only_when.setPlaceholderText("e.g. eos.cue.active.fading")
        self._only_when.setToolTip(
            "Optional. The widget appears only while this bus key is true or "
            "non-empty."
        )
        self._only_when.textChanged.connect(self._visibility_changed)
        form.addRow("Key:", _row_widget(
            self._only_when, self._key_button(self._only_when.setText), stretch=False
        ))

        self._hide_after = self._spin(0.0, 3600.0, 1.0, self._visibility_timing_changed, decimals=1)
        self._hide_after.setSpecialValueText("Never")
        self._hide_after.setSuffix(" s")
        self._hide_after.setToolTip(
            "Fade the widget out this long after the last change to anything "
            "it shows, and bring it back on the next. For a note that should "
            "not sit on the picture for the rest of the act."
        )
        form.addRow("Hide after:", self._hide_after)

        self._fade = self._spin(0.0, 5.0, 0.05, self._visibility_timing_changed, decimals=2)
        self._fade.setSuffix(" s")
        self._fade.setToolTip("How long it takes to fade in and out. 0 cuts.")
        form.addRow("Fade:", self._fade)

        self._reads = QLabel()
        self._reads.setWordWrap(True)
        # Not palette(mid), which on the dark theme is all but the colour of
        # the panel behind it.
        self._reads.setStyleSheet("color:#9aa0aa;")
        form.addRow("Reads:", self._reads)
        return page

    @staticmethod
    def _spin(low, high, step, slot, decimals: int = 3) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setDecimals(decimals)
        spin.valueChanged.connect(slot)
        return spin

    # ------------------------------------------------------------- bus keys

    def _key_button(self, insert) -> QToolButton:
        """A menu of bus keys, so a template is written by choosing, not recalling.

        Filled each time it opens, so its last section lists what is actually
        on the bus at that moment -- a Manual field added this afternoon, a
        second console -- and not only the keys Wer knows by name.
        """
        button = QToolButton()
        button.setText("Insert key")
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(button)
        menu.setToolTipsVisible(True)
        menu.aboutToShow.connect(lambda: self._fill_key_menu(menu, insert))
        button.setMenu(menu)
        return button

    def _fill_key_menu(self, menu: QMenu, insert) -> None:
        # Each submenu is made with the menu as its parent. menu.addMenu(title)
        # hands back a submenu Python owns, and this runs from aboutToShow, so
        # they were deleted the moment it returned -- before the menu was
        # drawn. Last opening's are cleared away first, or every opening
        # would leave another set behind.
        for old in menu.findChildren(QMenu):
            old.deleteLater()
        menu.clear()
        for group, keys in BUS_KEY_GROUPS:
            submenu = QMenu(group, menu)
            menu.addMenu(submenu)
            submenu.setToolTipsVisible(True)
            for key, what in keys:
                if "<" in key:
                    # A pattern (a user number, a softkey), not a key that
                    # works as written.
                    continue
                action = submenu.addAction(key)
                action.setToolTip(what)
                action.triggered.connect(lambda _=False, k=key: insert(k))
        live = self.compositor.bus.keys()
        if live:
            submenu = QMenu(f"On the bus now ({len(live)})", menu)
            menu.addMenu(submenu)
            for key in live:
                action = submenu.addAction(key)
                action.triggered.connect(lambda _=False, k=key: insert(k))

    def _insert_into_content(self, key: str) -> None:
        self._content.insertPlainText("{" + key + "}")
        self._content.setFocus()

    def _insert_lamp_key(self, key: str) -> None:
        self._lamp_key.setText(key)
        self._lamp_changed()

    # --------------------------------------------------------------- loading

    def _show_rows(self, kind: str | None) -> None:
        wanted = _ROWS_FOR_KIND.get(kind or "", frozenset())
        for name, (caption, field) in self._rows.items():
            caption.setVisible(name in wanted)
            field.setVisible(name in wanted)

    def _load_properties(self) -> None:
        widget = self._current
        if widget is None:
            # Clear rather than leave the last selection's values sitting in a
            # disabled panel, which reads as "these apply to something".
            self._loading = True
            try:
                self._content.clear()
                self._what.setText("")
                self._what.setStyleSheet(_CAPTION)
                self._show_rows(None)
                self._show_cmd_user(None)
                self._only_when.clear()
                self._reads.setText("")
                for swatch in (self._colour_swatch, self._box_swatch,
                               self._on_swatch, self._off_swatch):
                    swatch.setStyleSheet("")
            finally:
                self._loading = False
            return

        self._loading = True
        try:
            self._show_rows(_kind_key(widget))
            self._what.setText(widget.describe())
            # Reset in every case: a warning left over from a broken watermark
            # went on colouring the caption of whatever was selected next.
            self._what.setStyleSheet(_CAPTION)

            if isinstance(widget, TextWidget):
                self._content_label.setText("Content:")
                self._content.setText(widget.template)
                self._show_cmd_user(command_line_user(widget.template))
                self._emphasise.setChecked(widget.emphasise_first_line)
            elif isinstance(widget, StatusWidget):
                self._content_label.setText("On text:")
                self._content.setText(widget.on_text)
                self._show_cmd_user(None)
                self._lamp_key.setText(widget.key)
                self._off_text.setText(widget.off_text)
                self._set_swatch(widget.on_colour, self._on_swatch)
                self._set_swatch(widget.off_colour, self._off_swatch)
                self._show_dot.setChecked(widget.show_dot)
                self._hide_when_off.setChecked(widget.hide_when_off)
            else:
                self._content.setText("")
                self._show_cmd_user(None)

            if isinstance(widget, CueWidget):
                self._cue_list.setChecked(widget.show_list)
                self._cue_label.setChecked(widget.show_label)
                self._cue_next.setChecked(widget.show_pending)
                self._cue_previous.setChecked(widget.show_previous)
                self._cue_bar.setChecked(widget.show_progress)
            if isinstance(widget, PanelWidget):
                self._panel_width.setValue(widget.width * 100.0)
                self._panel_height.setValue(widget.height * 100.0)
            if isinstance(widget, FadeBarWidget):
                self._bar_width.setValue(widget.width * 100.0)
                self._bar_height.setValue(widget.height * 100.0)
                self._bar_idle.setChecked(widget.hide_when_idle)
            if isinstance(widget, CueTimerWidget):
                self._timer_from.setCurrentIndex(
                    max(0, self._timer_from.findData(widget.count_from))
                )
                self._timer_prefix.setText(widget.prefix)
            self._box_colour_label.setText(
                "Fill:" if isinstance(widget, PanelWidget) else "Box colour:"
            )
            self._show_image_controls(widget)

            placement = widget.placement
            self._anchor.setCurrentIndex(list(Anchor).index(placement.anchor))
            self._offset_x.setValue(placement.offset_x)
            self._offset_y.setValue(placement.offset_y)
            self._margin.setValue(placement.margin)
            self._max_width.setValue(widget.max_width)
            self._z_order.setValue(widget.z_order)
            self._locked.setChecked(widget.locked)

            style = widget.style
            self._font.setCurrentFont(QFont(style.family))
            self._size.setValue(style.size)
            weights = [weight for _label, weight in _WEIGHTS]
            self._weight.setCurrentIndex(
                min(range(len(weights)), key=lambda i: abs(weights[i] - style.weight))
            )
            self._italic.setChecked(style.italic)
            self._set_swatch(style.colour)
            self._align.setCurrentIndex(list(Align).index(style.align))
            self._opacity.setValue(style.opacity * 100.0)
            self._box.setChecked(style.box.enabled)
            self._set_swatch(style.box.fill, self._box_swatch)
            self._box_opacity.setValue(style.box.fill.a / 2.55)
            self._box_padding.setValue(style.box.padding)
            self._box_radius.setValue(style.box.corner_radius)
            self._outline.setChecked(style.outline_width > 0)
            self._shadow.setChecked(style.shadow)

            self._visible.setChecked(widget.visible)
            # A VisibilityRule has two condition fields -- only_when (truthy)
            # and only_when_present (non-empty) -- and one box to show them in.
            # It used to read and write only the second, so the Fade countdown
            # preset, which ships conditioned on the first, showed an empty box
            # and could not be un-conditioned; typing in it ANDed a second
            # condition on instead of replacing the one already there. The box
            # now edits whichever field this widget actually uses, and Appear
            # says which that is.
            rule = widget.visibility
            self._only_when_field = (
                "only_when" if rule.only_when else "only_when_present"
            )
            self._only_when.setText(rule.only_when or rule.only_when_present or "")
            self._only_when_mode.setCurrentIndex(
                0 if not (rule.only_when or rule.only_when_present)
                else self._only_when_mode.findData(self._only_when_field)
            )
            self._hide_after.setValue(rule.hide_after)
            self._fade.setValue(rule.fade)
            self._reads.setText(", ".join(widget.bus_keys) or "nothing")
        finally:
            self._loading = False

    def _show_image_controls(self, widget) -> None:
        """Fill in a picture widget's file, size and caption."""
        if not isinstance(widget, ImageWidget):
            return
        self._image_path.setText(widget.path)
        self._image_height.setValue(widget.height)
        self._image_opacity.setValue(widget.image_opacity * 100.0)
        # Ask the file the question now. `problem` is otherwise only recomputed
        # during a render, and the overlay is normally set up before the camera
        # is started -- so a watermark pointed at a file that is not there said
        # nothing at all, which is the one thing this caption exists to catch.
        #
        # Nothing to ask about until there is a path, though: a watermark has
        # none the moment it is added, by design, and warning about it in amber
        # italics makes a widget the operator has just created look broken.
        # describe() already says "Choose an image file to show one."
        if widget.path and widget.check():
            self._what.setText(widget.describe() + "  -- " + widget.problem)
            self._what.setStyleSheet(_WARNING)
        else:
            # Set in both branches: leaving the caption alone left the previous
            # selection's warning on screen, over a widget that is fine.
            self._what.setText(widget.describe())
            self._what.setStyleSheet(_CAPTION)

    def _choose_image(self) -> None:
        if not isinstance(self._current, ImageWidget):
            return
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose an image to overlay", self._image_path.text(),
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;All files (*)",
        )
        if chosen:
            self._image_path.setText(chosen)
            self._image_path_typed()

    def _image_path_typed(self) -> None:
        if self._loading or not isinstance(self._current, ImageWidget):
            return
        self._current.path = self._image_path.text().strip()
        self._current.reload()
        self._show_image_controls(self._current)
        self._apply()

    def _image_height_changed(self) -> None:
        if self._loading or not isinstance(self._current, ImageWidget):
            return
        self._current.height = self._image_height.value()
        self._current.mark_dirty()
        self._what.setText(self._current.describe())
        self._apply()

    def _image_opacity_changed(self) -> None:
        if self._loading or not isinstance(self._current, ImageWidget):
            return
        self._current.image_opacity = self._image_opacity.value() / 100.0
        self._apply()

    def _show_cmd_user(self, user: int | None) -> None:
        """Reveal the user picker only for a widget that shows a command line."""
        showing = user is not None
        self._cmd_user_label.setVisible(showing)
        self._cmd_user.setVisible(showing)
        if showing:
            index = self._cmd_user.findData(user)
            self._cmd_user.setCurrentIndex(index if index >= 0 else 0)

    def _cmd_user_changed(self) -> None:
        if self._loading or not isinstance(self._current, TextWidget):
            return
        wanted = int(self._cmd_user.currentData() or 0)
        rewritten = retarget_command_line(self._current.template, wanted)
        if rewritten == self._current.template:
            return
        self._current.template = rewritten

        rule = self._current.visibility
        if rule.only_when_present and "cmdline" in rule.only_when_present:
            # The visibility key has to follow the template. A widget shown
            # "only when there is a command line" would otherwise be judging
            # itself on a key it no longer displays.
            self._current.visibility = replace(
                rule,
                only_when_present=retarget_command_line(
                    rule.only_when_present, wanted
                ),
            )

        self._loading = True
        try:
            self._content.setText(rewritten)
            self._only_when.setText(
                self._current.visibility.only_when
                or self._current.visibility.only_when_present or ""
            )
        finally:
            self._loading = False
        self._reads.setText(", ".join(self._current.bus_keys) or "nothing")
        self._apply()

    def _set_swatch(self, colour: Colour, swatch: QLabel | None = None) -> None:
        (swatch or self._colour_swatch).setStyleSheet(
            f"background-color: {colour.to_hex()}; border: 1px solid #888;"
        )

    # --------------------------------------------------------------- editing

    def _apply(self) -> None:
        """Force a re-render and tell the world something changed."""
        if self._current is not None:
            self._current.mark_dirty()
            # Editing Content, the command-line user or the visibility key all
            # change which bus keys the widget reads, and the compositor
            # subscribed to the ones it had when it was added. Without this the
            # widget rendered correctly once, here, and then froze: the new keys
            # had no subscription and the old ones had gone quiet, so the value
            # it held at the moment you stopped typing was burned into every
            # frame of the rest of the take.
            self.compositor.resubscribe(self._current)
        self.layout_changed.emit()

    def _content_changed(self, text: str) -> None:
        if self._loading or self._current is None:
            return
        if isinstance(self._current, TextWidget):
            self._current.template = text
            self._reads.setText(", ".join(self._current.bus_keys) or "nothing")
            self._what.setText(self._current.describe())
            was_loading = self._loading
            self._loading = True
            try:
                self._show_cmd_user(command_line_user(text))
            finally:
                self._loading = was_loading
        elif isinstance(self._current, StatusWidget):
            self._current.on_text = text
            self._reads.setText(", ".join(self._current.bus_keys) or "nothing")
        self._apply()

    def _emphasise_changed(self, checked: bool) -> None:
        if self._loading or not isinstance(self._current, TextWidget):
            return
        self._current.emphasise_first_line = checked
        self._apply()

    def _lamp_changed(self) -> None:
        widget = self._current
        if self._loading or not isinstance(widget, StatusWidget):
            return
        widget.key = self._lamp_key.text().strip()
        widget.off_text = self._off_text.text()
        widget.show_dot = self._show_dot.isChecked()
        widget.hide_when_off = self._hide_when_off.isChecked()
        self._what.setText(widget.describe())
        self._reads.setText(", ".join(widget.bus_keys) or "nothing")
        self._apply()

    def _choose_lamp_colour(self, which: str) -> None:
        widget = self._current
        if not isinstance(widget, StatusWidget):
            return
        current = widget.on_colour if which == "on" else widget.off_colour
        chosen = QColorDialog.getColor(
            QColor(current.r, current.g, current.b), self, f"Lamp colour when {which}"
        )
        if not chosen.isValid():
            return
        colour = Colour(chosen.red(), chosen.green(), chosen.blue(), current.a)
        if which == "on":
            widget.on_colour = colour
            self._set_swatch(colour, self._on_swatch)
        else:
            widget.off_colour = colour
            self._set_swatch(colour, self._off_swatch)
        self._apply()

    def _cue_options_changed(self) -> None:
        widget = self._current
        if self._loading or not isinstance(widget, CueWidget):
            return
        widget.show_list = self._cue_list.isChecked()
        widget.show_label = self._cue_label.isChecked()
        widget.show_pending = self._cue_next.isChecked()
        widget.show_previous = self._cue_previous.isChecked()
        widget.show_progress = self._cue_bar.isChecked()
        self._what.setText(widget.describe())
        self._reads.setText(", ".join(widget.bus_keys) or "nothing")
        self._apply()

    def _panel_changed(self) -> None:
        widget = self._current
        if self._loading or not isinstance(widget, PanelWidget):
            return
        widget.width = self._panel_width.value() / 100.0
        widget.height = self._panel_height.value() / 100.0
        self._what.setText(widget.describe())
        self._apply()

    def _bar_changed(self) -> None:
        widget = self._current
        if self._loading or not isinstance(widget, FadeBarWidget):
            return
        widget.width = self._bar_width.value() / 100.0
        widget.height = self._bar_height.value() / 100.0
        widget.hide_when_idle = self._bar_idle.isChecked()
        self._what.setText(widget.describe())
        self._apply()

    def _timer_changed(self) -> None:
        widget = self._current
        if self._loading or not isinstance(widget, CueTimerWidget):
            return
        widget.count_from = self._timer_from.currentData() or "fade_start"
        widget.prefix = self._timer_prefix.text()
        widget.mark_dirty()
        self._what.setText(widget.describe())
        self._apply()

    def _anchor_changed(self) -> None:
        """Send the widget to a corner, clear of whatever is already in it.

        Setting a corner means what adding a widget to one means: the widget
        belongs at that corner, clear of whatever holds it. So the push
        _clear_of_others made at the old corner -- if it made one -- comes off
        first, putting the widget back on the offsets it was added with, and
        then the same clearing runs at the new corner and records whatever it
        does this time. Without the first half, a date pushed a third of the
        way down the frame to clear a clock kept that third of a frame when it
        was sent to an empty bottom left and sat in mid-air, which is what
        Hudson saw. Widgets that were never pushed keep their own offsets and
        are cleared just the same.
        """
        if self._loading or self._current is None:
            return
        widget = self._current
        pushed = self._remembered_push(widget)
        self._forget_push(widget.id)
        widget.placement = replace(
            widget.placement,
            anchor=self._anchor.currentData(),
            offset_y=widget.placement.offset_y - pushed,
        )
        self._clear_of_others(widget)
        # The clearing moves the widget after the box was set, so the nudges on
        # the Position tab have to be told what they now read -- they are the
        # only place the number is visible before it reaches the show file.
        self._show_placement(widget)
        self._apply()

    def _placement_changed(self) -> None:
        if self._loading or self._current is None:
            return
        # Typed in by hand, so the number is the operator's from here on and a
        # later corner change must not take Wer's old push out of it.
        self._forget_push(self._current.id)
        self._current.placement = replace(
            self._current.placement,
            offset_x=self._offset_x.value(),
            offset_y=self._offset_y.value(),
            margin=self._margin.value(),
        )
        self._apply()

    def _max_width_changed(self) -> None:
        if self._loading or self._current is None:
            return
        self._current.max_width = self._max_width.value()
        self._apply()

    def _locked_changed(self, checked: bool) -> None:
        if self._loading or self._current is None:
            return
        self._current.locked = checked
        self.refresh()
        self._apply()

    def _font_changed(self, font: QFont) -> None:
        # Its own handler rather than part of _style_changed: the font box
        # substitutes the nearest installed family for one it does not have,
        # and folding its reading into every style change would quietly swap
        # the font of a show file built on another machine the first time
        # anyone touched the size.
        if self._loading or self._current is None:
            return
        self._current.style = replace(self._current.style, family=font.family())
        self._apply()

    def _style_changed(self) -> None:
        if self._loading or self._current is None:
            return
        style = self._current.style
        box = replace(
            style.box,
            enabled=self._box.isChecked(),
            fill=style.box.fill.with_alpha(round(self._box_opacity.value() * 2.55)),
            padding=self._box_padding.value(),
            corner_radius=self._box_radius.value(),
        )
        self._current.style = replace(
            style,
            size=self._size.value(),
            weight=int(self._weight.currentData() or style.weight),
            italic=self._italic.isChecked(),
            align=self._align.currentData(),
            opacity=self._opacity.value() / 100.0,
            # A width of zero is how "no outline" is expressed. While it stays
            # ticked, a width the widget already has is kept -- this used to
            # write every outline back as 0.10 whenever any style setting
            # changed -- and a newly ticked one gets the 0.10 default that
            # survives a light background.
            outline_width=(
                (style.outline_width or 0.10) if self._outline.isChecked() else 0.0
            ),
            shadow=self._shadow.isChecked(),
            box=box,
        )
        self._apply()

    def _choose_colour(self) -> None:
        if self._current is None:
            return
        current = self._current.style.colour
        chosen = QColorDialog.getColor(
            QColor(current.r, current.g, current.b), self, "Text colour"
        )
        if not chosen.isValid():
            return
        colour = Colour(chosen.red(), chosen.green(), chosen.blue(), current.a)
        self._current.style = replace(self._current.style, colour=colour)
        self._set_swatch(colour)
        self._apply()

    def _choose_box_colour(self) -> None:
        if self._current is None:
            return
        box = self._current.style.box
        chosen = QColorDialog.getColor(
            QColor(box.fill.r, box.fill.g, box.fill.b), self, "Box colour"
        )
        if not chosen.isValid():
            return
        fill = Colour(chosen.red(), chosen.green(), chosen.blue(), box.fill.a)
        self._current.style = replace(self._current.style, box=replace(box, fill=fill))
        self._set_swatch(fill, self._box_swatch)
        self._apply()

    def _copy_style(self) -> None:
        if self._current is None:
            return
        self._style_clipboard = self._current.style
        self._paste_style_button.setEnabled(True)
        self._paste_style_button.setToolTip(
            f"Give the selected widget the look of {self._current.id}: font, "
            "colour, box, outline and shadow. Its text size stays its own."
        )

    def _paste_style(self) -> None:
        """Everything but the size: a cue number and a date wanting the same
        look almost never want the same height."""
        if self._current is None or self._style_clipboard is None:
            return
        self._current.style = replace(
            self._style_clipboard, size=self._current.style.size
        )
        self._load_properties()
        self._apply()

    def _visible_changed(self, checked: bool) -> None:
        if self._loading or self._current is None:
            return
        self._current.visible = checked
        self.refresh()
        self._apply()

    def _set_mode_quietly(self, field: str | None) -> None:
        was_loading = self._loading
        self._loading = True
        try:
            self._only_when_mode.setCurrentIndex(
                max(0, self._only_when_mode.findData(field)) if field else 0
            )
        finally:
            self._loading = was_loading

    def _only_when_mode_changed(self) -> None:
        if self._loading or self._current is None:
            return
        field = self._only_when_mode.currentData()
        rule = self._current.visibility
        if field is None:
            rule.only_when = None
            rule.only_when_present = None
            was_loading = self._loading
            self._loading = True
            try:
                self._only_when.clear()
            finally:
                self._loading = was_loading
        else:
            key = self._only_when.text().strip() or None
            other = "only_when" if field == "only_when_present" else "only_when_present"
            self._only_when_field = field
            setattr(rule, field, key)
            setattr(rule, other, None)
        self._reads.setText(", ".join(self._current.bus_keys) or "nothing")
        self._apply()

    def _visibility_changed(self, text: str) -> None:
        if self._loading or self._current is None:
            return
        key = text.strip() or None
        if key and self._only_when_mode.currentData() is None:
            # A key typed for a widget that appears always: "while it has a
            # value", which is what this box has always meant on its own.
            self._only_when_field = "only_when_present"
            self._set_mode_quietly(self._only_when_field)
        # Into the field this widget's condition already lives in, chosen when
        # the properties were loaded, so a typed key replaces the rule rather
        # than adding a second one it now also has to satisfy.
        setattr(self._current.visibility, self._only_when_field, key)
        # And clear the other field. One box cannot show two conditions, so a
        # widget carrying both -- which is only ever the work of the bug this
        # box was fixed for, ANDing a new key onto an invisible old one --
        # would keep the hidden one after the operator cleared the box, still
        # gating a widget that now claims to be unconditional. That is a
        # control that appears to do something and does not, on a widget that
        # then never appears in the recording. Whatever the box says is now the
        # whole rule; nothing ships using both fields, so this cannot discard a
        # condition anyone chose deliberately in the UI.
        other = (
            "only_when_present"
            if self._only_when_field == "only_when"
            else "only_when"
        )
        setattr(self._current.visibility, other, None)
        if key is None:
            self._set_mode_quietly(None)
        self._reads.setText(", ".join(self._current.bus_keys) or "nothing")
        self._apply()

    def _visibility_timing_changed(self) -> None:
        if self._loading or self._current is None:
            return
        rule = self._current.visibility
        rule.hide_after = self._hide_after.value()
        rule.fade = self._fade.value()
        self._apply()

    def _z_changed(self, value: int) -> None:
        if self._loading or self._current is None:
            return
        self._current.z_order = value
        self.compositor.resort()
        self._apply()

    # ---------------------------------------------------------------- dragging

    def begin_drag(self, widget_id: str) -> None:
        """The button went down on a widget on the preview.

        Remembers where the widget was, because every drag event after this is
        measured from that position and not from the event before it -- see
        geometry.drag_placement for why. A locked widget is not picked up.
        """
        widget = self.compositor.get(widget_id)
        if widget is None or widget.locked:
            self._drag = None
            return
        self._drag = (widget, widget.placement)

    def drag(
        self, widget_id: str, delta_x: float, delta_y: float,
        within: float, axis: str,
    ) -> None:
        """Put a dragged widget where the pointer is, snapping as it goes.

        ``delta_x`` and ``delta_y`` are the whole movement since begin_drag, as
        fractions of the frame. ``within`` is the snap distance in frame pixels
        (0 while Alt is held) and ``axis`` holds the drag to one direction.
        """
        if self._drag is None:
            return
        dragged, start = self._drag
        # The widget itself, not just its id. A layout switched mid-drag --
        # Ctrl+2 with the button still down -- puts a different widget under
        # the same id, and matching on the id alone moved that one instead.
        if dragged.id != widget_id or self.compositor.get(widget_id) is not dragged:
            return
        size = self.compositor.widget_size(widget_id)
        frame = self.compositor.frame_size
        if size is None or frame is None:
            return

        snapping = self._snap.isChecked() and within > 0
        result = drag_placement(
            start, delta_x, delta_y, *frame, *size,
            within=within if snapping else 0.0,
            others=self._other_rects(widget_id, frame) if snapping else (),
            grid=self.grid if snapping and self._snap_grid.isChecked() else None,
            axis=axis,
        )
        self._place(dragged, result.placement)
        self.snap_feedback.emit(result.guides, result.placement.anchor.label)

    def end_drag(self, widget_id: str, cancelled: bool) -> None:
        """The drag is over. Escape puts the widget back where it started."""
        drag, self._drag = self._drag, None
        self.snap_feedback.emit((), "")
        if drag is None:
            return
        dragged, start = drag
        # The widget itself, as in drag(): a layout switched mid-drag leaves a
        # different widget under the same id, and this one is no longer ours.
        if dragged.id != widget_id or self.compositor.get(widget_id) is not dragged:
            return
        if cancelled:
            self._place(dragged, start)
            return
        if dragged.placement == start:
            # Pressed and let go without moving it, which on the preview is not
            # a drag at all but how a widget is selected -- the click before
            # every corner change made from the property sheet. Counted as a
            # placement it threw Wer's push away one click before the change
            # that had to take it off, and Hudson's fault was back.
            return
        # Put down by hand, so where it sits is the operator's doing now.
        # Forgotten here rather than when the button went down, because Escape
        # puts the widget back exactly as it was -- push and all -- and a memory
        # dropped at the start could not have been put back with it.
        self._forget_push(widget_id)

    def nudge(self, widget_id: str, delta_x: float, delta_y: float) -> None:
        """Move a widget by a fraction of the frame: the arrow keys on the preview.

        Never snaps. The arrow keys are for the last pixel of placement, and a
        snap would take away exactly the control they are there to give.
        """
        widget = self.compositor.get(widget_id)
        if widget is None or widget.locked:
            return
        # Moved deliberately, a pixel at a time: the offset is the operator's.
        self._forget_push(widget_id)
        moved = replace(
            widget.placement,
            offset_x=widget.placement.offset_x + delta_x,
            offset_y=widget.placement.offset_y + delta_y,
        )
        size = self.compositor.widget_size(widget_id)
        frame = self.compositor.frame_size
        if size is not None and frame is not None:
            moved = clamped_placement(moved, *frame, *size)
        self._place(widget, moved)

    def _other_rects(
        self, widget_id: str, frame: tuple[int, int], *, shapes: bool = True
    ) -> list[Rect]:
        """Where every other widget is, in frame pixels, to line up against.

        Worked out from the sizes the compositor last drew rather than by
        rendering: rendering here would be the main thread painting a widget
        the capture thread may be painting at the same moment. A widget that is
        switched off or has never drawn has no size and is left out, because
        lining up with something that is not on the picture puts a widget
        somewhere that looks arbitrary. ``shapes`` False leaves out backing
        strips and boxes too, which other widgets are meant to sit on.
        """
        rects = []
        for other in self.compositor.widgets:
            if other.id == widget_id or not other.visible:
                continue
            if not shapes and isinstance(other, PanelWidget):
                continue
            size = self.compositor.widget_size(other.id)
            if size is not None:
                rects.append(other.placement.resolve(*frame, *size))
        return rects

    def _place(self, widget: OverlayWidget, placement: Placement) -> None:
        """Move a widget, and keep the property sheet saying where it is."""
        widget.placement = placement
        self._show_placement(widget)
        self.layout_changed.emit()

    def _show_placement(self, widget: OverlayWidget) -> None:
        """Say on the Position tab where a widget has just been put.

        The Corner box is set along with the nudges. Snapping changes a
        widget's anchor, and the box used to go on naming the old one. Quietly,
        so that setting the boxes is not taken for the operator setting them.
        """
        if widget is not self._current:
            return
        placement = widget.placement
        self._loading = True
        try:
            self._anchor.setCurrentIndex(list(Anchor).index(placement.anchor))
            self._offset_x.setValue(placement.offset_x)
            self._offset_y.setValue(placement.offset_y)
        finally:
            self._loading = False

    @property
    def edit_mode(self) -> bool:
        return self._edit_mode.isChecked()


def _kind(widget: OverlayWidget) -> str:
    """What the widget list calls a widget."""
    return {
        "cue": "cue block",
        "lamp": "lamp",
        "text": "text",
        "picture": "picture",
        "panel": "shape",
        "fadebar": "fade bar",
        "cuetimer": "time in cue",
    }.get(_kind_key(widget), type(widget).__name__)


def _kind_key(widget: OverlayWidget) -> str:
    """Which rows of the property sheet a widget gets; see _ROWS_FOR_KIND."""
    if isinstance(widget, CueWidget):
        return "cue"
    if isinstance(widget, StatusWidget):
        return "lamp"
    if isinstance(widget, TextWidget):
        return "text"
    if isinstance(widget, ImageWidget):
        return "picture"
    if isinstance(widget, PanelWidget):
        return "panel"
    if isinstance(widget, FadeBarWidget):
        return "fadebar"
    if isinstance(widget, CueTimerWidget):
        return "cuetimer"
    return ""


class _TemplateEdit(QPlainTextEdit):
    """A template box that takes several lines.

    A QLineEdit could only ever hold one line, and the renderer has always
    drawn several: one widget holding the time with the date beneath it is one
    box that can never overlap itself, where two widgets stacked by hand can.
    text() and setText() are provided so it reads like the line edit it
    replaced.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFixedHeight(self.fontMetrics().lineSpacing() * 3 + 14)
        # Fixed as well as a fixed height. A text box's policy is Expanding,
        # and the form gave the row spare room on the strength of it, which
        # then went between the box and the Insert key button beneath.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:  # noqa: N802 - matches QLineEdit
        self.setPlainText(text)


def _swatch() -> QLabel:
    swatch = QLabel()
    swatch.setFixedSize(40, 20)
    return swatch


def _form_page() -> tuple[QWidget, QFormLayout]:
    """A tab page whose rows stay at the top, however tall the tab is.

    With nothing beneath the form, the spare height of a short tab was shared
    out through its rows and the controls were scattered down the page.
    """
    page = QWidget()
    column = QVBoxLayout(page)
    form = QFormLayout()
    column.addLayout(form)
    column.addStretch(1)
    return page, form


def _row_widget(*parts: QWidget, stretch: bool = True) -> QWidget:
    """Several controls side by side, as one widget so the row hides whole."""
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    for part in parts:
        row.addWidget(part)
    if stretch:
        row.addStretch(1)
    return holder


def _column_widget(*parts: QWidget) -> QWidget:
    holder = QWidget()
    column = QVBoxLayout(holder)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(2)
    for part in parts:
        column.addWidget(part)
    return holder


def _percent_spin(low: float, high: float, step: float, decimals: int = 0) -> QDoubleSpinBox:
    """A fraction shown as a percentage, which is how people think of opacity."""
    spin = QDoubleSpinBox()
    spin.setRange(low, high)
    spin.setSingleStep(step)
    spin.setDecimals(decimals)
    spin.setSuffix(" %")
    return spin
