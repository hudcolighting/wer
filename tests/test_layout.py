"""Layouts: serialisation, switching, and the defaults."""

from __future__ import annotations

import json

import numpy as np
import pytest

from wer.core.databus import DataBus
from wer.overlay.geometry import Anchor, Placement
from wer.overlay.layout import (
    Layout,
    LayoutSet,
    default_layouts,
    widget_from_dict,
    widget_to_dict,
)
from wer.overlay.style import Align, Colour, TextStyle
from wer.overlay.widgets import CueWidget, TextWidget, VisibilityRule


# ------------------------------------------------------------- serialisation


def test_text_widget_round_trips(qt_app) -> None:
    original = TextWidget(
        "cmd", "Cue {eos.cue.active.number}",
        placement=Placement(Anchor.BOTTOM_RIGHT, offset_x=0.05, offset_y=-0.02),
        style=TextStyle(size=0.037, colour=Colour.from_hex("#ff8800"),
                        align=Align.RIGHT, shadow=False),
        z_order=17,
        visibility=VisibilityRule(only_when_present="eos.cmdline.text", hide_after=5.0),
    )
    restored = widget_from_dict(widget_to_dict(original))

    assert isinstance(restored, TextWidget)
    assert restored.id == "cmd"
    assert restored.template == original.template
    assert restored.z_order == 17
    assert restored.placement == original.placement
    assert restored.style.size == pytest.approx(0.037)
    assert restored.style.colour.to_hex() == "#ff8800"
    assert restored.style.align is Align.RIGHT
    assert restored.style.shadow is False
    assert restored.visibility.only_when_present == "eos.cmdline.text"
    assert restored.visibility.hide_after == 5.0


def test_cue_widget_round_trips(qt_app) -> None:
    original = CueWidget("cue", namespace="eos2", show_pending=False,
                         show_progress=False, show_label=True, z_order=3)
    restored = widget_from_dict(widget_to_dict(original))

    assert isinstance(restored, CueWidget)
    assert restored.ns == "eos2"
    assert restored.show_pending is False
    assert restored.show_progress is False
    assert restored.show_label is True


def test_a_cue_stack_round_trips(qt_app) -> None:
    original = CueWidget("cue", show_previous=True, show_list=False)
    restored = widget_from_dict(json.loads(json.dumps(widget_to_dict(original))))

    assert isinstance(restored, CueWidget)
    assert restored.show_previous is True
    assert restored.show_list is False


def test_a_two_line_clock_with_a_width_cap_round_trips(qt_app) -> None:
    """Through real JSON, as the show file is written, line break and all."""
    original = TextWidget(
        "clock", "{clock.wall}\n{clock.date_long}",
        emphasise_first_line=True, max_width=0.35,
    )
    restored = widget_from_dict(json.loads(json.dumps(widget_to_dict(original))))

    assert isinstance(restored, TextWidget)
    assert restored.template == "{clock.wall}\n{clock.date_long}"
    assert restored.emphasise_first_line is True
    assert restored.max_width == pytest.approx(0.35)


def test_a_panel_round_trips(qt_app) -> None:
    from wer.overlay.style import BoxStyle
    from wer.overlay.widgets import PanelWidget

    fill = Colour(10, 20, 30, 150)
    original = PanelWidget(
        "strip", width=0.8, height=0.12, z_order=1,
        style=TextStyle(box=BoxStyle(fill=fill, corner_radius=0.0)),
    )
    data = json.loads(json.dumps(widget_to_dict(original)))
    restored = widget_from_dict(data)

    assert data["type"] == "panel"
    assert isinstance(restored, PanelWidget)
    assert (restored.width, restored.height) == (0.8, 0.12)
    assert restored.style.box.fill == fill
    assert restored.style.box.corner_radius == 0.0


def test_a_fade_bar_round_trips(qt_app) -> None:
    from wer.overlay.widgets import FadeBarWidget

    original = FadeBarWidget(
        "fade", namespace="eos2", width=0.5, height=0.02, hide_when_idle=False
    )
    data = json.loads(json.dumps(widget_to_dict(original)))
    restored = widget_from_dict(data)

    assert data["type"] == "fadebar"
    assert isinstance(restored, FadeBarWidget)
    assert (restored.ns, restored.width, restored.height, restored.hide_when_idle) == (
        "eos2", 0.5, 0.02, False
    )


