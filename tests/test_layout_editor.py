"""Editing on the Overlay tab: deleting things, and the corner a widget is sent to.

Reported as "in the overlay page, we aren't able to delete overlays", which was
three faults wearing one coat. The Layout bar went on offering a layout that had
just been removed, because only the switch that follows a delete ever redrew it
and a delete does not always switch. The widget list kept the last selection
after another layout was made live, so Remove was offered for a widget this
compositor does not hold and did nothing when pressed -- while the property
sheet beside it quietly edited that widget, in the layout you had left. And
underneath both, a show file whose active_layout named a layout that was not in
it left every one of those paths pointing at nothing at all.

Reported later, against the same page: "when adding a widget to a position that
already has a widget it correctly nudges it so they don't overlap, however if
you then move that nudged widget it keeps its nudge even if there isn't another
widget in the position it moved to". The move is the Corner box on the Position
tab, and the nudge is the push _clear_of_others makes to keep a new widget off
the one already in its corner. That push was written straight into the widget's
offsets, where it was indistinguishable from a number the operator had typed,
so a date shoved a third of the way down the frame to clear a clock carried
that third of a frame to an empty bottom left and hung in mid-air. The last
section here is that push: what it does, and where it stops being Wer's.

The ready-made layouts and their two buttons live in
test_layout_editor_presets.py; the LayoutSet itself in test_layout.py; dragging,
the arrow keys and the arithmetic of stacking in test_snapping.py.
"""

from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtWidgets import QMessageBox

from wer.overlay.geometry import Anchor, Placement
from wer.overlay.layout import Layout, LayoutSet, default_layouts


@pytest.fixture()
def editor(qt_app):
    """A real LayoutEditor over a real Compositor, as the tab has."""
    from wer.core.databus import DataBus
    from wer.overlay.compositor import Compositor
    from wer.ui.layout_editor import LayoutEditor

    compositor = Compositor(DataBus())
    panel = LayoutEditor(compositor)
    yield panel, compositor
    panel.deleteLater()


@pytest.fixture()
def window(qt_app):
    """The whole app, wired the way the Overlay tab actually is."""
    from wer.ui.main_window import MainWindow

    win = MainWindow()
    # Whatever an earlier test autosaved into the throwaway data directory,
    # these start from the shipped three.
    win.layouts = LayoutSet(default_layouts(), "Tech")
    win.editor.set_layouts(win.layouts)
    win._rebuild_layout_menu()
    win.switch_layout("Tech")
    yield win
    win.close()
    win.deleteLater()


def _say_yes(monkeypatch):
    # A plain int, because that is what the real dialog returns in this
    # PySide6. Stubbing the enum member hid a delete that compared by identity
    # and never once deleted anything in the running program.
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: int(QMessageBox.StandardButton.Yes)
    )


def _names_in_box(panel) -> list[str]:
    return [panel._layout_box.itemText(i) for i in range(panel._layout_box.count())]


# ------------------------------------------------------------ deleting a layout


def test_delete_redraws_the_layout_bar_itself(editor, monkeypatch) -> None:
    """The bar has to describe the set it is looking at, switch or no switch.

    _delete_layout left redrawing to the layout_switch_requested it emits, so
    the box went on listing the deleted layout -- and offering to delete it
    again -- for as long as nothing came back the other way.
    """
    panel, _compositor = editor
    _say_yes(monkeypatch)
    layouts = LayoutSet(default_layouts() + [Layout("Mine")], "Tech")
    panel.set_layouts(layouts)

    panel._delete_layout_button.click()

    assert layouts.names == ["Minimal", "None", "Mine"]
    assert _names_in_box(panel) == ["Minimal", "None", "Mine"], "the box still offers Tech"
    assert panel._layout_box.currentText() == layouts.active_name


def test_delete_greys_itself_once_one_layout_is_left(editor, monkeypatch) -> None:
    """A show with no layout has no overlay and no way back, so the last one
    cannot go -- and the button has to say so the moment it is the last one."""
    panel, _compositor = editor
    _say_yes(monkeypatch)
    panel.set_layouts(LayoutSet(default_layouts(), "Tech"))
    assert panel._delete_layout_button.isEnabled()

    for _ in range(len(default_layouts()) - 1):
        panel._delete_layout_button.click()

    assert panel._delete_layout_button.isEnabled() is False


