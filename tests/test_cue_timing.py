"""Cue timing on the overlay: a fade countdown that moves with the video, and
how long the live cue has been up.

The desk reports a fade's time left about once a second -- 5.0, 4.0, 3.0, a
second apart, on a captured five-second fade -- so text showing it stood on
each figure for a second while the video ran at thirty. And nothing showed how
long a cue had been up, counted from when it was fired or from when its fade
landed.

Time is a stand-in clock here, so nothing waits and nothing is flaky: the bus
stamps values with time.perf_counter and the widgets read it, and both see the
clock the test moves.
"""

from __future__ import annotations

import time

import pytest

from wer.core.databus import DataBus


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch) -> Clock:
    stand_in = Clock()
    monkeypatch.setattr(time, "perf_counter", stand_in)
    return stand_in


def _publish(bus: DataBus, key: str, value, stale_after: float | None = None) -> None:
    bus.publish(key, value, source_connection_id="eos", stale_after=stale_after)


def _fade_report(bus: DataBus, remaining: float, *, fading: bool = True) -> None:
    """What the parser publishes from one of the desk's fade reports."""
    _publish(bus, "eos.cue.active.remaining", remaining, stale_after=3.0)
    _publish(bus, "eos.cue.active.fading", fading, stale_after=3.0 if fading else None)


def _go(bus: DataBus, number: str, *, fading: bool = True, cue_list: str = "1") -> None:
    """A cue fired: the desk sends the new live cue and its fade state together."""
    _publish(bus, "eos.cue.active.list", cue_list)
    _publish(bus, "eos.cue.active.number", number)
    _publish(bus, "eos.cue.active.fading", fading)


# ------------------------------------------------------------- the countdown


def test_a_fade_countdown_counts_down_between_the_desks_reports(clock, qt_app) -> None:
    """It used to sit on each figure the desk sent for a whole second."""
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    widget = TextWidget("countdown", "{eos.cue.active.remaining}s left")
    _fade_report(bus, 4.0)
    widget.image(bus, 1080, 1920)
    assert widget.text(bus) == "4.0s left"

    clock.advance(0.3)
    assert widget.text(bus) == "3.7s left", "the countdown waited for the desk"
    assert widget.wants_repaint(bus) is True
    widget.image(bus, 1080, 1920)
    assert widget.wants_repaint(bus) is False, "redrawn with nothing new to show"

    clock.advance(0.1)
    assert widget.wants_repaint(bus) is True


def test_a_fade_countdown_starts_again_from_each_report_and_never_goes_below_zero(
    clock,
) -> None:
    """Never further from the desk than one report, and never negative when a
    report is late."""
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    widget = TextWidget("countdown", "Fade {eos.cue.active.remaining}s")
    _fade_report(bus, 1.0)
    clock.advance(1.4)
    assert widget.text(bus) == "Fade 0.0s"

    _fade_report(bus, 2.0)
    assert widget.text(bus) == "Fade 2.0s", "a new report did not start it again"


def test_a_countdown_whose_figure_has_gone_stale_still_says_so(clock) -> None:
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    widget = TextWidget("countdown", "{eos.cue.active.remaining}s left")
    _fade_report(bus, 4.0)
    clock.advance(3.5)
    assert widget.text(bus) == "--s left", "a stale figure was counted down as live"


def test_a_countdown_between_fades_shows_the_desks_figure_and_stays_cached(clock, qt_app) -> None:
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    widget = TextWidget("countdown", "{eos.cue.active.remaining}s left")
    _fade_report(bus, 0.0, fading=False)
    widget.image(bus, 1080, 1920)
    clock.advance(2.0)
    assert widget.text(bus) == "0.0s left"
    assert widget.wants_repaint(bus) is False


def test_text_with_no_countdown_in_it_is_never_redrawn_on_its_own(clock, qt_app) -> None:
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    _publish(bus, "eos.cue.active.number", "58")
    widget = TextWidget("cue", "Cue {eos.cue.active.number}")
    widget.image(bus, 1080, 1920)
    clock.advance(5.0)
    assert widget.wants_repaint(bus) is False
    assert "eos.cue.active.fading" not in widget.content_keys