def test_settings_a_show_file_does_not_mention_take_their_defaults(qt_app) -> None:
    """Show files written before the width cap, the cue stack and two-line text
    existed have none of those keys, and must open looking exactly as they
    did. A hand-written panel or fade bar gets the sizes the presets start
    from."""
    from wer.overlay.widgets import FadeBarWidget, PanelWidget

    text = widget_from_dict({"type": "text", "id": "a", "template": "{x}"})
    assert (text.max_width, text.emphasise_first_line) == (0.0, False)

    cue = widget_from_dict({"type": "cue", "id": "c", "max_width": None})
    assert (cue.max_width, cue.show_previous, cue.show_list) == (0.0, False, True)

    panel = widget_from_dict({"type": "panel", "id": "p"})
    assert isinstance(panel, PanelWidget)
    assert (panel.width, panel.height) == (1.0, 0.10)

    fade = widget_from_dict({"type": "fadebar", "id": "f"})
    assert isinstance(fade, FadeBarWidget)
    assert (fade.ns, fade.width, fade.height, fade.hide_when_idle) == (
        "eos", 0.30, 0.012, True
    )


def test_a_widget_from_a_newer_version_is_skipped_not_fatal(qt_app) -> None:
    """A show file with one unknown widget should still open."""
    assert widget_from_dict({"type": "hologram", "id": "x"}) is None


def test_a_layout_with_an_unknown_widget_still_loads_the_rest(qt_app) -> None:
    data = {
        "name": "Mixed",
        "widgets": [
            {"type": "text", "id": "a", "template": "{x}"},
            {"type": "hologram", "id": "b"},
            {"type": "cue", "id": "c"},
        ],
    }
    layout = Layout.from_dict(data)
    assert [w.id for w in layout.widgets] == ["a", "c"]


def test_a_layout_serialises_to_json(qt_app) -> None:
    """The show file has to be human-readable and diffable."""
    layout = Layout("Tech", default_layouts()[0].widgets)
    text = json.dumps(layout.to_dict(), indent=2, sort_keys=True)
    assert '"name": "Tech"' in text
    assert json.loads(text)["widgets"]


def test_missing_fields_fall_back_to_defaults(qt_app) -> None:
    """Hand-editing a show file must not require writing out every key."""
    widget = widget_from_dict({"type": "text", "id": "a", "template": "{x}"})
    assert widget is not None
    assert widget.placement.anchor is Anchor.TOP_LEFT
    assert widget.style.size == TextStyle().size


def test_a_nonsense_anchor_does_not_break_loading(qt_app) -> None:
    widget = widget_from_dict(
        {"type": "text", "id": "a", "template": "{x}",
         "placement": {"anchor": "middle-of-nowhere"}}
    )
    assert widget is not None
    assert widget.placement.anchor is Anchor.TOP_LEFT


# -------------------------------------------------------------------- the set


def test_defaults_are_tech_minimal_and_none(qt_app) -> None:
    names = [layout.name for layout in default_layouts()]
    assert names == ["Tech", "Minimal", "None"]


def test_each_default_is_quieter_than_the_one_before(qt_app) -> None:
    """Tech is the whole tech table, Minimal three readouts, None the footage."""
    tech, minimal, nothing = default_layouts()
    assert len(tech.widgets) > len(minimal.widgets) > len(nothing.widgets)
    assert {w.id for w in minimal.widgets} == {"cue_number", "elapsed", "show_name"}
    assert nothing.widgets == [], "None is the picture on its own"


def test_the_widgets_tech_ships_with(qt_app) -> None:
    """What a new show file gets before anyone has opened the Overlay tab.

    Pinned field by field because it is the overlay most recordings are made
    with, and a widget quietly dropped or moved here is only noticed on the
    footage afterwards.
    """
    tech = default_layouts()[0]
    assert [
        (w.id, widget_to_dict(w)["type"], w.placement.anchor, w.z_order)
        for w in tech.widgets
    ] == [
        ("cue", "cue", Anchor.TOP_LEFT, 10),
        ("show_name", "text", Anchor.TOP_CENTER, 40),
        ("elapsed", "text", Anchor.TOP_RIGHT, 50),
        ("clock_date", "text", Anchor.TOP_RIGHT, 60),
        ("cue_countdown", "text", Anchor.BOTTOM_LEFT, 70),
        ("cue_timer", "cuetimer", Anchor.BOTTOM_LEFT, 80),
    ]
    by_id = {w.id: w for w in tech.widgets}
    assert by_id["show_name"].template == "{eos.show.name}"
    assert by_id["elapsed"].template == "{clock.elapsed}"
    assert by_id["clock_date"].template == "{clock.wall12}\n{clock.date_long}"
    assert by_id["clock_date"].emphasise_first_line is True
    assert by_id["cue_countdown"].template == "{eos.cue.active.remaining}s left"
    assert by_id["cue_timer"].count_from == "fade_start"
    assert by_id["cue_timer"].prefix == "In cue "
    # The two that grow with whatever the desk and the show file put in them
    # share the top band with the clock, so each is held to its own lane.
    assert by_id["cue"].max_width == 0.34
    assert by_id["show_name"].max_width == 0.26
    assert by_id["elapsed"].placement.offset_y == pytest.approx(0.133)
    assert by_id["cue_timer"].placement.offset_y == pytest.approx(-0.095)


