"""Eos parser, exercised against real console captures.

The fixtures in ``tests/fixtures/eos/`` are recordings from an ETCnomad, not
hand-written examples. That is the point: a parser written against a guessed
spec is worse than no parser, so these tests replay what the console actually
sent.

No Qt in this process (see tests/test_architecture.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from wer.connections.eos_parser import BusUpdate, EosParser, parse_cue_text
from wer.connections.osc import OscMessage

FIXTURES = Path(__file__).parent / "fixtures" / "eos"


def load(name: str) -> Iterator[OscMessage]:
    """Replay a captured .jsonl as OscMessage objects."""
    path = FIXTURES / name
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            args = tuple(
                bytes.fromhex(a["__blob__"]) if isinstance(a, dict) and "__blob__" in a
                else a
                for a in record["args"]
            )
            yield OscMessage(
                address=record["address"],
                args=args,
                typetags=record.get("typetags", ""),
            )


def replay(name: str) -> tuple[dict[str, Any], list]:
    """Replay a fixture; return the final bus state and every fire event."""
    parser = EosParser()
    state: dict[str, Any] = {}
    fires: list = []
    for message in load(name):
        result = parser.handle(message)
        for update in result.updates:
            state[update.key] = update.value
        fires.extend(result.events)
    return state, fires


# --------------------------------------------------------------- fixture sanity


def test_fixtures_are_present() -> None:
    """Guard against the captures being lost - the parser is meaningless without."""
    expected = {
        "connect-state-dump.jsonl",
        "cmdline-and-cue-fire.jsonl",
        "cue-advance.jsonl",
        "fade-progress.jsonl",
    }
    assert expected <= {p.name for p in FIXTURES.glob("*.jsonl")}


# ------------------------------------------------------------------ connect dump


def test_connect_dump_yields_show_and_cue_identity() -> None:
    state, _ = replay("connect-state-dump.jsonl")
    assert state["eos.show.name"] == "Comedy EOS"
    assert state["eos.cue.active.list"] == "1"
    assert state["eos.cue.active.number"] == "58"
    assert state["eos.cue.pending.number"] == "62"
    assert state["eos.cue.previous.number"] == "54"
    assert state["eos.user"] == 1


def test_cue_number_comes_from_the_address_not_the_arguments() -> None:
    """The argument was predicted to be label+duration. It is a float."""
    parser = EosParser()
    result = parser.handle(OscMessage("/eos/out/active/cue/1/58", (1.0,), "f"))
    keys = {u.key: u.value for u in result.updates}
    assert keys["eos.cue.active.list"] == "1"
    assert keys["eos.cue.active.number"] == "58"
    assert keys["eos.cue.active.progress"] == 1.0


def test_label_comes_from_the_fire_event() -> None:
    state, fires = replay("cmdline-and-cue-fire.jsonl")
    assert fires, "the capture contains a cue fire"
    assert state["eos.cue.active.label"] == "Adriana Xs Center"


# ------------------------------------------------------------------ cue advance


def test_cue_advance_produces_one_fire_event_per_cue() -> None:
    _, fires = replay("cue-advance.jsonl")
    fired = [(f.cue_list, f.number, f.label) for f in fires]
    assert ("1", "65", "Antiph Quote") in fired
    assert ("1", "66", "Dromio Quote") in fired


def test_cue_advance_tracks_the_whole_go_sequence() -> None:
    """The capture is twelve consecutive GOs. Every one must be seen, in order.

    Note the cue numbers are not consecutive integers (74 -> 78 -> 82): a cue
    list is whatever the designer numbered it, and anything that assumes
    "next cue = number + 1" is broken.
    """
    state, fires = replay("cue-advance.jsonl")
    assert [f.number for f in fires] == [
        "65", "66", "67", "68", "69", "70", "71", "72", "73", "74", "78", "82",
    ]
    assert state["eos.cue.active.number"] == "82"
    assert state["eos.cue.pending.number"] == "86"


def test_fire_event_display_is_marker_ready() -> None:
    """This goes straight into a marker file."""
    _, fires = replay("cue-advance.jsonl")
    match = next(f for f in fires if f.number == "65")
    assert match.display == "1/65 Antiph Quote"


# ---------------------------------------------------------------- fade progress


def test_the_addressed_form_can_arrive_part_way_through_a_fade() -> None:
    """The docstring on the handler said 0.0 or 1.0, and the reference said
    "0.0 when the cue starts fading". In the macro capture cue 1/245 (0.2 s)
    arrived at 0.31 and 1/180 (5.0 s) at 0.01 -- and 1/250, another 0.2 s cue
    whose state followed its fire event just as closely (126 ms, against 158
    for 1/245), at 0.0. The value is wherever the fade had got when the state
    was sent, nothing about the cue predicts it, and it has to be published
    as such -- a fade in flight, with the stale window a fade in flight
    gets."""
    expected = {
        "/eos/out/active/cue/1/245": 0.31,
        "/eos/out/active/cue/1/250": 0.0,
        "/eos/out/active/cue/1/180": 0.01,
    }
    parser = EosParser()
    seen: dict[str, dict[str, BusUpdate]] = {}
    for message in load("macro-record-toggle.jsonl"):
        updates = {u.key: u for u in parser.handle(message).updates}
        if message.address in expected:
            seen[message.address] = updates

    assert set(seen) == set(expected)
    for address, value in expected.items():
        progress = seen[address]["eos.cue.active.progress"]
        assert abs(progress.value - value) < 0.001, address
        assert seen[address]["eos.cue.active.fading"].value is True, address
        assert progress.stale_after is not None, "a fade in flight must expire"


def test_fade_progress_streams_on_the_bare_address() -> None:
    """The addressed form fires once, at wherever the fade has got (0.0 in
    this capture); the bare form streams."""
    parser = EosParser()
    seen: list[float] = []
    for message in load("fade-progress.jsonl"):
        for update in parser.handle(message).updates:
            if update.key == "eos.cue.active.progress":
                seen.append(update.value)

    intermediate = [p for p in seen if 0.0 < p < 1.0]
    assert len(intermediate) > 10, (
        "expected a stream of intermediate fade values, got " f"{sorted(set(seen))}"
    )
    assert max(seen) == 1.0


def test_fade_progress_is_monotonic_within_a_cue() -> None:
    """A progress bar that goes backwards is worse than none."""
    parser = EosParser()
    runs: list[list[float]] = [[]]
    for message in load("fade-progress.jsonl"):
        for update in parser.handle(message).updates:
            if update.key == "eos.cue.active.number":
                runs.append([])
            elif update.key == "eos.cue.active.progress":
                runs[-1].append(update.value)

    for run in runs:
        assert run == sorted(run), f"fade progress went backwards: {run}"


def test_fade_progress_carries_a_stale_window() -> None:
    """A frozen bar must not pass for a working one."""
    parser = EosParser()
    result = parser.handle(OscMessage("/eos/out/active/cue", (0.5,), "f"))
    progress = next(u for u in result.updates if u.key == "eos.cue.active.progress")
    assert progress.stale_after is not None and progress.stale_after > 0


def test_duration_is_latched_at_zero_percent() -> None:
    """Total fade time is only knowable at 0%, where remaining == duration."""
    parser = EosParser()
    parser.handle(OscMessage("/eos/out/active/cue/1/58", (0.0,), "f"))
    result = parser.handle(
        OscMessage("/eos/out/active/cue/text", ("1/58 Adriana Xs Center 19.9 0%",), "s")
    )
    keys = {u.key: u.value for u in result.updates}
    assert keys["eos.cue.active.duration"] == 19.9
    assert keys["eos.cue.active.remaining"] == 19.9

    later = parser.handle(
        OscMessage("/eos/out/active/cue/text", ("1/58 Adriana Xs Center 12.6 36%",), "s")
    )
    keys = {u.key: u.value for u in later.updates}
    assert keys["eos.cue.active.remaining"] == 12.6
    assert keys["eos.cue.active.duration"] == 19.9, "duration should stay latched"


def test_completion_does_not_let_remaining_jump_upward() -> None:
    """At 100% the time field reverts to the cue's recorded time, not remaining.

    Publishing that as "remaining" would make the readout leap up at the end of
    every single cue.
    """
    parser = EosParser()
    parser.handle(OscMessage("/eos/out/active/cue/1/58", (0.0,), "f"))
    parser.handle(
        OscMessage("/eos/out/active/cue/text", ("1/58 Adriana Xs Center 19.9 0%",), "s")
    )
    parser.handle(
        OscMessage("/eos/out/active/cue/text", ("1/58 Adriana Xs Center 0.2 99%",), "s")
    )
    result = parser.handle(
        OscMessage("/eos/out/active/cue/text", ("1/58 Adriana Xs Center 3.0 100%",), "s")
    )
    keys = {u.key: u.value for u in result.updates}
    assert keys["eos.cue.active.remaining"] == 0.0, "must not report 3.0 as remaining"
    assert keys["eos.cue.active.fading"] is False


def test_the_duration_settles_when_the_fade_finishes() -> None:
    """Every duration update carried a 3 s expiry, including the last one.

    Three seconds after each cue finished fading, the key expired and the Cue
    duration preset burned "Time --s" into the recording for as long as that cue
    was up -- which on a settled console is most of the show.
    """
    parser = EosParser()
    parser.handle(OscMessage("/eos/out/active/cue/1/62", (0.0,), "f"))
    parser.handle(
        OscMessage("/eos/out/active/cue/text", ("1/62 Enter Dromio 2.9 0%",), "s")
    )
    result = parser.handle(
        OscMessage("/eos/out/active/cue/text", ("1/62 Enter Dromio 2.9 100%",), "s")
    )

    duration = next(u for u in result.updates if u.key == "eos.cue.active.duration")
    assert duration.value == 2.9
    assert duration.stale_after is None, "a finished fade is a settled fact"


def test_a_desk_that_was_already_settled_still_reports_a_duration() -> None:
    """connect-state-dump.jsonl is Wer connecting to a console sitting on a cue
    that finished fading long ago. It never sees a 0%, so the duration used to
    be published not once -- the widget read "Time --s" from the first frame."""
    state, _ = replay("connect-state-dump.jsonl")
    assert state["eos.cue.active.duration"] == 3.0


def test_the_last_duration_of_a_real_capture_does_not_expire() -> None:
    parser = EosParser()
    last = None
    for message in load("fade-progress.jsonl"):
        for update in parser.handle(message).updates:
            if update.key == "eos.cue.active.duration":
                last = update
    assert last is not None
    assert last.stale_after is None, (
        "the fade the capture ends on is complete; its duration is not going "
        "out of date"
    )


# ------------------------------------------------------------------ command line


def test_command_line_error_flag_comes_from_the_integer() -> None:
    """The error TEXT is inconsistently formatted; the int is not."""
    parser = EosParser()
    ok = parser.handle(OscMessage("/eos/out/cmd", ("LIVE: Cue  58 : ", 0), "si"))
    assert {u.key: u.value for u in ok.updates}["eos.cmdline.error"] is False

    for text in (
        "LIVE: Cue  58 : Sub  Full - Error: Syntax Error",
        "LIVE: Cue  82 : Go To Cue 587 Error : Cue Does Not Exist",
        "LIVE: Cue  82 : Go To Cue 5871 Error: Number Out Of Range",
    ):
        bad = parser.handle(OscMessage("/eos/out/cmd", (text, 1), "si"))
        assert {u.key: u.value for u in bad.updates}["eos.cmdline.error"] is True


def test_per_user_command_lines_are_kept_apart() -> None:
    """All users' command lines are broadcast, and they differ."""
    state, _ = replay("connect-state-dump.jsonl")
    assert state["eos.cmdline.user.0.text"] != state["eos.cmdline.user.1.text"]
    assert state["eos.cmdline.user.1.text"].startswith("LIVE: Cue  58")
    assert state["eos.cmdline.user.2.error"] is True