def test_answering_no_deletes_nothing(editor, monkeypatch) -> None:
    panel, _compositor = editor
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: int(QMessageBox.StandardButton.No)
    )
    layouts = LayoutSet(default_layouts(), "Tech")
    panel.set_layouts(layouts)

    panel._delete_layout_button.click()

    assert layouts.names == ["Tech", "Minimal", "None"]
    assert _names_in_box(panel) == ["Tech", "Minimal", "None"]


def test_deleting_the_live_layout_puts_another_one_on_the_picture(
    window, monkeypatch
) -> None:
    _say_yes(monkeypatch)
    window.editor._delete_layout_button.click()

    assert window.layouts.names == ["Minimal", "None"]
    assert window.layouts.active_name == "Minimal"
    assert [w.id for w in window.compositor.widgets] == [
        w.id for w in window.layouts.get("Minimal").widgets
    ]
    assert [a.text().lstrip("&") for a in window._layout_actions] == ["Minimal", "None"]
    window._autosave()
    assert [entry["name"] for entry in window.show_file.layouts] == ["Minimal", "None"]
    assert window.show_file.active_layout == "Minimal"


def test_a_deleted_layouts_hotkey_does_not_outlive_it(window, monkeypatch) -> None:
    """Every action is parented to the window, so removing it from the menu left
    it alive and findable -- another pair of them on every rebuild."""
    from PySide6.QtGui import QAction

    _say_yes(monkeypatch)
    window.editor._delete_layout_button.click()

    named_tech = [a for a in window.findChildren(QAction) if a.text().lstrip("&") == "Tech"]
    assert named_tech == []


def test_a_layout_switch_to_a_name_that_has_gone_puts_the_box_back(window) -> None:
    """A console macro can name a layout that was deleted this afternoon.

    switch_layout returned without touching the editor, so whatever the box was
    left showing stayed there.
    """
    window.switch_layout("Minimal")
    window.layouts.remove("Tech")

    window.switch_layout("Tech")

    assert window.layouts.active_name == "Minimal"
    assert _names_in_box(window.editor) == ["Minimal", "None"]


def test_the_empty_layout_can_be_made_live_and_left_again(window) -> None:
    """None holds no widgets, and every path that ends at the compositor has to
    cope with that: the picture comes out clean, the editor has nothing to
    offer, and the layout it came from is still whole when you switch back."""
    window.switch_layout("None")

    assert window.layouts.active_name == "None"
    assert window.compositor.widgets == []
    assert window.editor._current is None
    assert window.editor._remove.isEnabled() is False
    frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    window.compositor.composite(frame, in_place=True)
    assert not frame.any(), "None drew something onto the recording"

    window.switch_layout("Tech")
    assert [w.id for w in window.compositor.widgets] == [
        w.id for w in window.layouts.get("Tech").widgets
    ]


# ------------------------------------------------------------ removing a widget


def test_remove_takes_the_widget_out_of_the_layout_and_the_show(window) -> None:
    editor = window.editor
    editor.select("cue_countdown")
    left = ["cue", "show_name", "elapsed", "clock_date", "cue_timer"]

    editor._remove.click()

    assert [w.id for w in window.compositor.widgets] == left
    assert [w.id for w in window.layouts.get("Tech").widgets] == left
    window.switch_layout("Minimal")
    window.switch_layout("Tech")
    assert [w.id for w in window.compositor.widgets] == left, (
        "the widget came back with its layout"
    )
    window._autosave()
    saved = next(e for e in window.show_file.layouts if e["name"] == "Tech")
    assert [w["id"] for w in saved["widgets"]] == left


