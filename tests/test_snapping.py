"""Dragging widgets on the preview: snapping, guides, the grid and the keys.

The complaint was that snapping was "a little too sticky". Measured on the old
code, it was worse than that: a widget sitting on an anchor could not be
dragged off it slowly at all. Each mouse movement was sent as a small step and
added to wherever the last step had snapped to, so a one-pixel move landed
within range of the anchor and was put straight back -- forty one-pixel moves
left it exactly where it started. The only way off was to cover the whole snap
distance, 22 frame pixels at 1080p, between two mouse events.

geometry.drag_placement is the fix: it snaps the pointer's real position,
measured from the press. These tests pin that down at each level -- the
arithmetic, the editor, and the preview surface that turns mouse events into
drags.
"""

from __future__ import annotations

import numpy as np
import pytest

from wer.overlay.geometry import (
    STACK_GAP,
    Anchor,
    Placement,
    Rect,
    drag_placement,
)

WIDTH, HEIGHT = 1920, 1080
MARGIN_PX = 0.02 * WIDTH  # the default margin, across


def _rect(result, size=(300, 80), frame=(WIDTH, HEIGHT)) -> Rect:
    return result.placement.resolve(*frame, *size)


# ----------------------------------------------------------------- arithmetic


def test_a_widget_on_a_guide_comes_off_it_as_soon_as_the_pointer_does() -> None:
    """The stickiness itself. Dragged a pixel at a time off the top-left
    corner, the widget stays on the guide only while the pointer is within
    the snap distance, and then follows the pointer exactly."""
    start = Placement(Anchor.TOP_LEFT)
    for moved in range(1, 41):
        result = drag_placement(
            start, moved / WIDTH, 0.0, WIDTH, HEIGHT, 300, 80, within=6.0
        )
        expected = MARGIN_PX if moved <= 6 else MARGIN_PX + moved
        assert _rect(result).x == round(expected), f"after {moved} px"


def test_snapping_to_the_right_margin_makes_the_widget_right_anchored() -> None:
    """A widget pulled flush to the right grows leftwards from then on, which
    is what being on the right means -- and the half of the anchor that was
    not snapped keeps its own offset."""
    start = Placement(Anchor.TOP_LEFT, offset_y=0.30)
    right_edge_target = WIDTH - MARGIN_PX - 300
    moved = (right_edge_target - 4 - MARGIN_PX) / WIDTH

    result = drag_placement(start, moved, 0.0, WIDTH, HEIGHT, 300, 80, within=6.0)

    assert result.placement.anchor is Anchor.TOP_RIGHT
    assert result.placement.offset_x == 0.0
    assert result.placement.offset_y == pytest.approx(0.30)
    assert [g.axis for g in result.guides] == ["x"]


def test_each_axis_snaps_on_its_own() -> None:
    """On the centre line across, free to sit anywhere down. The old snap
    only caught a widget near one of the nine points, both axes at once, so a
    widget could not be centred without also being put at the top, middle or
    bottom."""
    centred_left = WIDTH / 2 - 150
    start = Placement(
        Anchor.TOP_LEFT, offset_x=(centred_left + 3 - MARGIN_PX) / WIDTH, offset_y=0.37
    )
    result = drag_placement(start, 0.0, 0.0, WIDTH, HEIGHT, 300, 80, within=6.0)

    assert result.placement.anchor is Anchor.TOP_CENTER
    assert result.placement.offset_y == pytest.approx(0.37)
    assert _rect(result).x == round(centred_left)


@pytest.mark.parametrize("anchor", list(Anchor), ids=lambda a: a.value)
def test_all_nine_anchor_positions_can_still_be_snapped_to(anchor: Anchor) -> None:
    near = Placement(anchor, offset_x=3 / WIDTH, offset_y=-3 / HEIGHT)
    result = drag_placement(near, 0.0, 0.0, WIDTH, HEIGHT, 300, 80, within=6.0)
    assert result.placement == Placement(anchor)


def test_a_widget_placed_deliberately_off_every_guide_is_left_alone() -> None:
    deliberate = Placement(Anchor.TOP_LEFT, offset_x=0.25, offset_y=0.25)
    result = drag_placement(deliberate, 0.0, 0.0, WIDTH, HEIGHT, 300, 80, within=6.0)
    assert result.placement == deliberate
    assert result.guides == ()