def test_techs_bottom_left_reads_timer_above_countdown(qt_app) -> None:
    """Both hang off the same corner, and one offset decides which is which.

    The order they read in is what the New layout dialog and the help page say
    Tech looks like, and a sign read the wrong way round there describes a
    layout nobody has. Measured off the drawn rectangles rather than inferred
    from the offset, because the corner a widget hangs from decides which way
    its own height pushes it.
    """
    from wer.overlay.compositor import Compositor
    from wer.overlay.sample import sample_bus, sample_frame

    comp = Compositor(sample_bus())
    comp.apply_layout(default_layouts()[0].widgets)
    comp.composite(sample_frame(1920, 1080), in_place=True)

    where = {
        widget.id: widget.placement.resolve(
            1920, 1080, *comp.widget_size(widget.id)
        )
        for widget in comp.widgets
        if comp.widget_size(widget.id) is not None
    }
    assert where["cue_timer"].bottom <= where["cue_countdown"].y, (
        "how long the cue has been up belongs above the seconds left in it"
    )
    assert where["elapsed"].y >= where["clock_date"].bottom, (
        "the record time belongs under the clock and date"
    )


def test_the_widgets_minimal_ships_with(qt_app) -> None:
    minimal = default_layouts()[1]
    assert [
        (w.id, w.template, w.placement.anchor, w.z_order) for w in minimal.widgets
    ] == [
        ("cue_number", "Cue {eos.cue.active.number}", Anchor.BOTTOM_LEFT, 10),
        ("elapsed", "{clock.elapsed}", Anchor.BOTTOM_RIGHT, 20),
        ("show_name", "{eos.show.name}", Anchor.TOP_LEFT, 30),
    ]
    for widget in minimal.widgets:
        assert widget.style.box.enabled, f"{widget.id} lost its backing box"
        assert widget.style.size == pytest.approx(0.030)


def test_minimals_show_name_is_safe_uncapped_because_it_has_its_band_alone(
    qt_app,
) -> None:
    """The one default widget that grows with the desk and carries no lane.

    Tech's cue block and show name are capped because the clock is beside
    them. Minimal's show name is not, and the reason is not that show names
    are short -- a show name is whatever the desk's file happens to be called,
    and a long one draws clear across the top of the picture. The reason is
    that the cue number and the record time are both down in the bottom
    corners, so there is nothing up there to run into. Put a second widget in
    that band and the show name wants a ``max_width`` first; this is the check
    that says so.
    """
    from wer.overlay.compositor import Compositor
    from wer.overlay.sample import sample_bus, sample_frame

    minimal = default_layouts()[1]
    assert [w.id for w in minimal.widgets if w.max_width] == [], (
        "nothing on Minimal is held to a lane"
    )

    bus = sample_bus()
    bus.publish(
        "eos.show.name",
        "The Tragedie of Hamlet, Prince of Denmarke, " * 5,
        source_connection_id="test",
    )
    comp = Compositor(bus)
    comp.apply_layout(minimal.widgets)
    comp.composite(sample_frame(1920, 1080), in_place=True)
    where = {
        widget.id: widget.placement.resolve(
            1920, 1080, *comp.widget_size(widget.id)
        )
        for widget in comp.widgets
        if comp.widget_size(widget.id) is not None
    }

    assert where["show_name"].width > 1920 // 2, (
        "a name this long should be running away with the top of the picture; "
        "if it is not, this test has stopped checking anything"
    )
    assert where["show_name"].x >= 0 and where["show_name"].right <= 1920, (
        "the show name ran off the picture"
    )
    # Every other widget on the layout, not the two it has today: a widget
    # added to Minimal's top band is exactly the change this is here to catch.
    for other in (name for name in where if name != "show_name"):
        assert not where["show_name"].intersects(where[other]), (
            f"the show name grew into {other}, which is what a lane is for"
        )