def test_remove_tells_the_preview_to_drop_its_outline(editor) -> None:
    """In edit mode the removed widget's outline stayed on the picture."""
    from wer.overlay.presets import make_widget

    panel, compositor = editor
    clock = compositor.add(make_widget("clock_24", []))
    panel.refresh()
    panel.select(clock.id)
    seen: list[str] = []
    panel.selection_changed.connect(seen.append)

    panel._remove.click()

    assert compositor.get(clock.id) is None
    assert seen[-1] == "", "the preview was never told the selection had gone"


def test_a_selection_does_not_survive_the_layout_it_belonged_to(window) -> None:
    """Remove was offered for a widget this compositor does not hold, and did
    nothing when pressed -- which is how "we aren't able to delete overlays"
    reads from the booth."""
    editor = window.editor
    # One Tech has and Minimal does not: Tech and Minimal both carry a
    # "show_name" and an "elapsed", and a selection with the same id in both
    # is meant to follow the switch rather than be dropped.
    editor.select("cue_countdown")
    assert editor._current is not None

    window.switch_layout("Minimal")

    assert editor._current is None
    assert editor._remove.isEnabled() is False
    assert editor._duplicate.isEnabled() is False
    assert editor._properties.isEnabled() is False


def test_the_property_sheet_does_not_edit_the_layout_you_left(window) -> None:
    """The quieter half of the same fault: the sheet stayed live on the old
    widget, so a nudge meant for what was on screen went into a layout that
    was not, with nothing on the picture to show for it."""
    editor = window.editor
    editor.select("clock_date")
    before = editor._current.z_order

    window.switch_layout("Minimal")
    editor._z_order.setValue(before + 123)

    kept = next(w for w in window.layouts.get("Tech").widgets if w.id == "clock_date")
    assert kept.z_order == before


# ------------------------------------------------- the push that clears a corner

WIDTH, HEIGHT = 1920, 1080


def _draw(compositor) -> None:
    """Composite one frame, which is how the editor learns the frame size and
    how big each widget drew. Nothing is pushed clear of anything without it."""
    compositor.composite(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))


def _rect(compositor, widget_id: str):
    """Where a widget sits on the picture, in frame pixels."""
    widget = compositor.get(widget_id)
    return widget.placement.resolve(
        *compositor.frame_size, *compositor.widget_size(widget_id)
    )


def _set_corner(panel, anchor: Anchor) -> None:
    """Choose a corner on the Position tab, the way the operator does."""
    panel._anchor.setCurrentIndex(list(Anchor).index(anchor))


@pytest.fixture()
def occupied(qt_app):
    """An editor over a compositor that has drawn a frame, with the top right
    and the bottom left already taken.

    Two corners, because the fault needs somewhere occupied to be pushed out of
    and somewhere empty to be sent to. Plain text rather than presets: a widget
    that renders nothing has no size, and a widget with no size is not
    something anything can be stacked against.
    """
    from wer.core.databus import DataBus
    from wer.overlay.compositor import Compositor
    from wer.overlay.style import TextStyle
    from wer.overlay.widgets import TextWidget
    from wer.ui.layout_editor import LayoutEditor

    compositor = Compositor(DataBus())
    compositor.add(
        TextWidget("clock", "21:57:31", placement=Placement(Anchor.TOP_RIGHT),
                   style=TextStyle(size=0.034), z_order=20)
    )
    compositor.add(
        TextWidget("prompt", "LIVE: Chan 1 At Full",
                   placement=Placement(Anchor.BOTTOM_LEFT),
                   style=TextStyle(size=0.030), z_order=20)
    )
    _draw(compositor)
    panel = LayoutEditor(compositor)
    panel.refresh()
    yield panel, compositor
    panel.deleteLater()


def test_a_widget_added_to_an_occupied_corner_is_pushed_clear_of_it(occupied) -> None:
    """The behaviour the complaint begins by praising, guarded: the date goes
    under the clock rather than exactly on it."""
    panel, compositor = occupied

    panel._add("date")
    _draw(compositor)

    assert panel._current.placement.anchor is Anchor.TOP_RIGHT
    assert panel._current.placement.offset_y > 0.0, "the date was left on the clock"
    assert not _rect(compositor, "date").intersects(_rect(compositor, "clock"))