def test_a_countdown_hears_when_its_fade_stops(clock) -> None:
    """Whether the fade is running decides what a countdown shows, so a change
    to it has to reach the widget."""
    from wer.overlay.widgets import TextWidget

    widget = TextWidget("countdown", "{eos.cue.active.remaining}s left")
    assert "eos.cue.active.fading" in widget.content_keys


def test_the_calling_layouts_countdown_now_moves_with_the_video(clock) -> None:
    from wer.overlay.layout_presets import LAYOUT_PRESETS
    from wer.overlay.widgets import TextWidget

    countdowns = [
        widget
        for preset in LAYOUT_PRESETS
        for widget in preset.build()
        if isinstance(widget, TextWidget) and "cue.active.remaining" in widget.template
    ]
    assert countdowns, "precondition: a layout preset carries a fade countdown"
    bus = DataBus()
    _fade_report(bus, 3.0)
    clock.advance(0.5)
    for widget in countdowns:
        assert "2.5" in widget.text(bus), widget.template


# ------------------------------------------------------------ time in cue


def _timer(**kwargs):
    from wer.overlay.widgets import CueTimerWidget

    return CueTimerWidget("timer", **kwargs)


def test_time_in_cue_waits_for_a_cue_it_saw_fire(clock) -> None:
    """A cue already up when Wer connected has no known start: counting from
    when Wer noticed would burn a wrong time into the recording."""
    bus = DataBus()
    timer = _timer()
    _go(bus, "58")
    assert timer._timer_text(bus) == "In cue --"
    clock.advance(0.1)
    _go(bus, "59")
    assert timer._timer_text(bus) == "In cue 0:00"
    clock.advance(65.2)
    assert timer._timer_text(bus) == "In cue 1:05"


def test_time_in_cue_starts_again_when_the_next_cue_fires(clock) -> None:
    bus = DataBus()
    timer = _timer()
    _go(bus, "58")
    timer._timer_text(bus)
    _go(bus, "59")
    timer._timer_text(bus)
    clock.advance(0.2)
    timer._timer_text(bus)
    for _ in range(40):
        clock.advance(0.25)
        timer._timer_text(bus)
    _go(bus, "60")
    assert timer._timer_text(bus) == "In cue 0:00"
    clock.advance(3.0)
    assert timer._timer_text(bus) == "In cue 0:03"


def test_counting_from_the_end_of_the_fade_holds_at_zero_until_it_lands(clock) -> None:
    bus = DataBus()
    timer = _timer(count_from="fade_end")
    _go(bus, "58")
    timer._timer_text(bus)
    _go(bus, "59", fading=True)
    for _ in range(12):
        clock.advance(0.25)
        assert timer._timer_text(bus) == "In cue 0:00", "counted before the fade landed"
    _publish(bus, "eos.cue.active.fading", False)
    assert timer._timer_text(bus) == "In cue 0:00"
    for _ in range(40):
        clock.advance(0.25)
        timer._timer_text(bus)
    assert timer._timer_text(bus) == "In cue 0:10"


def test_counting_from_the_start_ignores_how_long_the_fade_took(clock) -> None:
    bus = DataBus()
    timer = _timer(count_from="fade_start")
    _go(bus, "58")
    timer._timer_text(bus)
    _go(bus, "59", fading=True)
    for _ in range(12):
        clock.advance(0.25)
        timer._timer_text(bus)
    _publish(bus, "eos.cue.active.fading", False)
    assert timer._timer_text(bus) == "In cue 0:03"


def test_a_cue_with_no_fade_counts_from_the_moment_it_fired_either_way(clock) -> None:
    for count_from in ("fade_start", "fade_end"):
        bus = DataBus()
        timer = _timer(count_from=count_from)
        _go(bus, "58")
        timer._timer_text(bus)
        _go(bus, "59", fading=False)
        timer._timer_text(bus)
        for _ in range(20):
            clock.advance(0.25)
            timer._timer_text(bus)
        assert timer._timer_text(bus) == "In cue 0:05", count_from


def test_a_desk_that_goes_away_shows_missing_and_the_cue_it_comes_back_on_is_not_trusted(
    clock,
) -> None:
    bus = DataBus()
    timer = _timer()
    _publish(bus, "eos.connected", True)
    _go(bus, "58")
    timer._timer_text(bus)
    _go(bus, "59")
    timer._timer_text(bus)

    _publish(bus, "eos.connected", False)
    assert timer._timer_text(bus) == "In cue --"
    clock.advance(0.1)
    _publish(bus, "eos.connected", True)
    _go(bus, "63")
    assert timer._timer_text(bus) == "In cue --", "a cue fired while away was given a start"
    clock.advance(0.1)
    _go(bus, "64")
    assert timer._timer_text(bus) == "In cue 0:00"