def test_activating_switches(qt_app) -> None:
    layouts = LayoutSet(default_layouts(), "Tech")
    assert layouts.active_name == "Tech"
    assert layouts.activate("Minimal") is not None
    assert layouts.active_name == "Minimal"


def test_activating_something_that_does_not_exist_is_refused(qt_app) -> None:
    layouts = LayoutSet(default_layouts(), "Tech")
    assert layouts.activate("Nope") is None
    assert layouts.active_name == "Tech", "the live layout must not be lost"


def test_next_cycles_and_wraps(qt_app) -> None:
    layouts = LayoutSet(default_layouts(), "Tech")
    assert layouts.next().name == "Minimal"
    assert layouts.next().name == "None", "an empty layout is cycled to like any other"
    assert layouts.next().name == "Tech"


def test_the_last_layout_cannot_be_removed(qt_app) -> None:
    """A show with no layout at all has no overlay and no way back."""
    layouts = LayoutSet([Layout("Only")], "Only")
    layouts.remove("Only")
    assert len(layouts) == 1


def test_adding_a_layout_with_an_existing_name_replaces_it(qt_app) -> None:
    layouts = LayoutSet(default_layouts(), "Tech")
    layouts.add(Layout("Tech", []))
    assert len(layouts) == 3
    assert layouts.get("Tech").widgets == []


def test_the_set_round_trips_through_json(qt_app) -> None:
    original = LayoutSet(default_layouts(), "Minimal")
    data = json.loads(json.dumps(original.to_list()))
    restored = LayoutSet.from_list(data, "Minimal")
    assert restored.names == original.names
    assert restored.active_name == "Minimal"
    tech = restored.get("Tech")
    assert [w.id for w in tech.widgets] == [
        "cue", "show_name", "elapsed", "clock_date", "cue_countdown", "cue_timer",
    ]
    assert restored.get("None").widgets == [], "the empty layout came back empty"


def test_a_show_saved_on_the_empty_layout_reopens_on_it(qt_app) -> None:
    """None holds no widgets, so nothing about it says it is there but its name.

    A set that lost it on the way through the file would put the overlay back
    onto a stretch that was being recorded clean, and nothing on screen would
    say why.
    """
    original = LayoutSet(default_layouts(), "None")
    restored = LayoutSet.from_list(json.loads(json.dumps(original.to_list())), "None")

    assert restored.active_name == "None"
    assert restored.active is not None
    assert restored.active.widgets == []


def test_an_empty_show_file_gets_the_defaults(qt_app) -> None:
    """Usable before anything has been configured."""
    assert LayoutSet.from_list([]).names == ["Tech", "Minimal", "None"]


def test_an_active_name_no_layout_answers_to_falls_back(qt_app, caplog) -> None:
    """A show file is free to disagree with itself, and one did.

    ``active_layout`` naming a layout whose entry in ``layouts`` did not
    survive -- an empty list, a hand edit -- left the set with an active
    layout that resolved to nothing, and every symptom of that was silence:
    a blank overlay, an empty widget list, edits dropped on the next switch,
    and a Delete button that removed nothing.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="wer.overlay.layout"):
        layouts = LayoutSet.from_list([], "Programming")

    assert layouts.active_name == "Tech"
    assert layouts.active is not None, "the active name must answer to a layout"
    assert "Programming" in caplog.text, "the log has to say the file disagreed"


def test_losing_every_saved_layout_to_the_defaults_is_said_out_loud(
    qt_app, caplog
) -> None:
    """Substituting the shipped layouts is right; doing it in silence is not.

    A show file whose layouts list survived as something other than a list of
    objects got the ready-made ones back with nothing in the log, which reads
    from the booth as "my overlays have vanished" -- and it is the same fault
    that left the active name pointing at nothing.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="wer.overlay.layout"):
        layouts = LayoutSet.from_list(["Tech", "Minimal"], "Tech")

    assert layouts.names == ["Tech", "Minimal", "None"]
    assert "2 saved layout" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="wer.overlay.layout"):
        LayoutSet.from_list([])
    assert caplog.text == "", "a first run has lost nothing"


def test_an_ordinary_delete_does_not_cry_wolf(qt_app, caplog) -> None:
    """The warning above is the line someone is asked to look for in the log.

    remove() takes the live layout away on purpose, so settling the active name
    afterwards is the expected end of a Delete, not a file disagreeing with
    itself. Warning on both put the same line in the log for the commonest
    thing you can do on the Overlay tab, and left it meaning nothing.
    """
    import logging

    layouts = LayoutSet(default_layouts(), "Tech")
    with caplog.at_level(logging.WARNING, logger="wer.overlay.layout"):
        layouts.remove("Tech")

    assert layouts.active_name == "Minimal"
    assert caplog.text == "", "an ordinary delete has nothing to report"