def test_a_corner_change_does_not_carry_the_push_to_an_empty_corner(occupied) -> None:
    """The complaint itself. Sent to a corner with nothing in it, the widget
    belongs in that corner -- not a third of a frame below it, because of a
    clock it is no longer anywhere near."""
    panel, compositor = occupied
    panel._add("date")
    _draw(compositor)
    assert panel._current.placement.offset_y > 0.0

    _set_corner(panel, Anchor.TOP_LEFT)

    assert panel._current.placement.anchor is Anchor.TOP_LEFT
    assert panel._current.placement.offset_y == pytest.approx(0.0), (
        "the push made at the old corner came with it"
    )
    assert panel._current.placement.offset_x == pytest.approx(0.0)


def test_a_corner_change_into_an_occupied_corner_pushes_again(occupied) -> None:
    """Setting a corner is the same act as adding to one, so the widget is put
    clear of what is already there -- upwards at the bottom, away from the edge
    it hangs from."""
    panel, compositor = occupied
    panel._add("date")
    _draw(compositor)

    _set_corner(panel, Anchor.BOTTOM_LEFT)

    assert panel._current.placement.offset_y < 0.0, "the date was put on the prompt"
    assert not _rect(compositor, "date").intersects(_rect(compositor, "prompt"))


def test_a_push_made_by_one_corner_change_comes_off_at_the_next(occupied) -> None:
    """A push is Wer's wherever it was made, not only at the add. Sent to the
    bottom left the date is put above the prompt; sent on to an empty top left
    it belongs at the top left, and the shove the prompt gave it is no more
    the operator's than the shove the clock gave it.

    It is also what says the Position tab is filled in quietly: written
    noisily, the boxes would read as the operator typing those offsets and the
    push would become theirs the moment it was made.
    """
    panel, compositor = occupied
    panel._add("date")
    _draw(compositor)

    _set_corner(panel, Anchor.BOTTOM_LEFT)
    assert panel._current.placement.offset_y < 0.0, "the date was put on the prompt"

    _set_corner(panel, Anchor.TOP_LEFT)

    assert panel._current.placement.offset_y == pytest.approx(0.0), (
        "the push the prompt made came with it to an empty corner"
    )


def test_a_backing_strip_sent_to_a_corner_is_not_pushed_off_what_sits_on_it(
    occupied,
) -> None:
    """A shape is the one thing that is meant to be under another widget, so
    nothing is cleared out of its way: adding a backing strip does not push it
    off the readouts it is added behind, and setting its Corner is the same
    act. Pushed, the strip slid out from under the clock it was put there to
    back, and the operator's answer would be to drag it back by hand.
    """
    from wer.overlay.widgets import PanelWidget

    panel, compositor = occupied
    strip = PanelWidget(
        "strip", height=0.10, placement=Placement(Anchor.TOP_LEFT), z_order=-10
    )
    compositor.add(strip)
    _draw(compositor)
    panel.refresh()
    panel.select("strip")

    _set_corner(panel, Anchor.TOP_RIGHT)

    assert strip.placement.anchor is Anchor.TOP_RIGHT
    assert strip.placement.offset_y == pytest.approx(0.0), (
        "the strip was pushed off the clock it is meant to sit behind"
    )


def test_a_nudge_typed_in_by_hand_survives_a_corner_change(occupied) -> None:
    """Once the operator has typed a number into Nudge down it is theirs, and
    a corner change may not take a slice off it on the way past."""
    panel, compositor = occupied
    panel._add("date")
    _draw(compositor)
    panel._offset_y.setValue(0.180)

    _set_corner(panel, Anchor.TOP_LEFT)

    assert panel._current.placement.offset_y == pytest.approx(0.180)


def test_the_arrow_keys_end_the_automatic_push(occupied) -> None:
    """A widget moved a pixel at a time is where the operator put it, whatever
    Wer did with it when it was added."""
    panel, compositor = occupied
    panel._add("date")
    _draw(compositor)
    placed = panel._current.placement.offset_y

    panel.nudge("date", 0.0, 0.010)
    _set_corner(panel, Anchor.TOP_LEFT)

    assert panel._current.placement.offset_y == pytest.approx(placed + 0.010)


