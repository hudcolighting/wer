"""Ready-made layouts: the catalogue, how each one draws, and the New Layout dialog."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from wer.overlay.geometry import Rect
from wer.overlay.layout import Layout, default_layouts, widget_to_dict
from wer.overlay.layout_presets import (
    LAYOUT_PRESETS,
    LayoutPreset,
    get_layout_preset,
    layout_preset_named,
    unique_layout_name,
)

#: The picture sizes a layout meets: a 720p preview, the usual 1080p camera,
#: and a 4K Blackmagic feed.
RESOLUTIONS = [(1920, 1080), (1280, 720), (3840, 2160)]
RESOLUTION_IDS = ["1080p", "720p", "4K"]

#: The layouts asked for by name, beyond the three a new show file starts with.
ASKED_FOR = {
    "Programming", "Performance", "Lower third", "Archive",
    "Notes session", "Documentation", "System check",
}

#: A command line longer than anyone types, and the worst case for width.
LONG_COMMAND = ("LIVE: " + "Chan 1 Thru 12 At 75 Enter " * 10)[:200]

#: Every string a layout puts on screen, far longer than a desk or a stage
#: manager will ever produce. Anything that grows sideways into its neighbour
#: shows up here.
LONG_TEXT = {
    key: ("Adriana crosses to the stage left portal and waits for the storm " * 4)[:200]
    for key in (
        "eos.cue.active.label", "eos.cue.pending.label", "eos.cue.previous.label",
        "eos.cue.active.text", "eos.chan.active", "eos.show.name",
        "manual.show", "manual.act", "manual.scene", "manual.note",
    )
} | {"eos.cmdline.text": LONG_COMMAND}


def _with_known_faults(faults: dict[str, str]) -> list:
    """Every preset, with the ones in ``faults`` marked as failing for that reason.

    Strict: when the fault is fixed the test starts passing, pytest reports
    that as a failure, and the mark gets removed instead of quietly outliving
    the bug it described.
    """
    return [
        pytest.param(
            preset, id=preset.key,
            marks=pytest.mark.xfail(strict=True, reason=faults[preset.key]),
        )
        if preset.key in faults else pytest.param(preset, id=preset.key)
        for preset in LAYOUT_PRESETS
    ]


#: Presets known to break the lane rules, by key, with the reason. Tech and
#: Archive were both here until their widgets were given a max_width; the tests
#: stay wired through these so a future known fault is marked, and announces
#: its own fix, rather than being skipped.
SHIPPED_COMMAND_LINE_UNBOUNDED: dict[str, str] = {}
SHIPPED_TEXT_UNBOUNDED: dict[str, str] = {}


# ------------------------------------------------------------------ catalogue


def test_every_layout_has_its_own_key_and_name() -> None:
    keys = [preset.key for preset in LAYOUT_PRESETS]
    names = [preset.name for preset in LAYOUT_PRESETS]
    assert len(set(keys)) == len(keys)
    assert len(set(names)) == len(names), (
        "two presets would make layouts of the same name, and LayoutSet.add "
        "replaces one layout with another of the same name"
    )


def test_the_shipped_three_come_first_and_are_exactly_what_ships(qt_app) -> None:
    """They are what a new show starts with and what Reset puts back. A copy
    kept here would drift from default_layouts() the first time any changed."""
    assert [preset.name for preset in LAYOUT_PRESETS[:3]] == ["Tech", "Minimal", "None"]
    for shipped in default_layouts():
        preset = layout_preset_named(shipped.name)
        assert preset is not None, f"no preset for {shipped.name}"
        assert [widget_to_dict(w) for w in preset.build()] == [
            widget_to_dict(w) for w in shipped.widgets
        ]


def test_the_none_preset_builds_nothing_at_all(qt_app) -> None:
    """The one layout whose point is that it draws nothing. Pinned on its own
    because every other check here passes trivially on an empty list, so a
    preset that quietly grew a widget would not be caught by any of them."""
    preset = get_layout_preset("none")
    assert preset.name == "None"
    assert preset.build() == []
    assert preset.layout("Clean").widgets == []


def test_every_layout_asked_for_is_in_the_catalogue() -> None:
    assert ASKED_FOR <= {preset.name for preset in LAYOUT_PRESETS}


@pytest.mark.parametrize("preset", LAYOUT_PRESETS, ids=lambda p: p.key)
def test_every_build_is_made_of_new_objects(qt_app, preset: LayoutPreset) -> None:
    """Two layouts made from one preset must share nothing the editor changes.

    A shared widget would move in both when dragged in one. A shared
    VisibilityRule is quieter and worse: the editor edits the rule in place,
    so clearing "Only when" on a copy would change the layout being recorded.
    """
    first, second = preset.build(), preset.build()
    assert not {id(w) for w in first} & {id(w) for w in second}
    assert not {id(w.visibility) for w in first} & {id(w.visibility) for w in second}


@pytest.mark.parametrize("preset", LAYOUT_PRESETS, ids=lambda p: p.key)
def test_every_layout_survives_the_show_file(qt_app, preset: LayoutPreset) -> None:
    """Through real JSON, as the show file is written."""
    original = preset.layout()
    restored = Layout.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored.name == preset.name
    assert [(w.id, type(w)) for w in restored.widgets] == [
        (w.id, type(w)) for w in original.widgets
    ]
    assert restored.to_dict() == original.to_dict()


@pytest.mark.parametrize("preset", LAYOUT_PRESETS, ids=lambda p: p.key)
def test_widget_ids_are_unique_within_a_layout(qt_app, preset: LayoutPreset) -> None:
    """The compositor keys everything by id and drops a second widget with the
    same one, so a duplicate would simply never appear."""
    ids = [widget.id for widget in preset.build()]
    assert len(set(ids)) == len(ids), ids


@pytest.mark.parametrize("preset", LAYOUT_PRESETS, ids=lambda p: p.key)
def test_backing_panels_are_drawn_underneath(qt_app, preset: LayoutPreset) -> None:
    from wer.overlay.widgets import PanelWidget

    widgets = preset.build()
    panels = [w.z_order for w in widgets if isinstance(w, PanelWidget)]
    others = [w.z_order for w in widgets if not isinstance(w, PanelWidget)]
    if panels:
        assert max(panels) < min(others), "a strip drawn over the text on it hides it"


def test_a_preset_is_found_by_its_key_or_by_the_name_it_gives_a_layout() -> None:
    assert get_layout_preset("programming").name == "Programming"
    assert layout_preset_named("Programming").key == "programming"
    assert get_layout_preset("hologram") is None
    assert layout_preset_named("hologram") is None


def test_a_numbered_copy_has_no_preset_to_reset_to() -> None:
    """Resetting somebody's "Programming 2" to Programming would throw away
    whatever made it different."""
    assert layout_preset_named("Programming 2") is None


def test_a_layout_takes_the_preset_name_unless_given_one(qt_app) -> None:
    preset = get_layout_preset("minimal")
    assert preset.layout().name == "Minimal"
    assert preset.layout("Clean feed").name == "Clean feed"


def test_a_new_layout_name_steps_round_the_ones_already_in_the_show() -> None:
    assert unique_layout_name("Programming", []) == "Programming"
    assert unique_layout_name("Programming", ["Programming"]) == "Programming 2"
    assert unique_layout_name(
        "Programming", ["Tech", "Programming", "Programming 2"]
    ) == "Programming 3"


@pytest.mark.parametrize("preset", LAYOUT_PRESETS, ids=lambda p: p.key)
def test_every_layout_is_described_for_the_person_choosing_it(preset: LayoutPreset) -> None:
    assert preset.summary and "\n" not in preset.summary, "the summary is one line of a list"
    assert len(preset.description) > len(preset.summary)
    assert preset.description.rstrip().endswith(".")


# ------------------------------------------------------------------ rendering


def _draw(
    preset: LayoutPreset, width: int, height: int, extra: dict[str, object] | None = None
) -> tuple[np.ndarray, dict[str, tuple[object, Rect]]]:
    """Composite a preset over the sample stage, as a thumbnail or recording would.

    Returns the frame, and for every widget that drew, the widget and where it
    landed -- worked out after compositing from the size the compositor
    actually drew it at, not predicted.
    """
    from wer.overlay.compositor import Compositor
    from wer.overlay.sample import sample_bus, sample_frame

    bus = sample_bus()
    for key, value in (extra or {}).items():
        bus.publish(key, value, source_connection_id="test")
    compositor = Compositor(bus)
    compositor.apply_layout(preset.build())
    frame = sample_frame(width, height)
    compositor.composite(frame, in_place=True)

    drawn = {}
    for widget in compositor.widgets:
        size = compositor.widget_size(widget.id)
        if size is not None:
            drawn[widget.id] = (widget, widget.placement.resolve(width, height, *size))
    return frame, drawn


def _contains(outer: Rect, inner: Rect) -> bool:
    return (
        inner.x >= outer.x and inner.y >= outer.y
        and inner.right <= outer.right and inner.bottom <= outer.bottom
    )


def _collisions(drawn: dict[str, tuple[object, Rect]], width: int, height: int) -> list[str]:
    """Everything drawn off the picture or over something else.

    Sitting on a panel is the one exception, and only wholly on it: text
    hanging off the end of its strip is half on a dark band and half on the
    stage, which is the ragged look the strip is there to prevent.
    """
    from wer.overlay.widgets import PanelWidget

    faults = []
    for widget_id, (_widget, rect) in drawn.items():
        if not _contains(Rect(0, 0, width, height), rect):
            faults.append(f"{widget_id} at {rect} runs off the {width}x{height} picture")

    items = list(drawn.items())
    for index, (first_id, (first, first_rect)) in enumerate(items):
        for second_id, (second, second_rect) in items[index + 1:]:
            if not first_rect.intersects(second_rect):
                continue
            if isinstance(first, PanelWidget) and _contains(first_rect, second_rect):
                continue
            if isinstance(second, PanelWidget) and _contains(second_rect, first_rect):
                continue
            faults.append(f"{first_id} at {first_rect} is drawn over {second_id} at {second_rect}")
    return faults


@pytest.mark.parametrize("preset", LAYOUT_PRESETS, ids=lambda p: p.key)
def test_every_widget_draws_on_the_sample_stage(qt_app, preset: LayoutPreset) -> None:
    """The sample bus has a value for everything, so a widget that draws nothing
    here is a hole in the thumbnail exactly where it will be on the recording."""
    from wer.overlay.sample import sample_frame

    frame, drawn = _draw(preset, 1920, 1080)
    if not preset.build():
        # None is the exception the rule is worth stating for: an untouched
        # frame is the whole of what it promises.
        assert np.array_equal(frame, sample_frame(1920, 1080))
        assert drawn == {}
        return
    assert not np.array_equal(frame, sample_frame(1920, 1080)), "drew nothing at all"
    assert set(drawn) == {widget.id for widget in preset.build()}


@pytest.mark.parametrize("size", RESOLUTIONS, ids=RESOLUTION_IDS)
@pytest.mark.parametrize("preset", LAYOUT_PRESETS, ids=lambda p: p.key)
def test_nothing_runs_off_the_picture_or_over_anything_else(
    qt_app, preset: LayoutPreset, size: tuple[int, int]
) -> None:
    """With everything on the sample bus present, every conditional widget is
    showing at once -- the most crowded the layout ever gets."""
    width, height = size
    _, drawn = _draw(preset, width, height)
    assert _collisions(drawn, width, height) == []


@pytest.mark.parametrize("size", RESOLUTIONS, ids=RESOLUTION_IDS)
@pytest.mark.parametrize("preset", _with_known_faults(SHIPPED_COMMAND_LINE_UNBOUNDED))
def test_a_very_long_command_line_stays_in_its_lane(
    qt_app, preset: LayoutPreset, size: tuple[int, int]
) -> None:
    """A command line grows towards whatever is beside it with every keystroke,
    and a long enough one used to run straight through the show title -- one
    drawn over the other, in every frame, for as long as the command was up."""
    width, height = size
    _, drawn = _draw(preset, width, height, {"eos.cmdline.text": LONG_COMMAND})
    assert _collisions(drawn, width, height) == []


@pytest.mark.parametrize("size", RESOLUTIONS, ids=RESOLUTION_IDS)
@pytest.mark.parametrize("preset", _with_known_faults(SHIPPED_TEXT_UNBOUNDED))
def test_long_labels_titles_and_notes_stay_in_their_lanes(
    qt_app, preset: LayoutPreset, size: tuple[int, int]
) -> None:
    """Cue labels, show titles and notes are typed by people, and nothing stops
    one being long. Each is cut short with an ellipsis at the edge of its own
    space rather than written over its neighbour."""
    width, height = size
    _, drawn = _draw(preset, width, height, LONG_TEXT)
    assert _collisions(drawn, width, height) == []


@pytest.mark.parametrize("preset", LAYOUT_PRESETS, ids=lambda p: p.key)
def test_text_keeps_its_legibility_aids(qt_app, preset: LayoutPreset) -> None:
    """Outline and shadow everywhere. The box may go only where a panel is the
    backing, and then only for text sitting wholly on it."""
    from wer.overlay.widgets import FadeBarWidget, PanelWidget

    _, drawn = _draw(preset, 1920, 1080)
    panels = [rect for widget, rect in drawn.values() if isinstance(widget, PanelWidget)]
    for widget_id, (widget, rect) in drawn.items():
        if isinstance(widget, (PanelWidget, FadeBarWidget)):
            continue
        assert widget.style.outline_width > 0, f"{widget_id} has no outline"
        assert widget.style.shadow, f"{widget_id} has no shadow"
        if not widget.style.box.enabled:
            assert any(_contains(panel, rect) for panel in panels), (
                f"{widget_id} has no box and nothing behind it"
            )


# --------------------------------------------------------------------- dialog


@pytest.fixture()
def new_layout_dialog(qt_app):
    """Build LayoutPresetDialogs without exec(), and tidy them away afterwards."""
    from wer.ui.layout_preset_dialog import LayoutPresetDialog

    made = []

    def build(existing=("Tech", "Minimal", "None")):
        dialog = LayoutPresetDialog(existing)
        made.append(dialog)
        return dialog

    yield build
    for dialog in made:
        dialog.deleteLater()


def _ok_button(dialog):
    from PySide6.QtWidgets import QDialogButtonBox

    return dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)


def test_the_dialog_offers_a_blank_layout_then_every_preset(new_layout_dialog) -> None:
    dialog = new_layout_dialog()
    listed = [dialog._list.item(row).text() for row in range(dialog._list.count())]

    assert dialog.windowTitle() == "New layout"
    assert listed == ["Blank layout"] + [preset.name for preset in LAYOUT_PRESETS]
    assert dialog.chosen_key() is None, "should open on Blank, as New... always did"


def test_choosing_a_preset_shows_its_picture_and_offers_its_name(new_layout_dialog) -> None:
    dialog = new_layout_dialog()
    dialog.select("programming")

    assert dialog.chosen_key() == "programming"
    assert dialog._preview.pixmap() is not None
    assert not dialog._preview.pixmap().isNull()
    assert dialog.layout_name() == "Programming"
    assert dialog._description.text() == get_layout_preset("programming").description
    assert dialog._thumbnail("programming").toImage() != dialog._thumbnail(None).toImage(), (
        "the picture is the bare stage with nothing drawn on it"
    )


def test_each_picture_is_drawn_once(new_layout_dialog) -> None:
    dialog = new_layout_dialog()
    dialog.select("performance")
    first = dialog._thumbnail("performance")
    dialog.select("minimal")
    dialog.select("performance")
    assert dialog._thumbnail("performance") is first


def test_the_offered_name_steps_round_names_already_in_the_show(new_layout_dialog) -> None:
    dialog = new_layout_dialog(
        ["Tech", "Minimal", "None", "Programming", "Programming 2"]
    )
    dialog.select("programming")
    assert dialog.layout_name() == "Programming 3"
    assert _ok_button(dialog).isEnabled()


def test_ok_is_refused_for_an_empty_or_a_used_name(new_layout_dialog) -> None:
    """LayoutSet.add replaces a layout of the same name without a word, so a
    duplicate here would be somebody's layout gone."""
    from PySide6.QtWidgets import QDialog

    dialog = new_layout_dialog()
    dialog.select("programming")
    assert _ok_button(dialog).isEnabled()

    for unusable in ("", "   ", "Tech", " Minimal "):
        dialog._name.setText(unusable)
        assert not _ok_button(dialog).isEnabled(), f"OK allowed for {unusable!r}"
        assert dialog._problem.text(), f"no reason given for refusing {unusable!r}"

    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted.value, (
        "accept() let a used name through when OK was not the way it arrived"
    )

    dialog._name.setText("Tech notes")
    assert _ok_button(dialog).isEnabled()
    assert dialog._problem.text() == ""