def test_removing_a_layout_always_leaves_a_live_one(qt_app) -> None:
    layouts = LayoutSet(default_layouts() + [Layout("Mine")], "Minimal")
    layouts.remove("Minimal")
    assert layouts.active_name in layouts.names
    assert layouts.active is not None
    layouts.remove("Tech")
    assert layouts.active is not None


# ------------------------------------------------------------- hot switching


def test_applying_a_layout_swaps_the_whole_widget_set(qt_app) -> None:
    from wer.overlay.compositor import Compositor

    bus = DataBus()
    comp = Compositor(bus)
    tech, minimal, nothing = default_layouts()

    comp.apply_layout(tech.widgets)
    assert [w.id for w in comp.widgets] == [
        "cue", "show_name", "elapsed", "clock_date", "cue_countdown", "cue_timer",
    ]

    comp.apply_layout(minimal.widgets)
    assert [w.id for w in comp.widgets] == ["cue_number", "elapsed", "show_name"]

    comp.apply_layout(nothing.widgets)
    assert comp.widgets == [], "switching to None left widgets on the picture"


def test_switching_layouts_keeps_compositing_working(qt_app) -> None:
    """The switch happens on a hotkey mid-recording; the next frame must draw."""
    from wer.overlay.compositor import Compositor

    bus = DataBus()
    bus.publish("eos.cue.active.number", "58", source_connection_id="eos")
    bus.publish("clock.wall", "21:57:31", source_connection_id="system")
    comp = Compositor(bus)
    tech, minimal, nothing = default_layouts()

    for layout in (tech, minimal, tech):
        comp.apply_layout(layout.widgets)
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        comp.composite(frame, in_place=True)
        assert frame.any(), f"nothing drawn after switching to {layout.name}"

    comp.apply_layout(nothing.widgets)
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    comp.composite(frame, in_place=True)
    assert not frame.any(), "None put something on the picture"


def test_switching_does_not_leak_subscriptions(qt_app) -> None:
    """Repeated switching over a long tech must not accumulate callbacks."""
    from wer.overlay.compositor import Compositor

    bus = DataBus()
    comp = Compositor(bus)
    tech, minimal, _nothing = default_layouts()

    for _ in range(20):
        comp.apply_layout(tech.widgets)
        comp.apply_layout(minimal.widgets)

    # Counted on the bus, which is where a leak actually lives, and against the
    # exact number the live layout needs rather than a loose ceiling.
    #
    # Both halves matter. The compositor's own subscription dict is keyed by
    # widget id, so it is bounded by the widget count however badly switching
    # leaks: counting it made this test unable to fail for the reason it was
    # written. And a ceiling of "under 30" passed at 9 as happily as at 7, so
    # the one extra round of bindings left behind by a missing cancel went
    # unnoticed. Measured: 7 here, 9 with Compositor._cancel_subscriptions
    # stubbed out, 360 with the per-widget cancel removed as well.
    expected = sum(len(widget.bus_keys) for widget in minimal.widgets)
    live = sum(len(subs) for subs in bus._exact.values()) + len(bus._globs)
    assert live == expected, (
        f"{live} subscriptions on the bus for a layout that needs {expected}"
    )


def test_a_hidden_widget_is_not_drawn_on_the_first_frames_after_a_switch(qt_app) -> None:
    """Every widget started fully visible, so one whose condition was false
    faded OUT over its first seven frames. A note widget with no note burned a
    fading "--" into the recording every time its layout went up -- after a
    switch, a Duplicate or a Reset."""
    from wer.overlay.compositor import Compositor

    comp = Compositor(DataBus())
    note = TextWidget(
        "note", "{manual.note}",
        placement=Placement(Anchor.BOTTOM_CENTER),
        visibility=VisibilityRule(only_when_present="manual.note"),
    )
    comp.apply_layout([note])

    for index in range(8):
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        comp.composite(frame, in_place=True)
        assert not frame.any(), f"the hidden note was drawn on frame {index}"