# -------------------------------------------------------------- text parsing


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "1/58 Adriana Xs Center 3.0 100%",
            {"list": "1", "number": "58", "label": "Adriana Xs Center",
             "time": 3.0, "percent": 100},
        ),
        (
            "1/62 Enter Dromio 2.9",
            {"list": "1", "number": "62", "label": "Enter Dromio", "time": 2.9},
        ),
        (
            "1/82 Triangle transitiion 6.0 100%",
            {"list": "1", "number": "82", "label": "Triangle transitiion",
             "time": 6.0, "percent": 100},
        ),
        (
            "1/86 Button 0.0",
            {"list": "1", "number": "86", "label": "Button", "time": 0.0},
        ),
    ],
)
def test_parse_cue_text(text: str, expected: dict[str, Any]) -> None:
    assert parse_cue_text(text) == expected


def test_parse_cue_text_returns_none_rather_than_raising() -> None:
    """Callers treat None as 'no extra information', never as an error."""
    assert parse_cue_text("not a cue") is None
    assert parse_cue_text("") is None


# --------------------------------------------------------------------- misc


def test_unknown_addresses_are_ignored_silently() -> None:
    """Eos emits plenty we have no use for, and firmware will add more."""
    parser = EosParser()
    for address in ("/eos/out/wheel", "/eos/out/pantilt", "/eos/out/notify/setup/list/0/1"):
        assert not parser.handle(OscMessage(address, (0.0,), "f")).updates


