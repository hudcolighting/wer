"""The Overlay tab's property sheet.

Most of what a widget can do had no control at all: a cue block's Next line
and fade bar, a lamp's key, off text and colours, a picture's opacity, the
hide-after and fade timings, the edge margin and the lock were all in the model
and the show file and nowhere on screen -- the cue block's Content box just
said "(built in)". These tests hold each control to changing the thing it says
it changes, and each kind of widget to showing only the rows it has.
"""

from __future__ import annotations

import pytest

from wer.overlay.compositor import Compositor
from wer.overlay.sample import sample_bus


@pytest.fixture()
def sheet(qt_app):
    from wer.ui.layout_editor import LayoutEditor

    bus = sample_bus()
    compositor = Compositor(bus)
    panel = LayoutEditor(compositor)
    yield panel, compositor, bus
    panel.deleteLater()


def _added(panel, preset: str):
    """Add a widget from the catalogue the way the menu does; it is selected."""
    panel._add(preset)
    assert panel._current is not None
    return panel._current


def _showing(panel, row: str) -> bool:
    caption, field = panel._rows[row]
    return not field.isHidden() and not caption.isHidden()


# ------------------------------------------------------------------ the rows


@pytest.mark.parametrize(
    ("preset", "shown", "hidden"),
    [
        ("clock_date", {"content", "emphasise", "font", "colour", "max_width"},
         {"cue_options", "lamp_key", "panel_width", "bar_width", "image_file"}),
        ("cue_stack", {"cue_options", "font", "colour", "box"},
         {"content", "lamp_key", "panel_width", "image_file"}),
        ("console_lamp", {"content", "lamp_key", "off_text", "lamp_colours", "font"},
         {"cue_options", "colour", "emphasise", "panel_width"}),
        ("backing_strip", {"panel_width", "panel_height", "box_colour", "box_radius"},
         {"content", "font", "size", "box", "max_width", "colour"}),
        ("fade_bar", {"bar_width", "bar_height", "bar_idle", "colour"},
         {"content", "font", "box_colour", "max_width"}),
        ("cue_timer", {"timer_from", "timer_prefix", "font", "colour", "max_width"},
         {"content", "cue_options", "bar_width", "image_file"}),
        ("watermark", {"image_file", "image_height", "image_opacity"},
         {"content", "font", "colour", "cue_options"}),
    ],
)
def test_each_kind_of_widget_shows_its_own_rows_and_no_others(
    sheet, preset, shown, hidden
) -> None:
    panel, _compositor, _bus = sheet
    _added(panel, preset)
    for row in shown:
        assert _showing(panel, row), f"{preset} should show {row}"
    for row in hidden:
        assert not _showing(panel, row), f"{preset} should not show {row}"


# ------------------------------------------------------------------ content


def test_a_text_widget_can_run_to_several_lines(sheet) -> None:
    panel, _compositor, bus = sheet
    widget = _added(panel, "custom_text")

    panel._content.setText("{clock.wall}\n{clock.date_long}")
    assert widget.template == "{clock.wall}\n{clock.date_long}"
    two_lines = widget.image(bus, 1080, 1920).height()

    panel._emphasise.setChecked(True)
    assert widget.emphasise_first_line is True
    assert widget.image(bus, 1080, 1920).height() < two_lines, (
        "the second line did not get smaller"
    )


def test_insert_key_puts_the_key_into_the_template(sheet) -> None:
    panel, _compositor, _bus = sheet
    widget = _added(panel, "custom_text")
    panel._content.setText("Take ")
    panel._content.moveCursor(panel._content.textCursor().MoveOperation.End)
    panel._insert_into_content("clock.take")
    assert widget.template == "Take {clock.take}"
    assert "clock.take" in widget.bus_keys