def test_a_date_can_be_stacked_just_below_a_clock() -> None:
    """What the old snap made impossible. The tech layout this was reported
    from had the date sitting exactly on top of the clock, and the REC lamp on
    top of the elapsed time: snapping to an anchor zeroed the offset, so two
    widgets dropped near one corner were given the same position."""
    clock = Placement(Anchor.TOP_RIGHT).resolve(WIDTH, HEIGHT, 200, 60)
    below = clock.bottom + STACK_GAP * HEIGHT
    start = Placement(
        Anchor.TOP_RIGHT,
        offset_x=-2 / WIDTH,
        offset_y=(below + 3 - 0.02 * HEIGHT) / HEIGHT,
    )

    result = drag_placement(
        start, 0.0, 0.0, WIDTH, HEIGHT, 280, 40, within=6.0, others=[clock]
    )
    date = _rect(result, size=(280, 40))

    assert result.placement.anchor is Anchor.TOP_RIGHT
    assert date.right == clock.right, "not lined up with the clock's right edge"
    assert date.y == round(below), "not stacked just below the clock"
    assert date.y > clock.bottom, "overlapping the clock"
    assert any(g.axis == "y" and g.kind == "widget" for g in result.guides)


def test_widgets_line_up_by_their_left_edges() -> None:
    other = Rect(500, 600, 240, 50)
    start = Placement(Anchor.TOP_LEFT, offset_x=(503 - MARGIN_PX) / WIDTH, offset_y=0.2)
    result = drag_placement(
        start, 0.0, 0.0, WIDTH, HEIGHT, 300, 80, within=6.0, others=[other]
    )
    assert _rect(result).x == 500
    assert result.placement.anchor is Anchor.TOP_LEFT


def test_the_frame_wins_a_tie_with_another_widget() -> None:
    """Both lines are the same distance away; the frame's is the one that also
    settles the anchor, so it is the one worth reporting."""
    frame = (1000, 1000)
    other = Rect(20, 500, 100, 40)  # sitting exactly on the left margin
    start = Placement(Anchor.TOP_LEFT, offset_x=0.003, offset_y=0.3)
    result = drag_placement(
        start, 0.0, 0.0, *frame, 200, 40, within=6.0, others=[other]
    )
    assert result.placement.offset_x == 0.0
    assert [g.kind for g in result.guides if g.axis == "x"] == ["frame"]


def test_nothing_snaps_while_snapping_is_off() -> None:
    """Alt, or Snap unticked: 1 px from a guide stays 1 px from it."""
    start = Placement(Anchor.TOP_LEFT)
    result = drag_placement(start, 1 / WIDTH, 1 / HEIGHT, WIDTH, HEIGHT, 300, 80, within=0.0)
    assert result.placement.offset_x == pytest.approx(1 / WIDTH)
    assert result.placement.offset_y == pytest.approx(1 / HEIGHT)
    assert result.guides == ()


def test_a_straight_drag_neither_moves_nor_snaps_the_held_axis() -> None:
    start = Placement(Anchor.TOP_LEFT, offset_x=0.3, offset_y=3 / HEIGHT)
    result = drag_placement(
        start, 0.15, 0.25, WIDTH, HEIGHT, 300, 80, within=6.0, axis="x"
    )
    assert result.placement.offset_y == start.offset_y, (
        "the held axis moved, or snapped to the margin 3 px away"
    )
    assert result.placement.offset_x == pytest.approx(0.45)


def test_the_grid_catches_the_anchored_edge_when_close() -> None:
    """A left-anchored widget puts its left edge on the line and a
    right-anchored one its right edge: the edge that stays put as the text
    changes is the one worth aligning."""
    grid = (32, 18)  # 60 px squares at 1080p

    left = Placement(Anchor.TOP_LEFT, offset_x=(124 - MARGIN_PX) / WIDTH, offset_y=0.3)
    result = drag_placement(left, 0, 0, WIDTH, HEIGHT, 300, 80, within=6.0, grid=grid)
    assert _rect(result).x == 120
    assert result.placement.anchor is Anchor.TOP_LEFT

    right = Placement(
        Anchor.TOP_RIGHT, offset_x=(1804 - (WIDTH - MARGIN_PX)) / WIDTH, offset_y=0.3
    )
    result = drag_placement(right, 0, 0, WIDTH, HEIGHT, 300, 80, within=6.0, grid=grid)
    assert _rect(result).right == 1800


def test_the_grid_does_not_catch_a_widget_between_lines() -> None:
    between = Placement(Anchor.TOP_LEFT, offset_x=(150 - MARGIN_PX) / WIDTH, offset_y=0.3)
    result = drag_placement(
        between, 0, 0, WIDTH, HEIGHT, 300, 80, within=6.0, grid=(32, 18)
    )
    assert result.placement == between


