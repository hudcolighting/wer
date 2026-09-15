"""Ready-made layouts on the Overlay tab: New starts from one, Reset puts one back.

The catalogue and the dialog have their own tests in test_layout_presets.py.
These are about the editor's two buttons that use them, and the help page
built from the same list.
"""

from __future__ import annotations

import pytest

from wer.overlay.layout import Layout, LayoutSet, default_layouts
from wer.overlay.layout_presets import LAYOUT_PRESETS, get_layout_preset


@pytest.fixture()
def editor(qt_app):
    from wer.core.databus import DataBus
    from wer.overlay.compositor import Compositor
    from wer.ui.layout_editor import LayoutEditor

    panel = LayoutEditor(Compositor(DataBus()))
    yield panel
    panel.deleteLater()


def test_new_adds_the_layout_the_dialog_built_and_switches_to_it(editor, monkeypatch) -> None:
    """New used to ask for a name and hand back an empty layout, so the way to
    anything useful was Duplicate on Tech and taking things away."""
    from wer.ui import layout_preset_dialog

    layouts = LayoutSet(default_layouts(), "Tech")
    editor.set_layouts(layouts)
    offered: list[list[str]] = []

    def choose(parent, existing_names):
        offered.append(list(existing_names))
        return get_layout_preset("programming").layout("Programming")

    monkeypatch.setattr(layout_preset_dialog, "choose_new_layout", choose)
    switched: list[str] = []
    changed: list[bool] = []
    editor.layout_switch_requested.connect(switched.append)
    editor.layout_set_changed.connect(lambda: changed.append(True))

    editor._new_layout()

    assert offered == [["Tech", "Minimal", "None"]], (
        "the dialog was not told which names are taken"
    )
    built = layouts.get("Programming")
    assert built is not None
    assert [w.id for w in built.widgets] == [
        w.id for w in get_layout_preset("programming").build()
    ]
    assert switched == ["Programming"]
    assert changed == [True]


def test_cancelling_new_changes_nothing(editor, monkeypatch) -> None:
    from wer.ui import layout_preset_dialog

    layouts = LayoutSet(default_layouts(), "Tech")
    editor.set_layouts(layouts)
    monkeypatch.setattr(layout_preset_dialog, "choose_new_layout", lambda parent, names: None)
    switched: list[str] = []
    editor.layout_switch_requested.connect(switched.append)

    editor._new_layout()

    assert layouts.names == ["Tech", "Minimal", "None"]
    assert switched == []


@pytest.mark.parametrize("preset", LAYOUT_PRESETS, ids=lambda p: p.key)
def test_reset_puts_any_ready_made_layout_back_the_way_it_ships(
    editor, monkeypatch, preset
) -> None:
    """Reset was offered for the layouts a show ships with only, and looked
    them up in default_layouts(), which knows nothing of the others."""
    from PySide6.QtWidgets import QMessageBox

    edited = preset.layout()
    edited.widgets = edited.widgets[:1]
    layouts = LayoutSet([edited, Layout("Mine")], preset.name)
    editor.set_layouts(layouts)
    assert editor._reset_button.isEnabled(), f"Reset not offered for {preset.name}"

    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: int(QMessageBox.StandardButton.Yes),
    )
    editor._reset_layout()

    assert [w.id for w in layouts.get(preset.name).widgets] == [
        w.id for w in preset.build()
    ]


def test_reset_empties_the_layout_that_is_meant_to_be_empty(editor, monkeypatch) -> None:
    """Every other layout is reset by being given its widgets back, and the
    check above cannot tell that apart from doing nothing when the layout ships
    with none. Putting a widget on None first is the only way to see that Reset
    takes one away -- which is the whole of what it has to do here."""
    from PySide6.QtWidgets import QMessageBox

    from wer.overlay.widgets import TextWidget

    lived_in = get_layout_preset("none").layout()
    lived_in.widgets = [TextWidget("clock", "{clock.wall}")]
    layouts = LayoutSet([lived_in, Layout("Mine")], "None")
    editor.set_layouts(layouts)
    assert editor._reset_button.isEnabled()

    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: int(QMessageBox.StandardButton.Yes),
    )
    editor._reset_layout()

    assert layouts.get("None").widgets == [], "None came back with something on it"


def test_reset_is_not_offered_for_a_copy_or_for_your_own_layout(editor) -> None:
    """"Programming 2" is somebody's own copy; resetting it to Programming
    would throw away whatever made it different."""
    copy = get_layout_preset("programming").layout("Programming 2")
    layouts = LayoutSet([copy, Layout("Mine")], "Programming 2")
    editor.set_layouts(layouts)
    assert not editor._reset_button.isEnabled()

    layouts.activate("Mine")
    editor.refresh_layouts()
    assert not editor._reset_button.isEnabled()


def test_the_layouts_help_lists_every_ready_made_layout() -> None:
    """Generated from the catalogue, so a new layout documents itself."""
    from wer.ui.help_content import topics

    body = next(topic.body for topic in topics() if topic.key == "layouts")
    for preset in LAYOUT_PRESETS:
        assert preset.name in body, f"{preset.name} is missing from the help"


def test_the_layout_with_nothing_on_it_gets_a_row_of_its_own() -> None:
    """The catalogue is built by walking each preset, and a layout that draws
    nothing has as much right to a row as one that draws everything -- more,
    because "None" is the one whose name alone does not say what it does."""
    from wer.ui.help_content import topics

    body = next(topic.body for topic in topics() if topic.key == "layouts")
    none = get_layout_preset("none")
    assert f"<b>{none.name}</b>" in body
    assert none.description in body