def test_the_layout_built_has_the_typed_name_and_the_presets_widgets(new_layout_dialog) -> None:
    dialog = new_layout_dialog()
    dialog.select("notes_session")
    dialog._name.setText("  Notes, Tuesday  ")

    layout = dialog.build_layout()
    assert isinstance(layout, Layout)
    assert layout.name == "Notes, Tuesday"
    assert [w.id for w in layout.widgets] == [
        w.id for w in get_layout_preset("notes_session").build()
    ]


def test_a_blank_layout_is_empty(new_layout_dialog) -> None:
    dialog = new_layout_dialog()
    dialog.select("lower_third")
    dialog.select(None)

    layout = dialog.build_layout()
    assert layout.widgets == []
    assert layout.name == "My layout"
    assert not dialog._preview.pixmap().isNull(), "Blank shows the bare stage"


def test_the_none_preset_has_a_picture_and_a_name_like_any_other(
    new_layout_dialog,
) -> None:
    """A preset that draws nothing still has to go through the dialog: the
    picture is the bare stage, which is exactly what it promises, and choosing
    it must not leave the previous layout's picture or name in place."""
    dialog = new_layout_dialog()
    dialog.select("lower_third")
    dialog.select("none")

    assert dialog.chosen_key() == "none"
    assert not dialog._preview.pixmap().isNull()
    assert dialog._thumbnail("none").toImage() == dialog._thumbnail(None).toImage()
    assert dialog.layout_name() == "None 2", "None is already in this show"
    assert dialog.build_layout().widgets == []