def test_the_key_menu_lists_the_known_keys_and_what_is_on_the_bus(sheet) -> None:
    from PySide6.QtWidgets import QMenu

    panel, _compositor, bus = sheet
    chosen: list[str] = []
    menu = QMenu()
    panel._fill_key_menu(menu, chosen.append)

    groups = {action.text(): action.menu() for action in menu.actions()}
    assert "From the console (Eos)" in groups
    live = next(title for title in groups if title.startswith("On the bus now"))
    assert f"({len(bus)})" in live

    keys = [a.text() for sub in groups.values() for a in sub.actions()]
    assert "eos.cue.active.number" in keys
    assert not [key for key in keys if "<" in key], "a pattern offered as a key"

    next(
        a for a in groups["From the clock"].actions() if a.text() == "clock.wall"
    ).trigger()
    assert chosen == ["clock.wall"]


def test_the_cue_block_lines_can_be_switched_on_and_off(sheet) -> None:
    panel, _compositor, _bus = sheet
    widget = _added(panel, "cue_stack")
    assert widget.show_previous and panel._cue_previous.isChecked()

    panel._cue_list.setChecked(False)
    panel._cue_previous.setChecked(False)
    panel._cue_bar.setChecked(False)
    panel._cue_next.setChecked(False)

    assert (widget.show_list, widget.show_previous, widget.show_progress,
            widget.show_pending) == (False, False, False, False)
    assert "eos.cue.previous.number" not in widget.bus_keys
    assert "eos.cue.pending.number" not in widget.bus_keys


def test_a_lamp_can_follow_any_key_with_its_own_words_and_dot(sheet) -> None:
    panel, _compositor, bus = sheet
    widget = _added(panel, "console_lamp")

    panel._lamp_key.setText("clock.recording")
    panel._lamp_changed()  # what editingFinished does
    panel._content.setText("REC {clock.elapsed}")
    panel._off_text.setText("standing by")
    panel._show_dot.setChecked(False)
    panel._hide_when_off.setChecked(True)

    assert widget.key == "clock.recording"
    assert widget.on_text == "REC {clock.elapsed}"
    assert widget.off_text == "standing by"
    assert (widget.show_dot, widget.hide_when_off) == (False, True)
    assert "clock.elapsed" in widget.bus_keys


def test_shapes_and_fade_bars_are_sized_in_percent(sheet) -> None:
    panel, _compositor, _bus = sheet
    strip = _added(panel, "backing_strip")
    panel._panel_height.setValue(20)
    panel._panel_width.setValue(50)
    assert (strip.width, strip.height) == pytest.approx((0.50, 0.20))

    bar = _added(panel, "fade_bar")
    panel._bar_width.setValue(80)
    panel._bar_idle.setChecked(False)
    assert bar.width == pytest.approx(0.80)
    assert bar.hide_when_idle is False


def test_a_pictures_opacity_is_on_the_sheet(sheet) -> None:
    panel, _compositor, _bus = sheet
    picture = _added(panel, "watermark")
    panel._image_opacity.setValue(40)
    assert picture.image_opacity == pytest.approx(0.40)


# ----------------------------------------------------------------- position


def test_margin_widest_and_lock(sheet) -> None:
    panel, _compositor, _bus = sheet
    widget = _added(panel, "cmdline")

    panel._margin.setValue(0.0)
    panel._max_width.setValue(0.5)
    panel._locked.setChecked(True)

    assert widget.placement.margin == 0.0
    assert widget.max_width == pytest.approx(0.5)
    assert widget.locked is True
    assert "locked" in panel._list.currentItem().text()


def test_a_tiny_widest_still_cuts_the_text(qt_app) -> None:
    """A limit narrower than the box's padding used to skip the cut and draw
    the whole line."""
    from wer.core.databus import DataBus
    from wer.overlay.widgets import TextWidget

    long_text = TextWidget("t", "LIVE: Chan 1 Thru 200 At Full Enter " * 4, max_width=0.002)
    image = long_text.image(DataBus(), 1080, 1920)
    assert image is not None
    assert image.width() < 200, f"drawn {image.width()} px wide under a 4 px limit"


# -------------------------------------------------------------------- style