def test_a_note_cleared_while_its_layout_was_off_screen_does_not_fade_out_again(
    qt_app,
) -> None:
    """The widget objects live on in the layout between switches, holding the
    opacity they had when it last left the screen."""
    from wer.overlay.compositor import Compositor

    bus = DataBus()
    bus.publish("manual.note", "Followspot 2 late", source_connection_id="manual")
    comp = Compositor(bus)
    note = TextWidget(
        "note", "{manual.note}",
        visibility=VisibilityRule(only_when_present="manual.note"),
    )
    comp.apply_layout([note])
    comp.composite(np.zeros((720, 1280, 3), dtype=np.uint8))

    comp.apply_layout([TextWidget("clock", "{clock.wall}")])
    bus.publish("manual.note", "", source_connection_id="manual")
    comp.apply_layout([note])

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    comp.composite(frame, in_place=True)
    assert not frame.any(), "the cleared note faded out again from full"


# ------------------------- choosing whose command line a widget shows


def test_a_template_reports_which_user_it_follows() -> None:
    from wer.ui.layout_editor import command_line_user

    assert command_line_user("{eos.cmdline.text}") == 0
    assert command_line_user("{eos.cmdline.user.2.text}") == 2
    assert command_line_user("{eos.cmdline.user.11.error}") == 11


def test_a_template_with_no_command_line_reports_none() -> None:
    """That is what hides the picker for every other kind of widget."""
    from wer.ui.layout_editor import command_line_user

    assert command_line_user("Cue {eos.cue.active.number}") is None
    assert command_line_user("{clock.wall}") is None


def test_retargeting_round_trips_in_both_directions() -> None:
    from wer.ui.layout_editor import retarget_command_line

    assert retarget_command_line("{eos.cmdline.text}", 2) == "{eos.cmdline.user.2.text}"
    assert retarget_command_line("{eos.cmdline.user.5.text}", 0) == "{eos.cmdline.text}"
    assert retarget_command_line("{eos.cmdline.user.5.text}", 2) == (
        "{eos.cmdline.user.2.text}"
    )


def test_retargeting_leaves_the_surrounding_text_alone() -> None:
    from wer.ui.layout_editor import retarget_command_line

    assert retarget_command_line("> {eos.cmdline.text}", 3) == "> {eos.cmdline.user.3.text}"


def test_retargeting_ignores_a_template_with_no_command_line() -> None:
    from wer.ui.layout_editor import retarget_command_line

    assert retarget_command_line("Cue {eos.cue.active.number}", 2) == (
        "Cue {eos.cue.active.number}"
    )


def test_the_picker_rewrites_the_widget_and_its_visibility_rule(qt_app) -> None:
    """The visibility key has to follow the template. A widget shown "only when
    there is a command line" would otherwise judge itself on a key it no longer
    displays, and never appear."""
    from wer.core.databus import DataBus
    from wer.overlay.compositor import Compositor
    from wer.overlay.presets import get_preset
    from wer.ui.layout_editor import LayoutEditor

    widget = get_preset("cmdline").build("cmdline")
    assert widget.template == "{eos.cmdline.text}"
    assert widget.visibility.only_when_present == "eos.cmdline.text"

    editor = LayoutEditor(Compositor(DataBus()))
    try:
        editor._current = widget
        editor._show_cmd_user(0)
        editor._cmd_user.setCurrentIndex(editor._cmd_user.findData(2))

        assert widget.template == "{eos.cmdline.user.2.text}"
        assert widget.visibility.only_when_present == "eos.cmdline.user.2.text"
        assert "eos.cmdline.user.2.text" in widget.bus_keys
    finally:
        editor.deleteLater()


def test_the_picker_is_hidden_for_a_widget_without_a_command_line(qt_app) -> None:
    from wer.core.databus import DataBus
    from wer.overlay.compositor import Compositor
    from wer.ui.layout_editor import LayoutEditor

    editor = LayoutEditor(Compositor(DataBus()))
    try:
        editor._show_cmd_user(None)
        assert editor._cmd_user.isVisible() is False
        editor._show_cmd_user(0)
        assert editor._cmd_user.isHidden() is False
    finally:
        editor.deleteLater()


# ------------------------------------------------ editing a widget in the editor


@pytest.fixture()
def editor(qt_app):
    """A real LayoutEditor over a real Compositor, with a bus to publish into."""
    from wer.overlay.compositor import Compositor
    from wer.ui.layout_editor import LayoutEditor

    bus = DataBus()
    compositor = Compositor(bus)
    panel = LayoutEditor(compositor)
    yield panel, compositor, bus
    panel.deleteLater()