def test_softkeys_are_captured() -> None:
    state, _ = replay("connect-state-dump.jsonl")
    assert state["eos.softkey.1"] == "Address"
    assert state["eos.softkey.10"] == ""


def test_parser_namespace_is_configurable() -> None:
    """Two consoles on one show must not collide on the bus."""
    parser = EosParser(namespace="eos2")
    result = parser.handle(OscMessage("/eos/out/show/name", ("Other",), "s"))
    assert result.updates == [BusUpdate("eos2.show.name", "Other")]


def test_completed_fade_does_not_expire() -> None:
    """A finished fade is a settled fact, not a value going out of date.

    Found by looking at the data monitor: an idle console on a completed cue
    was painting progress/fading/remaining red, which reads as a fault. "Cue 98
    finished fading" stays true as long as cue 98 is up.
    """
    parser = EosParser()
    result = parser.handle(OscMessage("/eos/out/active/cue", (1.0,), "f"))
    for update in result.updates:
        assert update.stale_after is None, (
            f"{update.key} should not expire once the fade is complete"
        )


def test_fade_in_flight_still_expires() -> None:
    """The dropout case the stale window exists for must still work."""
    parser = EosParser()
    result = parser.handle(OscMessage("/eos/out/active/cue", (0.4,), "f"))
    assert result.updates
    for update in result.updates:
        assert update.stale_after is not None, (
            f"{update.key} must expire while a fade is in flight"
        )