def test_the_style_controls_change_the_style(sheet) -> None:
    panel, _compositor, _bus = sheet
    widget = _added(panel, "clock_24")

    panel._weight.setCurrentIndex(panel._weight.findData(400))
    panel._italic.setChecked(True)
    panel._opacity.setValue(50)
    panel._box_opacity.setValue(40)
    panel._box_padding.setValue(0.02)
    panel._box_radius.setValue(0.015)

    style = widget.style
    assert (style.weight, style.italic) == (400, True)
    assert style.opacity == pytest.approx(0.5)
    assert style.box.fill.a == round(40 * 2.55)
    assert style.box.padding == pytest.approx(0.02)
    assert style.box.corner_radius == pytest.approx(0.015)


def test_changing_the_size_keeps_a_custom_outline_width(sheet) -> None:
    """Every style change used to write the outline back as 0.10."""
    from dataclasses import replace

    panel, _compositor, _bus = sheet
    widget = _added(panel, "clock_24")
    widget.style = replace(widget.style, outline_width=0.22)
    panel._load_properties()

    panel._size.setValue(0.05)
    assert widget.style.outline_width == pytest.approx(0.22)


def test_a_font_this_computer_lacks_is_kept_until_the_font_is_changed(sheet) -> None:
    """The font box shows the nearest installed family; that must not be
    written over a show file's font just because the size was touched."""
    from dataclasses import replace

    from PySide6.QtGui import QFont

    panel, _compositor, _bus = sheet
    widget = _added(panel, "clock_24")
    widget.style = replace(widget.style, family="A Font Nobody Has Installed")
    panel._load_properties()

    panel._size.setValue(0.05)
    assert widget.style.family == "A Font Nobody Has Installed"

    panel._font.setCurrentFont(QFont("Arial"))
    assert widget.style.family == "Arial"


def test_paste_style_gives_the_look_but_keeps_the_size(sheet) -> None:
    from dataclasses import replace

    from wer.overlay.style import Colour

    panel, _compositor, _bus = sheet
    source = _added(panel, "cue_number")
    source.style = replace(
        source.style, colour=Colour(243, 156, 18), size=0.08,
        box=replace(source.style.box, enabled=False),
    )
    panel._load_properties()
    panel._copy_style()

    target = _added(panel, "date")
    size = target.style.size
    panel._paste_style()

    assert target.style.colour == Colour(243, 156, 18)
    assert target.style.box.enabled is False
    assert target.style.size == size


# --------------------------------------------------------------- visibility


def test_appear_says_which_condition_a_key_is_and_always_clears_it(sheet) -> None:
    panel, _compositor, _bus = sheet
    widget = _added(panel, "clock_24")
    rule = widget.visibility

    panel._only_when.setText("clock.recording")
    assert panel._only_when_mode.currentData() == "only_when_present"
    assert (rule.only_when, rule.only_when_present) == (None, "clock.recording")

    panel._only_when_mode.setCurrentIndex(panel._only_when_mode.findData("only_when"))
    assert (rule.only_when, rule.only_when_present) == ("clock.recording", None)

    panel._only_when_mode.setCurrentIndex(0)
    assert (rule.only_when, rule.only_when_present) == (None, None)
    assert panel._only_when.text() == ""


def test_hide_after_and_fade_are_on_the_sheet(sheet) -> None:
    panel, _compositor, _bus = sheet
    widget = _added(panel, "manual_note")
    panel._hide_after.setValue(8)
    panel._fade.setValue(0)
    assert widget.visibility.hide_after == pytest.approx(8)
    assert widget.visibility.fade == 0.0


def test_a_watermark_warning_does_not_follow_you_to_the_next_widget(
    sheet, tmp_path
) -> None:
    panel, compositor, _bus = sheet
    picture = _added(panel, "watermark")
    panel._image_path.setText(str(tmp_path / "gone.png"))
    panel._image_path_typed()
    assert "e0a03a" in panel._what.styleSheet()

    _added(panel, "clock_24")
    assert "e0a03a" not in panel._what.styleSheet(), (
        "the clock's caption is still coloured as a warning"
    )
    assert compositor.get(picture.id) is not None