def test_retyping_a_widgets_content_rebinds_it_to_the_new_keys(editor) -> None:
    """The one that burns a frozen value into the whole take.

    A widget's bus subscriptions were the keys it read when it was ADDED.
    Retyping the Content field changed what it reads and nothing rebound it:
    the editor's own repaint made it look right, and from then on no change to
    the new key ever marked it dirty. With the old key quiet -- a show name, a
    date, anything typed rather than sent -- the value it happened to hold at
    the moment you stopped typing is what every recorded frame gets.
    """
    panel, compositor, bus = editor
    bus.publish("show.name", "Twelfth Night", source_connection_id="manual")
    widget = compositor.add(TextWidget("t", "{show.name}"))
    panel.refresh()
    panel._select_by_id("t")

    panel._content.setText("Cue {eos.cue.active.number}")
    rendered = widget.image(bus, 1080)

    bus.publish("eos.cue.active.number", "58", source_connection_id="eos")
    assert widget.image(bus, 1080) is not rendered, (
        "the widget was never rebound to the key it now reads"
    )


def test_bring_forward_does_not_add_a_second_copy_of_the_widget(editor) -> None:
    """It re-adds the widget to force a re-sort; that used to append."""
    panel, compositor, _ = editor
    compositor.add(TextWidget("a", "{clock.wall}", z_order=10))
    compositor.add(TextWidget("b", "{show.name}", z_order=20))
    panel.refresh()
    panel._select_by_id("a")

    panel._reorder(+1)
    panel._reorder(+1)
    panel._reorder(-1)

    assert sorted(w.id for w in compositor.widgets) == ["a", "b"]
    assert panel._list.count() == 2


def test_the_layer_spinbox_does_not_add_a_second_copy(editor) -> None:
    panel, compositor, _ = editor
    compositor.add(TextWidget("a", "{clock.wall}", z_order=10))
    panel.refresh()
    panel._select_by_id("a")

    panel._z_order.setValue(45)
    panel._z_order.setValue(60)

    assert [w.id for w in compositor.widgets] == ["a"]
    assert compositor.get("a").z_order == 60


def test_a_widgets_existing_visibility_condition_is_shown_and_replaced(editor) -> None:
    """"Only when:" read and wrote only one of VisibilityRule's two fields.

    The shipped 'Fade countdown' preset is conditioned with the other one, so
    the editor showed an empty box for a widget that is gated, gave no way to
    see or clear that gate, and turned a typed key into an AND of two
    conditions -- a widget that then needs both to be true before it appears.
    """
    panel, compositor, bus = editor
    panel._add("cue_countdown")
    widget = compositor.widgets[0]
    assert widget.visibility.only_when == "eos.cue.active.fading"

    assert panel._only_when.text() == "eos.cue.active.fading", (
        "a conditioned widget showed a blank condition box"
    )

    panel._only_when.setText("manual.act")
    rule = compositor.widgets[0].visibility
    conditions = [c for c in (rule.only_when, rule.only_when_present) if c]
    assert conditions == ["manual.act"], f"ended up with {conditions}"

    panel._only_when.clear()
    rule = compositor.widgets[0].visibility
    assert (rule.only_when, rule.only_when_present) == (None, None)


def test_clearing_the_box_clears_a_second_hidden_condition_too(editor) -> None:
    """A show file written by the old editor carries both condition fields.

    One box cannot show two, so the second one is invisible: clearing the box
    left it gating the widget, which then faded out and stayed out with an
    empty "Only when:" claiming it was unconditional. A control that appears to
    do something and does not, on a widget that never reaches the recording.
    """
    from wer.core.databus import DataBus

    panel, compositor, bus = editor
    panel._add("cue_countdown")
    widget = compositor.widgets[0]
    # Exactly what the pre-fix editor produced: typing ANDed a key on.
    widget.visibility.only_when_present = "manual.act"
    panel._select_by_id(widget.id)

    panel._only_when.clear()

    rule = compositor.widgets[0].visibility
    assert (rule.only_when, rule.only_when_present) == (None, None)
    assert rule.evaluate(DataBus(), 0.0), (
        "the widget is still gated by a condition the editor does not show"
    )


