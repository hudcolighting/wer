"""Commands a console macro sends to Wer: remote control over OSC.

No Qt, and no socket of its own. A macro on the Eos desk sends an OSC string
such as ``/wer/record/toggle``, and it arrives on the link Wer already holds to
that desk. The Eos connection hands every ``/wer/`` message to a
CommandDispatcher (``EosConnection.commands``), and the window registers what
each one does.

Measured on 13 Sep 2026 against Eos running on the development laptop, over
TCP 3032 (tests/fixtures/eos/macro-record-toggle.jsonl): each run of the macro
sent the console's own ``/eos/out/event/macro/1`` and, about 25 ms later,
``/wer/record/toggle``, with no arguments.

This replaced a UDP listener with a port and a tab of its own. The desk never
reached it: its commands came down the Eos link instead, and scrolled past in
that tab's monitor as somebody else's traffic while nothing acted on them. Only
the console drives Wer, so the one link carries both.

Wer still never drives the console. Nothing here sends anything to a desk.

Repeats
-------
A macro fires once per press, and a fast tapper presses often: the capture
holds twenty runs in 4.5 seconds. Acting on each would start a take and stop it
again within a fifth of a second, over and over. So a start, stop or toggle
that arrives within REPEAT_QUIET_SECONDS of the same command is ignored, and
every ignored repeat restarts the wait: a burst counts as one press, however
long it goes on. Different commands never fold into each other, because a
macro that sends stop and then start must still do both.

Button releases
---------------
A controller that sends ``1`` on press and ``0`` on release would act twice per
gesture, so a first argument of zero is taken as a release and ignored. Eos sent
no arguments at all in the capture; this is a guard, not something the console
was seen to need.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from wer.connections.osc import OscMessage

log = logging.getLogger(__name__)

__all__ = [
    "COMMANDS",
    "COMMAND_PREFIX",
    "REPEAT_QUIET_SECONDS",
    "SINGLE_PRESS",
    "UNKNOWN_ADDRESSES_SAID",
    "CommandDispatcher",
    "OscCommand",
    "is_button_release",
]

#: Every command lives under this. The Eos connection hands anything starting
#: with it to the dispatcher instead of treating it as another app's traffic.
COMMAND_PREFIX = "/wer/"

#: How long the console must be quiet before the same record command counts
#: again. Chosen by Hudson on 13 Sep 2026 after the capture above.
REPEAT_QUIET_SECONDS = 1.0

#: How many different unknown addresses get a log line of their own. A typo in
#: a macro is one address pressed many times, and each typo is said once; past
#: this many different ones something is sending rubbish, and only a running
#: count is logged.
UNKNOWN_ADDRESSES_SAID = 20


@dataclass(frozen=True)
class OscCommand:
    """One address Wer answers to."""

    address: str
    summary: str
    #: What the arguments mean, if any.
    arguments: str = ""


#: The published command set. Also used to generate the help page, so the two
#: cannot drift apart.
COMMANDS: tuple[OscCommand, ...] = (
    OscCommand("/wer/record/start", "Start a recording"),
    OscCommand("/wer/record/stop", "Stop a recording"),
    OscCommand("/wer/record/toggle", "Toggle the recording state"),
    OscCommand("/wer/record/snapshot", "Capture a still frame"),
    OscCommand(
        "/wer/marker", "Drop a marker at the current point",
        "optional string: a note for the marker",
    ),
    OscCommand(
        "/wer/layout", "Switch to a named layout",
        "string: the layout name, e.g. Minimal",
    ),
    OscCommand("/wer/layout/next", "Switch to the next layout"),
    OscCommand("/wer/overlay/hide", "Hide the overlay"),
    OscCommand("/wer/overlay/show", "Show the overlay again"),
)

#: The commands a burst of repeats folds into one press. Starting and stopping
#: are the ones where a second press undoes the first. A burst of markers or
#: snapshots leaves one for every press, which is what was asked for, and next
#: layout is meant to be pressed repeatedly.
SINGLE_PRESS = frozenset(
    {"/wer/record/start", "/wer/record/stop", "/wer/record/toggle"}
)


class CommandDispatcher:
    """Hands each command to its handler, on the thread that received it.

    That is the Eos connection's thread. Anything that touches Qt must marshal;
    the window does it by routing every handler through a queued signal.
    """

    def __init__(
        self,
        *,
        quiet_seconds: float = REPEAT_QUIET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.handlers: dict[str, Callable[[OscMessage], None]] = {}
        self.unknown_count = 0
        self.repeats_ignored = 0
        self.quiet_seconds = quiet_seconds
        self._clock = clock
        #: When each single-press command last arrived, acted on or not.
        self._last_arrival: dict[str, float] = {}
        #: Repeats ignored in the burst still going on, per command, so the log
        #: says a burst began once rather than once per tap.
        self._burst: dict[str, int] = {}
        #: How often each unknown address has arrived; see _note_unknown.
        self._unknown: dict[str, int] = {}

    def on(self, address: str, handler: Callable[[OscMessage], None]) -> None:
        """Register a handler for an address."""
        self.handlers[address] = handler

    def dispatch(self, message: OscMessage, source: str = "the console") -> bool:
        """Act on one command. True when a handler ran without raising."""
        if is_button_release(message):
            log.debug("Ignoring button release for %s", message.address)
            return False
        if self._is_repeat(message.address):
            return False

        handler = self.handlers.get(message.address)
        if handler is None:
            self._note_unknown(message.address, source)
            return False

        log.info("Command from %s: %s", source, message)
        try:
            handler(message)
        except Exception:  # noqa: BLE001 - a bad handler must not take the link down
            log.exception("The handler for %s failed", message.address)
            return False
        return True

    def _note_unknown(self, address: str, source: str) -> None:
        """Log an address that is not a command: once per address, then counted.

        Per address, not one allowance for the night. With a single counter, a
        typo pressed five times in the tech used up the lines, and a second
        typo at the interval arrived with nothing in the log at all.
        """
        self.unknown_count += 1
        seen = self._unknown.get(address)
        if seen is not None:
            self._unknown[address] = seen + 1
            if (seen + 1) % 500 == 0:
                log.info(
                    "%s from %s is still not a Wer command [%d times]",
                    address, source, seen + 1,
                )
            return
        if len(self._unknown) < UNKNOWN_ADDRESSES_SAID:
            self._unknown[address] = 1
            log.info("Ignoring %s from %s: it is not a Wer command", address, source)
        elif self.unknown_count % 500 == 0:
            log.info(
                "Still ignoring addresses from %s that are not Wer commands "
                "[%d so far]", source, self.unknown_count,
            )

    def _is_repeat(self, address: str) -> bool:
        """Whether this is the same record command again inside the quiet time.

        Every arrival restarts the time, ignored ones included, so a burst of
        any length is one press.
        """
        if address not in SINGLE_PRESS:
            return False
        now = self._clock()
        last = self._last_arrival.get(address)
        self._last_arrival[address] = now
        if last is None or now - last >= self.quiet_seconds:
            self._burst[address] = 0
            return False
        self.repeats_ignored += 1
        self._burst[address] = self._burst.get(address, 0) + 1
        if self._burst[address] == 1:
            log.info(
                "Ignoring %s again %.2f s after the last one: a burst of presses "
                "counts as one. Leave %g s between presses that are meant "
                "separately.",
                address, now - last, self.quiet_seconds,
            )
        return True


def is_button_release(message: OscMessage) -> bool:
    """True for the ``0`` half of a controller's press/release pair."""
    if not message.args:
        return False
    first = message.args[0]
    if isinstance(first, bool):
        return first is False
    if isinstance(first, (int, float)):
        return float(first) == 0.0
    return False