def test_the_grid_is_ignored_unless_asked_for() -> None:
    near_line = Placement(Anchor.TOP_LEFT, offset_x=(124 - MARGIN_PX) / WIDTH, offset_y=0.3)
    result = drag_placement(near_line, 0, 0, WIDTH, HEIGHT, 300, 80, within=6.0)
    assert result.placement == near_line


def test_a_widget_snapped_to_the_frame_is_in_the_same_place_at_4k() -> None:
    """Snapped to the right margin at 1080p, it is on the right margin at 4K:
    the snap is kept as an anchor, not as a pixel offset that would drift."""
    near_right = WIDTH - MARGIN_PX - 300 - 5
    start = Placement(
        Anchor.TOP_LEFT, offset_x=(near_right - MARGIN_PX) / WIDTH, offset_y=0.001
    )
    result = drag_placement(start, 0.0, 0.0, WIDTH, HEIGHT, 300, 80, within=6.0)
    assert result.placement.anchor is Anchor.TOP_RIGHT
    at_4k = result.placement.resolve(3840, 2160, 600, 160)
    assert at_4k == Placement(Anchor.TOP_RIGHT).resolve(3840, 2160, 600, 160)


def test_a_drag_off_the_frame_is_still_pulled_back_and_draws_no_guides() -> None:
    result = drag_placement(
        Placement(Anchor.TOP_LEFT), 5.0, 0.0, WIDTH, HEIGHT, 300, 80, within=6.0
    )
    rect = _rect(result)
    assert rect.x < WIDTH, "dragged out of reach"
    assert result.guides == ()


# ------------------------------------------------------------------- editor


@pytest.fixture()
def editor(qt_app):
    """A real LayoutEditor over a real Compositor that has drawn one frame, so
    the editor knows the frame size and how big each widget is."""
    from wer.core.databus import DataBus
    from wer.overlay.compositor import Compositor
    from wer.overlay.style import TextStyle
    from wer.overlay.widgets import TextWidget
    from wer.ui.layout_editor import LayoutEditor

    bus = DataBus()
    compositor = Compositor(bus)
    compositor.add(
        TextWidget("cue", "Cue 58", placement=Placement(Anchor.TOP_LEFT),
                   style=TextStyle(size=0.045), z_order=10)
    )
    compositor.add(
        TextWidget("clock", "21:57:31", placement=Placement(Anchor.TOP_RIGHT),
                   style=TextStyle(size=0.034), z_order=20)
    )
    compositor.composite(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))
    panel = LayoutEditor(compositor)
    panel.refresh()
    yield panel, compositor
    panel.deleteLater()


def test_dragging_slowly_off_a_corner_through_the_editor_is_not_sticky(editor) -> None:
    """The same forty one-pixel moves that left the widget where it started."""
    panel, compositor = editor
    panel.begin_drag("cue")
    for moved in range(1, 41):
        panel.drag("cue", moved / WIDTH, 0.0, 6.0, "")
        if moved == 5:
            assert compositor.get("cue").placement.offset_x == 0.0, "should still hold"
    panel.end_drag("cue", False)

    assert compositor.get("cue").placement.offset_x == pytest.approx(40 / WIDTH)


def test_escape_puts_a_dragged_widget_back(editor) -> None:
    panel, compositor = editor
    before = compositor.get("cue").placement
    panel.begin_drag("cue")
    panel.drag("cue", 0.3, 0.2, 6.0, "")
    assert compositor.get("cue").placement != before
    panel.end_drag("cue", True)
    assert compositor.get("cue").placement == before


def test_a_locked_widget_stays_put(editor) -> None:
    panel, compositor = editor
    compositor.get("cue").locked = True
    before = compositor.get("cue").placement
    panel.begin_drag("cue")
    panel.drag("cue", 0.3, 0.2, 6.0, "")
    panel.nudge("cue", 0.1, 0.0)
    assert compositor.get("cue").placement == before


def test_the_arrow_keys_never_snap(editor) -> None:
    """One pixel off the margin is one pixel off the margin, even though a
    drag would have pulled it back on."""
    panel, compositor = editor
    panel.nudge("cue", 1 / WIDTH, 0.0)
    assert compositor.get("cue").placement.offset_x == pytest.approx(1 / WIDTH)


def test_unticking_snap_turns_it_off(editor) -> None:
    panel, compositor = editor
    panel._snap.setChecked(False)
    panel.begin_drag("cue")
    panel.drag("cue", 3 / WIDTH, 0.0, 6.0, "")
    assert compositor.get("cue").placement.offset_x == pytest.approx(3 / WIDTH)