def test_a_drag_ends_the_automatic_push(occupied) -> None:
    panel, compositor = occupied
    panel._add("date")
    _draw(compositor)
    placed = panel._current.placement.offset_y

    panel.begin_drag("date")
    panel.drag("date", 0.0, 0.050, 0.0, "")   # no snap distance: no snapping
    panel.end_drag("date", False)
    dragged = panel._current.placement.offset_y
    assert dragged == pytest.approx(placed + 0.050)

    _set_corner(panel, Anchor.TOP_LEFT)

    assert panel._current.placement.offset_y == pytest.approx(dragged)


def test_clicking_a_widget_on_the_preview_is_not_placing_it(occupied) -> None:
    """A click on the picture is how the widget whose Corner you are about to
    change is chosen, and it reaches the editor as a drag that went nowhere:
    button down, button up, nothing moved in between. Counted as a placement
    it threw the push away one click before the corner change that had to take
    it off -- the reported fault back again, by the shortest route to it.
    """
    panel, compositor = occupied
    panel._add("date")
    _draw(compositor)

    panel.begin_drag("date")
    panel.end_drag("date", False)  # pressed and let go: the widget never moved

    _set_corner(panel, Anchor.TOP_LEFT)

    assert panel._current.placement.offset_y == pytest.approx(0.0), (
        "a click that moved nothing was taken for the operator placing it"
    )


def test_a_cancelled_drag_leaves_the_push_where_it_was(occupied) -> None:
    """Escape puts the widget back exactly as it was, push and all, so the push
    is still Wer's to take off at the next corner change."""
    panel, compositor = occupied
    panel._add("date")
    _draw(compositor)
    placed = panel._current.placement.offset_y

    panel.begin_drag("date")
    panel.drag("date", 0.0, 0.050, 0.0, "")
    panel.end_drag("date", True)
    assert panel._current.placement.offset_y == pytest.approx(placed)

    _set_corner(panel, Anchor.TOP_LEFT)

    assert panel._current.placement.offset_y == pytest.approx(0.0)


def test_the_position_tab_shows_the_offsets_a_corner_change_leaves(occupied) -> None:
    """The nudges are the only place the number can be read before it reaches
    the show file, so they have to say what a corner change made of them."""
    panel, compositor = occupied
    panel._add("date")
    _draw(compositor)
    assert panel._offset_y.value() > 0.0, "the sheet never showed the push at all"

    _set_corner(panel, Anchor.TOP_LEFT)
    # Compared with ==: the box hands back the bare string it was given, the
    # way the dialogs hand back a bare int, and Anchor subclasses str.
    assert panel._anchor.currentData() == Anchor.TOP_LEFT
    assert panel._offset_y.value() == pytest.approx(0.0)

    _set_corner(panel, Anchor.BOTTOM_LEFT)
    # The box holds three decimals; what matters is that it agrees with the
    # widget rather than going on showing the offsets of a corner ago.
    assert panel._offset_y.value() == pytest.approx(
        panel._current.placement.offset_y, abs=5e-4
    )
    assert panel._offset_y.value() < 0.0


def test_a_push_does_not_outlive_the_layout_it_was_made_in(occupied) -> None:
    """Every layout has its own "date", and the one in the next layout was
    never pushed anywhere -- its offsets are whatever the show file says."""
    from wer.overlay.style import TextStyle
    from wer.overlay.widgets import TextWidget

    panel, compositor = occupied
    panel._add("date")
    _draw(compositor)
    assert panel._current.placement.offset_y > 0.0

    other = TextWidget(
        "date", "2026-09-14",
        placement=Placement(Anchor.TOP_RIGHT, offset_y=0.400),
        style=TextStyle(size=0.024),
    )
    compositor.apply_layout([compositor.get("clock"), other])
    panel.refresh()
    _draw(compositor)
    panel.select("date")

    _set_corner(panel, Anchor.MIDDLE_LEFT)

    assert other.placement.offset_y == pytest.approx(0.400), (
        "a push made in another layout was taken off a widget that never had one"
    )
