"""Turn Eos OSC messages into DataBus keys.

Written against real captures in ``tests/fixtures/eos/``, not against a written
table of messages -- which was wrong about the most important detail: the cue
list and number arrive in the address, not the arguments. Every rule in this
file traces to an observed message; anything not observed is absent rather than
guessed.

No Qt, no sockets, no DataBus import. This is a pure function of messages to
updates, so it can be tested by replaying a fixture in a bare interpreter.

Key namespace
-------------
::

    eos.show.name                  str
    eos.user                       int   OSC user this connection is bound to
    eos.connected                  bool

    eos.cue.active.list            str   from the ADDRESS, not the arguments
    eos.cue.active.number          str
    eos.cue.active.label           str
    eos.cue.active.text            str   raw "1/58 Label 19.9 0%"
    eos.cue.active.progress        float 0.0-1.0, arrives at ~1 Hz
    eos.cue.active.remaining       float seconds left in the fade
    eos.cue.active.duration        float total fade: captured at 0%, and
                                         settled at 100% to the cue's own
                                         recorded time, which is what the desk
                                         reports once the fade has landed
    eos.cue.active.fading          bool

    eos.cue.pending.list/.number/.label/.text
    eos.cue.previous.list/.number/.label/.text

    eos.cmdline.text               str   updates per keystroke
    eos.cmdline.error              bool  from the int argument, not the text
    eos.cmdline.user.<n>.text      str
    eos.cmdline.user.<n>.error     bool

    eos.chan.active                str
    eos.softkey.<1-12>             str
    eos.event.state                int
    eos.event.locked               int
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from wer.connections.osc import OscBundle, OscMessage

__all__ = [
    "BusUpdate",
    "CueFired",
    "ParseResult",
    "EosParser",
    "parse_cue_text",
]

#: A fade IN PROGRESS is only believable while the console keeps saying so. If
#: Eos goes away mid-fade, a progress bar frozen at 40% is worse than one that
#: admits it has lost contact. Fade progress arrives at ~1 Hz, so three missed
#: updates is a generous threshold.
#:
#: This applies ONLY while progress < 1.0. A completed fade is a settled fact,
#: not a value going out of date: "cue 98 finished fading" stays true for as
#: long as cue 98 is up. Expiring it would paint a healthy idle console red and
#: send the user hunting for a fault that is not there.
FADE_STALE_AFTER = 3.0


def _fade_expiry(progress: float) -> float | None:
    """Stale window for fade keys: a window while fading, none once settled."""
    return FADE_STALE_AFTER if progress < 1.0 else None

#: The command line and cue identity have no natural expiry - they stay true
#: until the console says otherwise. Staleness for those is handled at the
#: connection level, by marking the whole connection stale.
NO_EXPIRY: float | None = None


@dataclass(frozen=True, slots=True)
class BusUpdate:
    """One key to publish. Deliberately not a DataBus call, so this is testable."""

    key: str
    value: Any
    stale_after: float | None = None


@dataclass(frozen=True, slots=True)
class CueFired:
    """A cue actually went.

    An auto-marker is dropped on these. This is the right trigger: the fire
    event leads the state update by ~90 ms and carries the label directly, so
    it is both the earliest and the cleanest signal.
    """

    cue_list: str
    number: str
    label: str

    @property
    def display(self) -> str:
        return f"{self.cue_list}/{self.number} {self.label}".strip()


@dataclass(slots=True)
class ParseResult:
    updates: list[BusUpdate] = field(default_factory=list)
    events: list[CueFired] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.updates or self.events)


# Addresses that carry the cue number in the path rather than the arguments.
_ACTIVE_CUE = re.compile(r"^/eos/out/active/cue/(\d+)/([\d.]+)$")
_PENDING_CUE = re.compile(r"^/eos/out/pending/cue/(\d+)/([\d.]+)$")
_PREVIOUS_CUE = re.compile(r"^/eos/out/previous/cue/(\d+)/([\d.]+)$")
_CUE_FIRE = re.compile(r"^/eos/out/event/cue/(\d+)/([\d.]+)/fire$")
_USER_CMD = re.compile(r"^/eos/out/user/(\d+)/cmd$")
_SOFTKEY = re.compile(r"^/eos/out/softkey/(\d+)$")

# "1/58 Adriana Xs Center 19.9 0%" -> list, number, label, time, percent.
# The percent group is optional because pending and previous omit it.
# The label is non-greedy and may be empty; anchoring on the trailing numeric
# fields is what makes this tractable at all, and it is still only a fallback -
# the label from the fire event is authoritative.
_CUE_TEXT = re.compile(
    r"^(?P<list>\d+)/(?P<number>[\d.]+)\s+"
    r"(?P<label>.*?)\s*"
    r"(?P<time>\d+(?:\.\d+)?)\s*"
    r"(?:(?P<percent>\d+)%)?$"
)


def parse_cue_text(text: str) -> dict[str, Any] | None:
    """Pull the fields out of a ``/eos/out/*/cue/text`` string.

    Returns ``None`` if the string does not match, which is a real possibility:
    labels contain spaces and digits, so the boundary between label and time is
    ambiguous by inspection. Callers must treat a ``None`` as "no extra
    information", never as an error worth surfacing.
    """
    match = _CUE_TEXT.match(text.strip())
    if match is None:
        return None
    parsed: dict[str, Any] = {
        "list": match.group("list"),
        "number": match.group("number"),
        "label": match.group("label"),
        "time": float(match.group("time")),
    }
    if match.group("percent") is not None:
        parsed["percent"] = int(match.group("percent"))
    return parsed


class EosParser:
    """Stateful translator from Eos OSC to bus updates.

    State is needed for two reasons the captures made clear:

    - There is no address whose *argument* is the current cue number, so the
      parser must remember what it last saw in an address path.
    - Fade duration is only knowable at the moment progress hits 0%, so it has
      to be latched then and carried until the next cue.
    """

    def __init__(self, *, namespace: str = "eos") -> None:
        self.ns = namespace
        self._active_list: str | None = None
        self._active_number: str | None = None
        self._fade_duration: float | None = None

    def reset(self) -> None:
        """Forget everything latched from a previous console.

        The latched cue number is what turns a bare /eos/out/active/cue fade
        into a cue identity, so carrying it across a switch to a different desk
        would attribute the new console's fades to the old console's cue.
        """
        self._active_list = None
        self._active_number = None
        self._fade_duration = None

    # ------------------------------------------------------------------ entry

    def handle(self, message: OscMessage | OscBundle) -> ParseResult:
        """Translate one message (or every message in a bundle)."""
        result = ParseResult()
        if isinstance(message, OscBundle):
            for element in message.messages():
                sub = self.handle(element)
                result.updates.extend(sub.updates)
                result.events.extend(sub.events)
            return result
        self._dispatch(message, result)
        return result

    def _dispatch(self, message: OscMessage, out: ParseResult) -> None:
        address = message.address
        args = message.args

        # --- cue identity and fade, the part that matters most --------------
        if (match := _ACTIVE_CUE.match(address)) is not None:
            self._handle_active_cue(match, args, out)
            return

        if address == "/eos/out/active/cue":
            self._handle_fade_progress(args, out)
            return

        if address == "/eos/out/active/cue/text":
            self._handle_cue_text("active", args, out)
            return

        if (match := _PENDING_CUE.match(address)) is not None:
            out.updates += [
                BusUpdate(self._key("cue.pending.list"), match.group(1)),
                BusUpdate(self._key("cue.pending.number"), match.group(2)),
            ]
            return

        if address == "/eos/out/pending/cue/text":
            self._handle_cue_text("pending", args, out)
            return

        if (match := _PREVIOUS_CUE.match(address)) is not None:
            out.updates += [
                BusUpdate(self._key("cue.previous.list"), match.group(1)),
                BusUpdate(self._key("cue.previous.number"), match.group(2)),
            ]
            return

        if address == "/eos/out/previous/cue/text":
            self._handle_cue_text("previous", args, out)
            return

        if (match := _CUE_FIRE.match(address)) is not None:
            cue_list, number = match.group(1), match.group(2)
            label = str(args[0]) if args else ""
            out.events.append(
                CueFired(cue_list=cue_list, number=number, label=label)
            )
            # The fire event is the earliest and cleanest statement of which
            # cue is now running: the number is in the address, the label in
            # the argument, no string parsing involved.
            #
            # Publish BOTH, together. Publishing only the label used to leave
            # the number behind until the next /text arrived, and a desk does
            # not send the numbered active-cue address on every change -- over
            # a minute of a running cue list, four cues fired and only one of
            # them produced /eos/out/active/cue/<list>/<number>. The overlay
            # showed the new label beside the old number.
            #
            # The label is published even when empty, which is what stops a
            # cue with no label inheriting the previous cue's. An overlay that
            # captions cue 20.1 as "label 20" is worse than one that captions
            # it as nothing, because it is burned into the recording as fact.
            self._active_list, self._active_number = cue_list, number
            self._fade_duration = None
            out.updates += [
                BusUpdate(self._key("cue.active.list"), cue_list),
                BusUpdate(self._key("cue.active.number"), number),
                BusUpdate(self._key("cue.active.label"), label),
                # The new cue's fade is not described until its first text
                # arrives. Resetting only the internal latch left the PUBLISHED
                # duration holding the previous cue's, which is what the
                # overlay reads -- so clear it here too, and let the first
                # text put the real value back a moment later.
                BusUpdate(self._key("cue.active.duration"), None, FADE_STALE_AFTER),
                BusUpdate(self._key("cue.active.remaining"), None, FADE_STALE_AFTER),
            ]
            return

        # --- command line ---------------------------------------------------
        if address == "/eos/out/cmd":
            self._handle_cmd("cmdline", args, out)
            return

        if (match := _USER_CMD.match(address)) is not None:
            self._handle_cmd(f"cmdline.user.{match.group(1)}", args, out)
            # Also THE command line. When Wer is bound to user 0 -- the default,
            # because a recorder wants every user's traffic -- Eos sends the
            # generic /eos/out/cmd exactly once, at connect, and carries every
            # change after that on the per-user address. A widget reading
            # eos.cmdline.text was therefore frozen on whatever the desk said
            # at the moment of connecting ("LIVE: Cue 1 :") for the whole
            # session. So the generic key is "whichever command line most
            # recently changed, from any user", which is what a recording of a
            # tech wants to show anyway.
            self._handle_cmd("cmdline", args, out)
            return

        # --- everything else ------------------------------------------------
        if address == "/eos/out/show/name":
            out.updates.append(self._scalar("show.name", args))
            return

        if address == "/eos/out/user":
            out.updates.append(self._scalar("user", args))
            return

        if address == "/eos/out/active/chan":
            out.updates.append(self._scalar("chan.active", args, default=""))
            return

        if (match := _SOFTKEY.match(address)) is not None:
            out.updates.append(self._scalar(f"softkey.{match.group(1)}", args, default=""))
            return

        if address == "/eos/out/event/state":
            out.updates.append(self._scalar("event.state", args))
            return

        if address == "/eos/out/event/locked":
            out.updates.append(self._scalar("event.locked", args))
            return

        # Unrecognised addresses are not an error. Eos emits plenty we have no
        # use for (wheel, switch, pantilt, notify/setup), and new firmware will
        # add more. They stay visible in the raw monitor; they just do not
        # become bus keys.

    # -------------------------------------------------------------- handlers

    def _handle_active_cue(
        self, match: re.Match[str], args: tuple[Any, ...], out: ParseResult
    ) -> None:
        """``/eos/out/active/cue/<list>/<number>`` - identity, plus how far the
        fade has got.

        The float argument is the fade's progress at the moment the message is
        sent: 0.0 when the fade is only starting, 1.0 when the cue is already
        complete, and part-way when the fade was under way by the time the
        state went out. How far is not predictable from the cue: in
        tests/fixtures/eos/macro-record-toggle.jsonl two 0.2 s cues arrived at
        0.0 and 0.31, and a 5.0 s one at 0.01, each 125-160 ms after its fire
        event. It does NOT stream; that is the bare address's job.
        """
        cue_list, number = match.group(1), match.group(2)
        changed = (cue_list, number) != (self._active_list, self._active_number)
        self._active_list, self._active_number = cue_list, number

        out.updates += [
            BusUpdate(self._key("cue.active.list"), cue_list),
            BusUpdate(self._key("cue.active.number"), number),
        ]

        if changed:
            # A new cue invalidates the latched duration; it gets re-latched
            # from the next /text at 0%.
            self._fade_duration = None

        if args and isinstance(args[0], (int, float)):
            progress = float(args[0])
            expiry = _fade_expiry(progress)
            out.updates += [
                BusUpdate(self._key("cue.active.progress"), progress, expiry),
                BusUpdate(self._key("cue.active.fading"), progress < 1.0, expiry),
            ]

    def _handle_fade_progress(self, args: tuple[Any, ...], out: ParseResult) -> None:
        """The bare ``/eos/out/active/cue`` - live fade progress at ~1 Hz.

        Published with a stale window because a frozen progress bar reads as a
        working one. The widget interpolates between these; 1 Hz is far too
        coarse to drive a bar at 30 fps directly.
        """
        if not args or not isinstance(args[0], (int, float)):
            return
        progress = float(args[0])
        expiry = _fade_expiry(progress)
        out.updates += [
            BusUpdate(self._key("cue.active.progress"), progress, expiry),
            BusUpdate(self._key("cue.active.fading"), progress < 1.0, expiry),
        ]

    def _handle_cue_text(
        self, which: str, args: tuple[Any, ...], out: ParseResult
    ) -> None:
        text = str(args[0]) if args else ""
        out.updates.append(BusUpdate(self._key(f"cue.{which}.text"), text))

        if not text.strip():
            # Eos sends an EMPTY cue text to say "there is nothing here" --
            # most often for the pending cue once the list has run past its
            # last entry. Returning early on that left the previous cue's
            # number and label on screen indefinitely: "Next 20.1" long after
            # 20.1 had gone, with nothing pending at all.
            #
            # An empty string is information. It has to clear the identity,
            # not be mistaken for silence.
            out.updates += [
                BusUpdate(self._key(f"cue.{which}.list"), ""),
                BusUpdate(self._key(f"cue.{which}.number"), ""),
                BusUpdate(self._key(f"cue.{which}.label"), ""),
            ]
            if which == "active":
                self._active_list = self._active_number = None
                self._fade_duration = None
            return

        parsed = parse_cue_text(text)
        if parsed is None:
            # Non-empty but unparseable. Labels contain spaces and digits, so
            # the boundary is genuinely ambiguous and this is expected now and
            # then. Deliberately NOT cleared: the cue really is there, we just
            # cannot read the fields, and the last known identity is closer to
            # the truth than a blank. The raw text is published above either
            # way, so the text widget still shows exactly what the desk said.
            return

        # Only fill in identity from text when the address has not already said
        # so. The address is authoritative; this is the fallback.
        out.updates += [
            BusUpdate(self._key(f"cue.{which}.list"), parsed["list"]),
            BusUpdate(self._key(f"cue.{which}.number"), parsed["number"]),
        ]
        # Unconditionally, empty included. The number and the label come from
        # the same parse of the same string, so they cannot disagree -- and a
        # cue with no label must clear the last one rather than inherit it.
        out.updates.append(
            BusUpdate(self._key(f"cue.{which}.label"), parsed["label"])
        )

        if which != "active":
            # For pending and previous the time field is the cue's recorded
            # fade time, with no percentage attached.
            out.updates.append(
                BusUpdate(self._key(f"cue.{which}.duration"), parsed["time"])
            )
            return

        percent = parsed.get("percent")
        if percent is None:
            return

        if percent >= 100:
            # At 100% the time field reverts to the cue's recorded time rather
            # than remaining time, so publishing it as "remaining" would make
            # the readout jump upward at the end of every cue.
            #
            # It IS the duration, though, and settling it here is what stops
            # the Cue duration widget reading "Time --s" for most of the show.
            # Every duration published during a fade carries a 3 s window,
            # rightly -- a fade nobody is describing any more is not to be
            # believed. But the last one before completion carried it too, so
            # three seconds after every cue landed the key expired and the
            # overlay burned "--" in for as long as that cue was up. On the two
            # real captures the number was present 48% and 18% of the time; on
            # a settled desk between cues it trends to never. A finished fade
            # is a settled fact, so it gets no expiry, like the pending and
            # previous durations either side of it.
            out.updates += [
                BusUpdate(self._key("cue.active.remaining"), 0.0, None),
                BusUpdate(self._key("cue.active.fading"), False, None),
                BusUpdate(self._key("cue.active.duration"), parsed["time"], None),
            ]
            self._fade_duration = None
            return

        remaining = parsed["time"]
        if percent == 0:
            # At 0% the remaining time IS the full duration. Exact.
            self._fade_duration = remaining
        elif self._fade_duration is None:
            # First sighting of this fade is already mid-flight, which is
            # ordinary rather than rare: the fire event leads the state update
            # by 50-100 ms, so any fade under about three seconds is first
            # reported at a non-zero percent. Waiting for a 0% that will never
            # come used to leave this key holding the PREVIOUS cue's duration
            # -- 2.9 s burned in over a cue the desk said was 2.0 s.
            #
            # It is recoverable, so recover it rather than blanking it. The
            # desk reports remaining time and percent complete, and those give
            # the whole: at 3% with 1.9 s left, 1.9 / 0.97 = 1.96; at 54% with
            # 0.9 s left, 0.9 / 0.46 = 1.96. Both land on the same answer,
            # against a true 2.0.
            self._fade_duration = round(remaining / (1.0 - percent / 100.0), 1)

        out.updates.append(
            BusUpdate(self._key("cue.active.remaining"), remaining, FADE_STALE_AFTER)
        )
        # Published every time, never conditionally. A conditional publish is
        # what let one cue's duration survive into the next.
        out.updates.append(
            BusUpdate(
                self._key("cue.active.duration"),
                self._fade_duration,
                FADE_STALE_AFTER,
            )
        )

    def _handle_cmd(self, prefix: str, args: tuple[Any, ...], out: ParseResult) -> None:
        """Command line. The int argument is the error flag.

        Never detect errors by string-matching the text: the captures showed
        "Error :", "Error:" and "- Error:" for different error classes. The
        integer is consistent and is what it is for.
        """
        text = str(args[0]) if args else ""
        out.updates.append(BusUpdate(self._key(f"{prefix}.text"), text))
        if len(args) > 1 and isinstance(args[1], int):
            out.updates.append(BusUpdate(self._key(f"{prefix}.error"), bool(args[1])))

    # --------------------------------------------------------------- helpers

    def _key(self, suffix: str) -> str:
        return f"{self.ns}.{suffix}"

    def _scalar(
        self, suffix: str, args: tuple[Any, ...], default: Any = None
    ) -> BusUpdate:
        return BusUpdate(self._key(suffix), args[0] if args else default)