def test_a_change_of_cue_seen_after_a_gap_is_not_trusted(clock) -> None:
    """Off screen or without a picture, the desk may have re-sent the cue since
    it fired, which moves the timestamp a start would be read from."""
    bus = DataBus()
    timer = _timer()
    _go(bus, "58")
    timer._timer_text(bus)
    clock.advance(2.0)
    _go(bus, "59")
    assert timer._timer_text(bus) == "In cue --"


def test_time_in_cue_passes_an_hour_as_hours(clock) -> None:
    bus = DataBus()
    timer = _timer(prefix="")
    _go(bus, "58")
    timer._timer_text(bus)
    _go(bus, "59")
    timer._timer_text(bus)
    clock.advance(3605.0)
    assert timer._timer_text(bus) == "1:00:05"


def test_time_in_cue_is_redrawn_once_a_second_not_every_frame(clock, qt_app) -> None:
    bus = DataBus()
    timer = _timer()
    _go(bus, "58")
    timer.image(bus, 1080, 1920)
    _go(bus, "59")
    timer.notify_changed()
    assert timer.image(bus, 1080, 1920) is not None
    clock.advance(0.2)
    assert timer.wants_repaint(bus) is False
    clock.advance(0.9)
    assert timer.wants_repaint(bus) is True


def test_the_editor_preview_shows_a_stand_in_time(qt_app) -> None:
    """A sample bus never fires a cue, and "--" would not show the size."""
    from wer.overlay.sample import sample_bus

    bus = sample_bus()
    timer = _timer()
    assert timer._timer_text(bus) == "In cue 1:24"
    assert timer.image(bus, 1080, 1920) is not None


def test_time_in_cue_keeps_its_settings_through_a_save_and_load() -> None:
    from wer.overlay.layout import widget_from_dict, widget_to_dict
    from wer.overlay.widgets import CueTimerWidget

    saved = widget_to_dict(_timer(count_from="fade_end", prefix=""))
    loaded = widget_from_dict(saved)
    assert isinstance(loaded, CueTimerWidget)
    assert (loaded.count_from, loaded.prefix) == ("fade_end", "")

    saved["count_from"] = "sometime"
    assert widget_from_dict(saved).count_from == "fade_start", (
        "an unrecognised setting stopped the widget loading or kept the nonsense"
    )


def test_time_in_cue_is_on_the_add_menu_and_in_help() -> None:
    from wer.overlay.presets import PRESETS
    from wer.overlay.widgets import CueTimerWidget
    from wer.ui.help_content import _widget_catalogue

    preset = next(p for p in PRESETS if p.key == "cue_timer")
    assert isinstance(preset.build("t"), CueTimerWidget)
    assert "Time in cue" in _widget_catalogue()


# ---------------------------------------------------------- the Overlay tab


@pytest.fixture()
def sheet(qt_app):
    from wer.overlay.compositor import Compositor
    from wer.overlay.sample import sample_bus
    from wer.ui.layout_editor import LayoutEditor

    compositor = Compositor(sample_bus())
    panel = LayoutEditor(compositor)
    yield panel
    panel.deleteLater()


def _showing(panel, row: str) -> bool:
    caption, field = panel._rows[row]
    return not field.isHidden() and not caption.isHidden()


def test_the_overlay_tab_sets_where_time_in_cue_counts_from_and_its_label(sheet) -> None:
    from wer.overlay.widgets import CueTimerWidget

    sheet._add("cue_timer")
    widget = sheet._current
    assert isinstance(widget, CueTimerWidget)
    for row in ("timer_from", "timer_prefix", "font", "colour"):
        assert _showing(sheet, row), row
    for row in ("content", "cue_options", "bar_width"):
        assert not _showing(sheet, row), row

    sheet._timer_from.setCurrentIndex(sheet._timer_from.findData("fade_end"))
    assert widget.count_from == "fade_end"
    assert "finished" in sheet._what.text()

    sheet._timer_prefix.setText("Cue up ")
    assert widget.prefix == "Cue up "