def test_the_corner_box_follows_a_snap(editor) -> None:
    """Snapping changes the anchor; the property sheet went on naming the old
    one."""
    panel, compositor = editor
    panel._select_by_id("cue")
    size = compositor.widget_size("cue")
    to_the_right = (WIDTH - 2 * MARGIN_PX - size[0] - 2) / WIDTH

    panel.begin_drag("cue")
    panel.drag("cue", to_the_right, 0.0, 6.0, "")
    panel.end_drag("cue", False)

    assert compositor.get("cue").placement.anchor is Anchor.TOP_RIGHT
    assert panel._anchor.currentData() == Anchor.TOP_RIGHT


def test_a_drag_reports_the_guides_and_clears_them_at_the_end(editor) -> None:
    panel, _compositor = editor
    feedback = []
    panel.snap_feedback.connect(lambda guides, label: feedback.append((guides, label)))

    panel.begin_drag("cue")
    panel.drag("cue", 2 / WIDTH, 0.0, 6.0, "")
    guides, label = feedback[-1]
    assert guides and label == "Top Left"

    panel.end_drag("cue", False)
    assert feedback[-1] == ((), "")


def test_snap_and_grid_settings_come_from_and_go_back_to_the_show_file(editor) -> None:
    from wer.core.showfile import EditorConfig

    panel, _compositor = editor
    grids, saves = [], []
    panel.grid_changed.connect(lambda columns, rows: grids.append((columns, rows)))
    panel.editor_settings_changed.connect(lambda: saves.append(True))

    config = EditorConfig(snap=False, snap_to_grid=True, grid_columns=64, grid_rows=36)
    panel.set_editor_config(config)
    assert not panel._snap.isChecked()
    assert panel._snap_grid.isChecked() and not panel._snap_grid.isEnabled()
    assert panel.grid == (64, 36) and grids[-1] == (64, 36)
    assert saves == [], "loading the settings is not a change to save"

    panel._grid_size.setCurrentIndex(0)
    assert (config.grid_columns, config.grid_rows) == (16, 9)
    assert saves == [True]


def test_a_hand_edited_grid_wer_does_not_offer_falls_back(editor) -> None:
    from wer.core.showfile import EditorConfig

    panel, _compositor = editor
    config = EditorConfig(grid_columns=7, grid_rows=None)
    panel.set_editor_config(config)
    assert panel.grid == (32, 18)
    assert (config.grid_columns, config.grid_rows) == (32, 18)


def test_a_layout_switched_mid_drag_does_not_move_the_new_layouts_widget(editor) -> None:
    """Ctrl+2 with the button still held: the next layout has its own "cue",
    and matching the drag on the id alone threw that one around instead."""
    from wer.overlay.widgets import TextWidget

    panel, compositor = editor
    panel.begin_drag("cue")
    replacement = TextWidget("cue", "Cue 58", placement=Placement(Anchor.BOTTOM_LEFT))
    compositor.apply_layout([replacement, compositor.get("clock")])

    panel.drag("cue", 0.3, 0.2, 6.0, "")
    panel.end_drag("cue", True)

    assert replacement.placement == Placement(Anchor.BOTTOM_LEFT)


def _drawn_rect(compositor, widget) -> Rect:
    width, height = compositor.frame_size
    image = widget.image(compositor.bus, height, width)
    return widget.placement.resolve(width, height, image.width(), image.height())


def test_a_widget_added_to_an_occupied_corner_is_stacked_clear_of_it(editor) -> None:
    """The date goes under the clock, not on it -- which is where the layout
    this was reported from wanted it, and where the old snapping would not
    let it go."""
    panel, compositor = editor
    clock = _drawn_rect(compositor, compositor.get("clock"))

    panel._add("date")
    date = _drawn_rect(compositor, panel._current)

    assert not date.intersects(clock), "the new widget was put on the clock"
    assert date.y >= clock.bottom
    assert panel._current.placement.anchor is Anchor.TOP_RIGHT


def test_a_widget_added_at_the_bottom_stacks_upwards(editor) -> None:
    panel, compositor = editor
    compositor.bus.publish("eos.cmdline.text", "LIVE: Chan 1 At Full", source_connection_id="t")
    panel._add("cmdline")
    compositor.composite(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))
    command_line = _drawn_rect(compositor, panel._current)

    panel._add("cue_previous")  # also bottom left
    previous = _drawn_rect(compositor, panel._current)

    assert not previous.intersects(command_line)
    assert previous.bottom <= command_line.y


