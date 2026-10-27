"""Cue timing on the overlay: a fade countdown that moves with the video, and
how long the live cue has been up.

The desk reports a fade's time left about once a second -- 5.0, 4.0, 3.0, a
second apart, on a captured five-second fade -- so text showing it stood on
each figure for a second while the video ran at thirty. And nothing showed how
long a cue had been up, counted from when it was fired or from when its fade
landed.

The time in cue counts from the desk's own fire event, so its tests send the
desk's messages through the real connection and replay the captures where
they show what a desk sends: the connect dump names the live cue with no fire
behind it, and Go To Cue on the live cue is a fire with nothing behind it.

Time is a stand-in clock here, so nothing waits and nothing is flaky: the bus
stamps values with time.perf_counter and the widgets read it, and both see the
clock the test moves.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

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


FIXTURES = Path(__file__).parent / "fixtures" / "eos"


def _timer(**kwargs):
    from wer.overlay.widgets import CueTimerWidget

    return CueTimerWidget("timer", **kwargs)


class Desk:
    """The console, through the connection Wer really uses.

    Every message goes through EosConnection._consume, so what reaches the bus
    is what the parser and the connection publish -- the fire, the live cue,
    eos.connected -- stamped on the test's clock. The connection is UDP and is
    never started, so silence is what makes it Stale, as on a real UDP link.

    Times are kept off whole seconds (10.5 s, not 10 s): the clock is a float,
    and a start a tenth of a second off the base is not exact in binary.
    """

    def __init__(self, bus: DataBus, clock: Clock) -> None:
        from wer.connections.base import ConnectionState
        from wer.connections.eos import EosConnection, EosSettings, EosTransport

        self.clock = clock
        self.connection = EosConnection(
            "eos", bus, EosSettings(transport=EosTransport.UDP)
        )
        # Where a run starts: listening, nothing heard yet.
        self.connection._set_state(ConnectionState.WAITING)

    def send(self, address: str, *args) -> None:
        from wer.connections.osc import encode_message

        self.connection._consume(encode_message(address, *args))

    def fire(self, number: str, *, cue_list: str = "1", label: str = "") -> None:
        self.send(f"/eos/out/event/cue/{cue_list}/{number}/fire", label)

    def go(self, number: str, *, fade: float = 3.0, cue_list: str = "1") -> None:
        """A GO as the captures show it: the fire, then about 100 ms later the
        new live cue at the start of its fade, or already landed if it has
        none."""
        self.fire(number, cue_list=cue_list)
        self.clock.advance(0.1)
        self.send(f"/eos/out/active/cue/{cue_list}/{number}", 0.0 if fade else 1.0)

    def fading(self, progress: float) -> None:
        """One of the once-a-second reports while a fade runs."""
        self.send("/eos/out/active/cue", progress)

    def lands(self, number: str, *, cue_list: str = "1", fade: float = 3.0) -> None:
        """The fade finishing, as the desk says it: 1.0, then the 100% text."""
        self.send("/eos/out/active/cue", 1.0)
        self.send("/eos/out/active/cue/text", f"{cue_list}/{number} {fade} 100%")

    def goes_quiet(self, seconds: float) -> None:
        """Nothing from the desk for this long, then the staleness check the
        UI runs twice a second."""
        self.clock.advance(seconds)
        self.connection.refresh_staleness()

    def types(self, text: str = "LIVE: Chan 1") -> None:
        self.send("/eos/out/user/1/cmd", text, 0)

    def replay(self, name: str):
        """A capture, message for message on its own recorded clock. Yields
        each record once it has been sent, so a test can look in between."""
        start = self.clock.now
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            self.clock.now = start + record["t"]
            self.send(record["address"], *record["args"])
            yield record

    def connects(self) -> None:
        """The state dump a real desk sent on connect, with cue 1/58 live."""
        for _record in self.replay("connect-state-dump.jsonl"):
            pass

    def redials(self) -> None:
        """The link drops and a new run starts, as the connection does it."""
        from wer.connections.base import ConnectionState

        self.connection._link_down()
        self.connection._set_state(ConnectionState.WAITING)
        self.connects()


def test_a_cue_already_up_when_wer_connected_has_no_start(clock) -> None:
    """The connect dump names the live cue without firing it, so there is no
    start to count from; counting from when Wer noticed would burn a wrong
    time into the recording. The next GO is a real start."""
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    for _record in desk.replay("connect-state-dump.jsonl"):
        assert timer._timer_text(bus) == "In cue --"
    assert bus.value("eos.cue.active.number") == "58", "precondition: a live cue"
    assert bus.value("eos.connected") is True

    desk.go("62")
    assert timer._timer_text(bus) == "In cue 0:00"
    clock.advance(65.2)
    assert timer._timer_text(bus) == "In cue 1:05"


def test_time_in_cue_starts_again_when_the_next_cue_fires(clock) -> None:
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    desk.go("59")
    for _ in range(40):
        clock.advance(0.25)
        timer._timer_text(bus)
    desk.go("60")
    assert timer._timer_text(bus) == "In cue 0:00"
    clock.advance(3.0)
    assert timer._timer_text(bus) == "In cue 0:03"


def test_a_go_while_the_link_read_stale_is_counted(clock) -> None:
    """Hudson's report: on long cues the time in cue lost its number.

    A quiet desk made the link read Stale, and a GO arriving then was not
    trusted, because the widget had not watched the cue change on a link it
    believed. That whole cue read "--", and typing on the desk could not bring
    it back. The fire is the start now, and the fire is the desk talking.
    """
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    desk.connects()
    timer._timer_text(bus)
    desk.go("62")
    assert timer._timer_text(bus) == "In cue 0:00"

    desk.goes_quiet(130.0)
    assert bus.value("eos.connected") is False, "precondition: the link read Stale"
    assert timer._timer_text(bus) == "In cue --"

    desk.go("66")
    assert bus.value("eos.connected") is True
    assert timer._timer_text(bus) == "In cue 0:00", "a GO on a Stale link was not counted"
    clock.advance(30.0)
    assert timer._timer_text(bus) == "In cue 0:30"


def test_typing_on_the_desk_brings_the_time_back_with_the_start_it_had(clock) -> None:
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    desk.connects()
    timer._timer_text(bus)
    desk.go("62")
    timer._timer_text(bus)
    desk.goes_quiet(130.0)
    assert timer._timer_text(bus) == "In cue --"

    desk.types()
    assert timer._timer_text(bus) == "In cue 2:10"


def test_a_layout_that_was_off_screen_reads_the_right_time_on_its_return(clock) -> None:
    """A start used to be trusted only from a cue change the widget watched,
    so a layout switched away from over a GO came back to "--" for the rest
    of that cue."""
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    desk.connects()
    timer._timer_text(bus)
    desk.go("62")
    assert timer._timer_text(bus) == "In cue 0:00"

    desk.go("66")
    clock.advance(120.0)
    assert timer._timer_text(bus) == "In cue 2:00"


def test_firing_the_live_cue_again_starts_the_count_again(clock) -> None:
    """It is a new marker and a new chapter as well, and the three agree.

    cmdline-and-cue-fire.jsonl is that press on a real desk: the connect dump
    with 58 already live, 85 s of an idle desk, then Go To Cue 58 -- a fire
    with no cue state behind it, because nothing has changed.
    """
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    before_the_fire = set()
    fired_at = None
    for record in desk.replay("cmdline-and-cue-fire.jsonl"):
        if record["address"] == "/eos/out/event/cue/1/58/fire":
            fired_at = clock.now
            assert timer._timer_text(bus) == "In cue 0:00"
        elif fired_at is None:
            before_the_fire.add(timer._timer_text(bus))
    assert before_the_fire == {"In cue --"}, "a cue up at connect was given a start"
    assert fired_at is not None, "fixture lost its cue fire"

    clock.now = fired_at + 61.5
    assert timer._timer_text(bus) == "In cue 1:01"
    desk.fire("58")
    assert timer._timer_text(bus) == "In cue 0:00", "the count went on through a re-fire"


def test_a_fire_is_news_to_the_overlay_even_when_it_names_the_live_cue(
    clock, qt_app
) -> None:
    """The overlay redraws a widget when a key it reads changes, and the bus
    stays quiet about a repeated value. A re-fire repeats the value."""
    from wer.overlay.compositor import Compositor

    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    compositor = Compositor(bus)
    compositor.add(timer)
    desk.go("58")
    clock.advance(30.0)
    timer.image(bus, 1080, 1920)
    assert timer._dirty is False

    desk.fire("58")
    assert timer._dirty is True, "the overlay was not told about a re-fire"


def test_a_redial_does_not_inherit_the_last_fire(clock) -> None:
    """A redial's state dump names the live cue without firing it. Had the
    last fire survived the drop, a dump naming the same cue would have carried
    its start on, even with that cue fired again while the link was down."""
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    desk.connects()
    desk.fire("58")
    clock.advance(10.5)
    assert timer._timer_text(bus) == "In cue 0:10"

    desk.redials()
    assert bus.value("eos.connected") is True
    assert bus.value("eos.cue.active.number") == "58"
    assert timer._timer_text(bus) == "In cue --", "the dump took the old fire's start"


def test_a_live_cue_that_moved_without_a_fire_is_not_given_the_last_fires_start(
    clock,
) -> None:
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    desk.go("58")
    clock.advance(5.0)
    desk.send("/eos/out/active/cue/1/60", 1.0)
    assert timer._timer_text(bus) == "In cue --"

    desk.send("/eos/out/active/cue/text", "")
    assert timer._timer_text(bus) == "In cue --"


def test_a_live_cue_named_only_by_its_text_is_not_given_the_last_fires_start(
    clock,
) -> None:
    """The /text names the live cue too, and moves it when the address has
    not said so. Another cue named there has no fire behind it either."""
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    desk.go("58")
    clock.advance(5.0)
    desk.send("/eos/out/active/cue/text", "1/60 Enter Dromio 2.9 100%")
    assert bus.value("eos.cue.active.number") == "60", "precondition: the text moved it"
    assert timer._timer_text(bus) == "In cue --"


def test_a_point_cue_in_another_list_counts_and_is_told_from_list_ones(
    clock,
) -> None:
    """The fire names its list as well as its number, and both have to match
    the live cue: cue 29.5 in list 2 is not cue 29.5 in list 1."""
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    desk.go("29.5", cue_list="2")
    clock.advance(12.5)
    assert bus.value("eos.cue.fired") == "2/29.5"
    assert timer._timer_text(bus) == "In cue 0:12"

    desk.send("/eos/out/active/cue/1/29.5", 1.0)
    assert timer._timer_text(bus) == "In cue --", "list 1 was given list 2's fire"


def test_a_frame_drawn_part_way_through_a_go_reads_the_new_cue(clock) -> None:
    """A GO reaches the bus one key at a time while frames are being drawn, so
    a frame can fall between the fire and the live cue it names. With the fire
    published last, that frame took the new cue for one that had moved without
    firing, and "--" went into the recording between the last cue's time and
    0:00."""
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    desk.go("58")
    clock.advance(30.5)
    assert timer._timer_text(bus) == "In cue 0:30"

    drawn: list[tuple[str, str]] = []
    publish = bus.publish

    def drawing(key, value, **kwargs):
        entry = publish(key, value, **kwargs)
        drawn.append((key, timer._timer_text(bus)))
        return entry

    bus.publish = drawing  # type: ignore[method-assign]
    try:
        desk.fire("62")
    finally:
        bus.publish = publish  # type: ignore[method-assign]
    assert "eos.cue.active.number" in dict(drawn), "precondition: the GO published"
    assert {text for _key, text in drawn} == {"In cue 0:00"}, drawn


def test_a_go_landing_while_a_frame_reads_the_bus_is_not_misread(clock) -> None:
    """A frame reads the live cue and the fire one after the other, and the GO
    can land between the two reads. Read the other way round, that paired the
    fire from before the GO with the live cue from after it, and drew "--"."""
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    desk.go("58")
    clock.advance(30.5)
    read = bus.get
    pending = ["62"]

    def reading(key):
        entry = read(key)
        if pending and key in ("eos.cue.active.number", "eos.cue.fired"):
            desk.fire(pending.pop())
        return entry

    # Asked of the start directly: the text around it reads the live cue
    # once already, to tell a preview, and the GO would land on that read.
    bus.get = reading  # type: ignore[method-assign]
    try:
        start = timer._fired_at(bus, clock.now)
    finally:
        bus.get = read  # type: ignore[method-assign]
    assert not pending, "precondition: the GO landed while the frame read"
    assert start == clock.now, "the GO's start was not read"
    assert timer._timer_text(bus) == "In cue 0:00"


def test_counting_from_the_end_of_the_fade_holds_at_zero_until_it_lands(clock) -> None:
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer(count_from="fade_end")
    desk.go("59")
    for progress in (0.33, 0.66):
        clock.advance(1.0)
        desk.fading(progress)
        assert timer._timer_text(bus) == "In cue 0:00", "counted before the fade landed"
    clock.advance(1.0)
    desk.lands("59")
    assert timer._timer_text(bus) == "In cue 0:00"
    clock.advance(10.5)
    assert timer._timer_text(bus) == "In cue 0:10"


def test_a_go_pressed_as_the_last_fade_landed_does_not_take_that_landing(
    clock,
) -> None:
    """Until the desk describes the new cue, about 100 ms behind its fire, the
    fade key still holds the last cue's "not fading". Taken for this cue's, the
    count started at the fire with the new fade still to run."""
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer(count_from="fade_end")
    desk.connects()
    timer._timer_text(bus)
    desk.go("62")
    timer._timer_text(bus)
    clock.advance(3.0)
    desk.lands("62")
    timer._timer_text(bus)
    clock.advance(0.05)

    desk.fire("66")
    assert timer._timer_text(bus) == "In cue 0:00"
    clock.advance(0.1)
    desk.send("/eos/out/active/cue/1/66", 0.0)
    for report in (0.5, None):
        clock.advance(1.0)
        assert timer._timer_text(bus) == "In cue 0:00", "counted through the new fade"
        if report is not None:
            desk.fading(report)
    desk.lands("66", fade=2.0)
    clock.advance(4.5)
    assert timer._timer_text(bus) == "In cue 0:04"


def test_counting_from_the_end_of_the_fade_is_right_after_a_spell_off_screen(
    clock,
) -> None:
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer(count_from="fade_end")
    desk.go("59")
    assert timer._timer_text(bus) == "In cue 0:00"
    clock.advance(3.0)
    desk.lands("59")
    clock.advance(60.5)
    assert timer._timer_text(bus) == "In cue 1:00"


def test_counting_from_the_end_keeps_the_first_landing_when_it_is_said_again(
    clock,
) -> None:
    """The bus stamps a value published again unchanged as new, so the fade
    key's stamp moves each time the cue's settled state reaches it again. The
    first landing after the fire is the one kept, or each repeat would start
    the count again."""
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer(count_from="fade_end")
    desk.go("59")
    clock.advance(3.0)
    desk.lands("59")
    assert timer._timer_text(bus) == "In cue 0:00"
    clock.advance(40.5)
    desk.lands("59")
    clock.advance(20.0)
    assert timer._timer_text(bus) == "In cue 1:00", "the repeat moved the landing"


def test_counting_from_the_start_ignores_how_long_the_fade_took(clock) -> None:
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer(count_from="fade_start")
    desk.go("59")
    clock.advance(2.95)
    desk.lands("59")
    assert timer._timer_text(bus) == "In cue 0:03"


def test_a_cue_with_no_fade_counts_from_its_first_report_either_way(clock) -> None:
    for count_from in ("fade_start", "fade_end"):
        bus = DataBus()
        desk = Desk(bus, clock)
        timer = _timer(count_from=count_from)
        desk.go("59", fade=0.0)
        timer._timer_text(bus)
        clock.advance(5.5)
        assert timer._timer_text(bus) == "In cue 0:05", count_from


def test_counting_from_the_end_waits_for_a_report_after_a_re_fire(clock) -> None:
    """A re-fire of a settled cue sends no fade report at all -- the capture
    in test_firing_the_live_cue_again_starts_the_count_again has none -- so
    the count holds at 0:00 until the desk next describes the cue, rather
    than inventing a landing for it."""
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer(count_from="fade_end")
    desk.go("58")
    clock.advance(3.0)
    desk.lands("58")
    clock.advance(30.5)
    assert timer._timer_text(bus) == "In cue 0:30"

    desk.fire("58")
    clock.advance(30.0)
    assert timer._timer_text(bus) == "In cue 0:00"
    desk.lands("58")
    clock.advance(5.5)
    assert timer._timer_text(bus) == "In cue 0:05"


def test_time_in_cue_passes_an_hour_as_hours(clock) -> None:
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer(prefix="")
    desk.go("59")
    clock.advance(3605.0)
    assert timer._timer_text(bus) == "1:00:05"


def test_time_in_cue_is_redrawn_once_a_second_not_every_frame(clock, qt_app) -> None:
    bus = DataBus()
    desk = Desk(bus, clock)
    timer = _timer()
    desk.go("59")
    timer.notify_changed()
    assert timer.image(bus, 1080, 1920) is not None
    clock.advance(0.2)
    assert timer.wants_repaint(bus) is False
    clock.advance(0.9)
    assert timer.wants_repaint(bus) is True


def test_the_editor_preview_shows_a_stand_in_time(qt_app) -> None:
    """A sample bus's fire is stamped as the preview is built, and 0:00 or
    "--" would not show the size."""
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