def test_completion_via_text_also_settles() -> None:
    parser = EosParser()
    parser.handle(OscMessage("/eos/out/active/cue/1/58", (0.0,), "f"))
    parser.handle(
        OscMessage("/eos/out/active/cue/text", ("1/58 Label 19.9 0%",), "s")
    )
    result = parser.handle(
        OscMessage("/eos/out/active/cue/text", ("1/58 Label 3.0 100%",), "s")
    )
    settled = {u.key: u for u in result.updates}
    assert settled["eos.cue.active.fading"].stale_after is None
    assert settled["eos.cue.active.remaining"].stale_after is None


# ------------------------------ the cue number keeps up with the label


def _bus_after(messages):
    """Run a sequence through a fresh parser and return the resulting bus."""
    from wer.connections.eos_parser import EosParser
    from wer.connections.osc import OscMessage

    parser = EosParser()
    bus = {}
    for address, args in messages:
        for update in parser.handle(OscMessage(address, list(args))).updates:
            bus[update.key] = update.value
    return bus


def test_a_cue_fire_publishes_the_number_not_just_the_label() -> None:
    """Reported from the field as "the label updates but not the cue".

    A desk does not send /eos/out/active/cue/<list>/<number> on every change.
    Over a minute of a running cue list on a real Ion XE, four cues fired and
    only one produced that address -- so the number came only from /text, and
    between the fire and the text the overlay showed the new label beside the
    old number.
    """
    bus = _bus_after([
        ("/eos/out/active/cue/1/10", (1.0,)),
        ("/eos/out/active/cue/text", ("1/10 label 10 5.0 100%",)),
        ("/eos/out/event/cue/1/20/fire", ("label 20",)),
    ])
    assert bus["eos.cue.active.number"] == "20"
    assert bus["eos.cue.active.label"] == "label 20"


def test_the_fire_event_also_moves_the_list() -> None:
    bus = _bus_after([("/eos/out/event/cue/3/44/fire", ("Blackout",))])
    assert bus["eos.cue.active.list"] == "3"
    assert bus["eos.cue.active.number"] == "44"


def test_an_unlabelled_cue_does_not_inherit_the_previous_label() -> None:
    """Cue 20.1 on Hudson's desk fires with an empty label. Keeping the last
    one meant the overlay captioned it "label 20" -- wrong information burned
    into the recording, which is worse than no information."""
    bus = _bus_after([
        ("/eos/out/event/cue/1/20/fire", ("label 20",)),
        ("/eos/out/event/cue/1/20.1/fire", ("",)),
    ])
    assert bus["eos.cue.active.number"] == "20.1"
    assert bus["eos.cue.active.label"] == ""


def test_cue_text_with_no_label_also_clears_it() -> None:
    bus = _bus_after([
        ("/eos/out/active/cue/text", ("1/20 label 20 5.0 100%",)),
        ("/eos/out/active/cue/text", ("1/20.1 5.0 100%",)),
    ])
    assert bus["eos.cue.active.number"] == "20.1"
    assert bus["eos.cue.active.label"] == ""