def test_an_offered_name_follows_the_selection(new_layout_dialog) -> None:
    dialog = new_layout_dialog()
    dialog.select("programming")
    dialog.select("performance")
    assert dialog.layout_name() == "Performance"


def test_a_name_you_typed_survives_changing_the_selection(new_layout_dialog) -> None:
    """Browsing on to look at another picture must not wipe what was typed."""
    dialog = new_layout_dialog()
    dialog.select("programming")
    dialog._name.setText("Dress rehearsal")

    dialog.select("performance")
    dialog.select(None)
    assert dialog.layout_name() == "Dress rehearsal"


def test_double_clicking_a_layout_does_not_confirm_it(new_layout_dialog) -> None:
    """Picking from a list is a two-click gesture for most people, and the
    second click landing on the same row used to create the layout before they
    had looked at the name field. OK and Enter are the ways to confirm; the
    selection still moves, so a double click is harmless rather than ignored."""
    dialog = new_layout_dialog()
    dialog.select("documentation")
    dialog._list.itemDoubleClicked.emit(dialog._list.currentItem())

    assert not dialog.isVisible() or dialog.result() == 0, "the dialog confirmed itself"
    assert dialog.chosen_key() == "documentation", "the selection was lost"


def test_selecting_a_preset_that_does_not_exist_is_an_error(new_layout_dialog) -> None:
    """Not silently ignored: the caller would build whatever was selected before."""
    dialog = new_layout_dialog()
    with pytest.raises(KeyError):
        dialog.select("hologram")


def test_the_dialog_cannot_reach_capture_hardware() -> None:
    """Choosing a layout must never go near a camera, a console or an audio
    device: it can be opened mid-recording, and the capture card is in use.

    Checked by what importing the dialog pulls in, in a fresh interpreter for
    the reason test_architecture gives -- once another test has imported the
    video code, an in-process check passes whatever happens.
    """
    script = (
        "import sys, wer.ui.layout_preset_dialog\n"
        "print(';'.join(sorted(m for m in sys.modules if m.startswith("
        "('wer.video', 'wer.connections', 'wer.ui.preview', "
        "'wer.ui.record_panel', 'wer.ui.main_window')))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout.strip(), f"reaches hardware code: {result.stdout.strip()}"