def test_a_backing_shape_is_added_underneath_and_widgets_may_sit_on_it(editor) -> None:
    """Added on top like everything else, a backing strip covered the widgets
    it was put there to sit behind."""
    panel, compositor = editor
    panel._add("backing_strip")
    strip = panel._current
    assert strip.z_order < min(
        w.z_order for w in compositor.widgets if w is not strip
    )

    compositor.composite(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))
    panel._add("cue_previous")  # bottom left, on the strip
    assert panel._current.placement == Placement(
        Anchor.BOTTOM_LEFT, offset_x=0.0, offset_y=0.0
    ), "a widget was pushed off the strip it is meant to sit on"


# ------------------------------------------------------------ preview surface


@pytest.fixture()
def surface(qt_app):
    """A surface showing a black 1080p frame at exactly half size, in edit
    mode, with one widget on it."""
    from wer.ui.preview import VideoSurface

    view = VideoSurface()
    view.resize(960, 540)
    view.show_frame(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))
    view.set_edit_mode(True)
    view.set_widget_rects({"cue": (100, 100, 400, 100)}, (WIDTH, HEIGHT))
    yield view
    view.deleteLater()


def _mouse(kind, x, y, modifiers=None):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    button = (
        Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove
        else Qt.MouseButton.LeftButton
    )
    held = (
        Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease
        else Qt.MouseButton.LeftButton
    )
    return QMouseEvent(
        kind, QPointF(x, y), QPointF(x, y), button, held,
        modifiers if modifiers is not None else Qt.KeyboardModifier.NoModifier,
    )


def test_the_preview_sends_the_whole_movement_since_the_press(surface) -> None:
    """Not the step since the last event -- which is what made it stick."""
    from PySide6.QtCore import QEvent

    dragged = []
    surface.widget_dragged.connect(lambda *args: dragged.append(args))

    surface.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, 100, 75))
    for step in range(1, 21):
        surface.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, 100 + step, 75))

    widget_id, delta_x, delta_y, within, axis = dragged[-1]
    assert widget_id == "cue"
    assert delta_x == pytest.approx(40 / WIDTH), "20 screen px is 40 frame px at half size"
    assert delta_y == 0.0
    assert within == pytest.approx(12.0), "6 screen px is 12 frame px at half size"
    assert axis == ""


def test_alt_and_shift_reach_the_editor(surface) -> None:
    from PySide6.QtCore import QEvent, Qt

    dragged = []
    surface.widget_dragged.connect(lambda *args: dragged.append(args))
    surface.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, 100, 75))

    surface.mouseMoveEvent(
        _mouse(QEvent.Type.MouseMove, 110, 77, Qt.KeyboardModifier.AltModifier)
    )
    assert dragged[-1][3] == 0.0, "Alt should turn snapping off"

    surface.mouseMoveEvent(
        _mouse(QEvent.Type.MouseMove, 110, 77, Qt.KeyboardModifier.ShiftModifier)
    )
    assert dragged[-1][4] == "x", "Shift should hold a mostly-sideways drag to x"


def test_escape_cancels_a_drag_and_the_release_does_nothing_more(surface) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    finished = []
    surface.drag_finished.connect(lambda *args: finished.append(args))
    surface.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, 100, 75))
    surface.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, 130, 90))

    surface.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    )
    surface.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, 130, 90))

    assert finished == [("cue", True)]


def test_arrow_keys_nudge_by_a_pixel_or_a_grid_square(surface) -> None:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    moved = []
    surface.widget_moved.connect(lambda *args: moved.append(args))
    surface.set_selected("cue")
    surface.set_grid(32, 18)

    surface.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier)
    )
    surface.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    )

    assert moved[0] == ("cue", pytest.approx(1 / WIDTH), 0.0)
    assert moved[1] == ("cue", 0.0, pytest.approx(1 / 18))


def test_the_grid_is_drawn_while_a_widget_moves_and_not_otherwise(surface) -> None:
    """Checked on the pixels. A column line of the 32-column grid falls at
    x = 30 on this half-size preview; the frame underneath is black."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QColor

    def brightness_on_a_grid_line() -> int:
        image = surface.grab().toImage()
        return QColor(image.pixel(30, 315)).red()

    surface.set_grid(32, 18)
    assert brightness_on_a_grid_line() == 0, "grid drawn with nothing moving"

    surface.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, 100, 75))
    assert brightness_on_a_grid_line() > 20, "no grid while dragging"

    surface.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, 100, 75))
    assert brightness_on_a_grid_line() == 0, "grid left behind after the drop"