def test_a_watermark_pointed_at_a_missing_file_says_so_at_once(editor, tmp_path) -> None:
    """The one affordance that exists to catch a watermark that will not appear.

    ``problem`` is only recomputed during a render, and the editor read it
    before the render had happened -- so it reassured you about a path that
    does not exist, and then warned you about the old one after you had fixed
    it. Configuring the overlay before starting the camera, which is the normal
    order, meant no render at all and no warning ever.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    from wer.overlay.widgets import ImageWidget

    good = tmp_path / "logo.png"
    picture = QImage(40, 20, QImage.Format.Format_ARGB32)
    picture.fill(Qt.GlobalColor.red)
    assert picture.save(str(good))

    panel, compositor, _ = editor
    compositor.add(ImageWidget("wm", str(good)))
    panel.refresh()
    panel._select_by_id("wm")

    panel._image_path.setText(str(tmp_path / "gone.png"))
    panel._image_path_typed()
    assert "gone.png" in panel._what.text(), (
        f"no warning about a missing image: {panel._what.text()!r}"
    )

    panel._image_path.setText(str(good))
    panel._image_path_typed()
    assert "gone.png" not in panel._what.text(), (
        f"still warning about the old path: {panel._what.text()!r}"
    )


def test_a_watermark_with_no_path_yet_is_not_reported_as_a_problem(editor) -> None:
    """A watermark is added before it is pointed anywhere; that is the design.

    Asking the file the question the moment the widget is selected is what
    makes a missing logo visible, but a widget the operator created two seconds
    ago has no file to ask about, and an amber italic warning under it makes a
    brand-new widget look broken. describe() already says what to do next.
    """
    panel, compositor, _ = editor
    panel._add("watermark")

    assert "--" not in panel._what.text(), (
        f"a fresh watermark was flagged as a problem: {panel._what.text()!r}"
    )
    assert "e0a03a" not in panel._what.styleSheet(), "warned in amber about nothing"


def test_a_broken_watermark_is_logged_once_however_often_it_is_clicked(
    editor, tmp_path, caplog
) -> None:
    """The editor asks the file on every selection, and a failed load is not
    cached on purpose -- a logo restored on the show server should start
    working again without anyone retyping the path. That made every click on a
    broken watermark write another warning into the session log, which is the
    file attached to the recording.
    """
    import logging

    from wer.overlay.widgets import ImageWidget

    panel, compositor, _ = editor
    compositor.add(ImageWidget("wm", str(tmp_path / "gone.png")))
    panel.refresh()

    with caplog.at_level(logging.WARNING, logger="wer.overlay.widgets"):
        for _ in range(4):
            panel._current = compositor.get("wm")
            panel._load_properties()

    warnings = [r for r in caplog.records if "gone.png" in r.getMessage()]
    assert len(warnings) == 1, f"{len(warnings)} log lines for one bad path"
    assert "gone.png" in panel._what.text(), "stopped warning on screen as well"


# ----------------------------------------------- widgets stay within reach
#
# Snapping itself is tested in test_snapping.py.


def test_a_widget_cannot_be_dragged_out_of_reach() -> None:
    """The bug this came from: a widget dragged off the frame had nothing left
    to grab, and the only way back was to delete and rebuild it."""
    from wer.overlay.geometry import Anchor, Placement, clamped_placement

    for offset_x, offset_y in ((5.0, 0.0), (-5.0, 0.0), (0.0, 5.0), (0.0, -5.0)):
        lost = Placement(Anchor.TOP_LEFT, offset_x=offset_x, offset_y=offset_y)
        rect = clamped_placement(lost, 1920, 1080, 300, 80).resolve(1920, 1080, 300, 80)
        visible_x = min(rect.x + 300, 1920) - max(rect.x, 0)
        visible_y = min(rect.y + 80, 1080) - max(rect.y, 0)
        assert visible_x > 0 and visible_y > 0, (offset_x, offset_y)
        assert visible_x >= 300 * 0.3 or visible_y >= 80 * 0.3


def test_a_widget_already_on_screen_is_not_moved() -> None:
    from wer.overlay.geometry import Anchor, Placement, clamped_placement

    fine = Placement(Anchor.TOP_LEFT, offset_x=0.1, offset_y=0.1)
    assert clamped_placement(fine, 1920, 1080, 300, 80) == fine


def test_a_widget_with_an_unreadable_value_is_left_out_not_fatal(qt_app, caplog) -> None:
    """One typo in one hand-edited widget stopped the main window being built.

    widget_from_dict skipped a widget of an unknown kind but raised on a known
    kind carrying a value that is not a number, and nothing between it and the
    window's constructor caught it.
    """
    import logging

    data = [{
        "name": "Tech",
        "widgets": [
            {"type": "text", "id": "fine", "template": "{clock.wall}"},
            {"type": "panel", "id": "typo", "height": "big"},
            {"type": "cue", "id": "also_typo", "max_width": "wide"},
        ],
    }]
    with caplog.at_level(logging.WARNING, logger="wer.overlay.layout"):
        layouts = LayoutSet.from_list(data, "Tech")

    assert [w.id for w in layouts.get("Tech").widgets] == ["fine"]
    assert "typo" in caplog.text and "also_typo" in caplog.text