def test_number_and_label_never_disagree_across_a_real_sequence() -> None:
    """The invariant that matters: at no point may the displayed number belong
    to one cue and the displayed label to another."""
    from wer.connections.eos_parser import EosParser
    from wer.connections.osc import OscMessage

    sequence = [
        ("/eos/out/active/cue/1/10", (1.0,)),
        ("/eos/out/active/cue/text", ("1/10 label 10 5.0 100%",)),
        ("/eos/out/event/cue/1/20/fire", ("label 20",)),
        ("/eos/out/active/cue/text", ("1/20 label 20 5.0 100%",)),
        ("/eos/out/event/cue/1/20.1/fire", ("",)),
        ("/eos/out/active/cue/text", ("1/20.1 5.0 100%",)),
        ("/eos/out/event/cue/1/1/fire", ("label 1",)),
        ("/eos/out/active/cue/text", ("1/1 label 1 3.0 100%",)),
    ]
    expected = {"10": "label 10", "20": "label 20", "20.1": "", "1": "label 1"}

    parser = EosParser()
    bus = {}
    for address, args in sequence:
        for update in parser.handle(OscMessage(address, list(args))).updates:
            bus[update.key] = update.value
        number = bus.get("eos.cue.active.number")
        label = bus.get("eos.cue.active.label")
        if number is not None and label is not None:
            assert label == expected[number], (
                f"after {address}: cue {number} showed label {label!r}, "
                f"which belongs to a different cue"
            )


# ------------------------ an empty cue text means "nothing", not "no news"


def test_an_empty_pending_text_clears_the_next_cue() -> None:
    """Reported from the field: "Next" stuck on 20.1 forever.

    Eos sends an empty /eos/out/pending/cue/text once the list has run past
    its last entry. The parser bailed on it, so the previous cue's number and
    label stayed on screen with nothing actually pending.
    """
    bus = _bus_after([
        ("/eos/out/pending/cue/text", ("1/20.1 5.0",)),
        ("/eos/out/pending/cue/text", ("",)),
    ])
    assert bus["eos.cue.pending.number"] == ""
    assert bus["eos.cue.pending.label"] == ""


def test_an_empty_active_text_clears_the_live_cue() -> None:
    bus = _bus_after([
        ("/eos/out/active/cue/text", ("1/20 label 20 5.0 100%",)),
        ("/eos/out/active/cue/text", ("",)),
    ])
    assert bus["eos.cue.active.number"] == ""
    assert bus["eos.cue.active.label"] == ""


def test_clearing_the_active_cue_also_drops_the_latched_identity() -> None:
    """The latched number is what turns a bare fade message into a cue
    identity. Leaving it behind would attribute the next fade to a cue that
    is no longer running."""
    from wer.connections.eos_parser import EosParser
    from wer.connections.osc import OscMessage

    parser = EosParser()
    parser.handle(OscMessage("/eos/out/active/cue/1/58", [1.0]))
    assert parser._active_number == "58"
    parser.handle(OscMessage("/eos/out/active/cue/text", [""]))
    assert parser._active_number is None


def test_an_unreadable_text_does_not_wipe_a_cue_that_is_really_there() -> None:
    """Labels contain spaces and digits, so the field boundary is genuinely
    ambiguous and a parse failure is expected now and then. The cue exists;
    the last known identity is closer to the truth than a blank."""
    bus = _bus_after([
        ("/eos/out/event/cue/1/58/fire", ("Adriana Xs Center",)),
        ("/eos/out/active/cue/text", ("this is not a cue text at all",)),
    ])
    assert bus["eos.cue.active.number"] == "58"
    assert bus["eos.cue.active.label"] == "Adriana Xs Center"


def test_the_raw_text_is_published_whatever_happens() -> None:
    """The text widget must always show exactly what the desk said, parsed or
    not, empty or not."""
    for text in ("", "1/20 label 20 5.0", "unparseable rubbish"):
        bus = _bus_after([("/eos/out/active/cue/text", (text,))])
        assert bus["eos.cue.active.text"] == text


# --------------------- a fade's duration belongs to its own cue


def test_a_fade_first_seen_mid_flight_does_not_inherit_the_last_duration() -> None:
    """From the real capture: the fire event leads the state update by 50-100
    ms, so any fade under about three seconds is first reported at a non-zero
    percent. Cue 74 in cue-advance.jsonl first appears at 3%."""
    bus = _bus_after([
        ("/eos/out/active/cue/text", ("1/78 Dromio Exits 2.9 0%",)),
        ("/eos/out/active/cue/text", ("1/78 Dromio Exits 2.9 100%",)),
        ("/eos/out/event/cue/1/74/fire", ("Bit Ends",)),
        ("/eos/out/active/cue/text", ("1/74 Bit Ends 1.9 3%",)),
    ])
    assert bus["eos.cue.active.number"] == "74"
    assert bus["eos.cue.active.duration"] != 2.9, "inherited cue 78's fade time"


def test_the_duration_is_recovered_from_remaining_and_percent() -> None:
    """The desk gives remaining time and percent complete, and those give the
    whole. Cue 74 is really 2.0 s; blanking it would be honest but recovering
    it is better."""
    for text in ("1/74 Bit Ends 1.9 3%", "1/74 Bit Ends 0.9 54%"):
        bus = _bus_after([("/eos/out/event/cue/1/74/fire", ("Bit Ends",)),
                          ("/eos/out/active/cue/text", (text,))])
        assert bus["eos.cue.active.duration"] == 2.0, text


def test_a_fade_seen_from_zero_is_still_exact() -> None:
    """The recovery must not disturb the case that already worked."""
    bus = _bus_after([("/eos/out/active/cue/text", ("1/82 Triangle 6.0 0%",))])
    assert bus["eos.cue.active.duration"] == 6.0


def test_firing_a_cue_clears_the_previous_fade_immediately() -> None:
    """Between the fire and the first text there is nothing true to say about
    the new cue's fade, so the old one must not stand in for it."""
    bus = _bus_after([
        ("/eos/out/active/cue/text", ("1/78 Dromio Exits 2.9 0%",)),
        ("/eos/out/event/cue/1/74/fire", ("Bit Ends",)),
    ])
    assert bus["eos.cue.active.duration"] is None
    assert bus["eos.cue.active.remaining"] is None


def test_every_cue_in_the_real_capture_reports_its_own_fade_time() -> None:
    """The end-to-end guard, replayed from the desk's own traffic. The 100%
    text states the cue's recorded time, so it is ground truth."""
    import json

    from wer.connections.eos_parser import EosParser, parse_cue_text
    from wer.connections.osc import OscMessage

    rows = [json.loads(line) for line
            in Path("tests/fixtures/eos/cue-advance.jsonl").read_text(
                encoding="utf-8").splitlines() if line.strip()]
    parser, bus, seen = EosParser(), {}, 0
    for row in rows:
        for update in parser.handle(
            OscMessage(row["address"], list(row.get("args") or []))
        ).updates:
            bus[update.key] = update.value
        if row["address"] != "/eos/out/active/cue/text":
            continue
        parsed = parse_cue_text(str((row.get("args") or [""])[0]))
        if not parsed or parsed.get("percent") not in (None, 100):
            continue
        truth = parsed["time"]
        if truth <= 0:
            continue          # an instant cue has no fade to describe
        seen += 1
        got = bus.get("eos.cue.active.duration")
        assert got is not None and abs(got - truth) <= 0.15, (
            f"cue {parsed['number']}: desk says {truth}s, bus says {got}"
        )
    assert seen >= 2, "the capture should contain real fades to check"


# ---------------------- the command line keeps moving when bound to user 0


def test_a_per_user_command_line_also_updates_the_generic_key() -> None:
    """Bound to user 0, Eos sends /eos/out/cmd once at connect and then only
    the per-user address. A command-line widget reads the generic key, so it
    sat on "LIVE: Cue 1 :" for an entire session while the desk had moved on
    to cue 20."""
    bus = _bus_after([
        ("/eos/out/cmd", ("LIVE: Cue  1 : ", 0)),
        ("/eos/out/user/2/cmd", ("LIVE: Cue  10 : ", 0)),
        ("/eos/out/user/2/cmd", ("LIVE: Cue  20 : ", 0)),
    ])
    assert bus["eos.cmdline.text"] == "LIVE: Cue  20 : "
    assert bus["eos.cmdline.user.2.text"] == "LIVE: Cue  20 : "


def test_the_error_flag_travels_with_it() -> None:
    bus = _bus_after([("/eos/out/user/2/cmd", ("Chan 9999 Error : ", 1))])
    assert bus["eos.cmdline.error"] is True
    assert bus["eos.cmdline.user.2.error"] is True
