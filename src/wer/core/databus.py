"""The DataBus: the one object between connections and widgets.

Connections publish namespaced dotted keys; widgets subscribe by exact key or
glob. Neither imports the other. Three things fall out of this for free and all
three are wanted: the raw data monitor, template binding, and the sidecar log.

No Qt. Connections run on their own threads and publish from them, so this is
locked internally; the UI adapter is responsible for marshalling callbacks onto
the main thread.

Threading contract
------------------
``publish`` may be called from any thread. Subscriber callbacks run **on the
publishing thread**, not on the main thread, and not under the lock. Anything
that touches a Qt widget must therefore hop threads itself - see
``wer.ui.bus_bridge``. Running callbacks outside the lock is what lets a
callback publish again without deadlocking; the cost is that a subscriber can
observe a value that has already been superseded, which for a display bus is
the right trade.
"""

from __future__ import annotations

import fnmatch
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "BusEntry",
    "DataBus",
    "Subscription",
    "MISSING",
    "TEMPLATE_PATTERN",
]

#: Rendered in place of a key that has never been published. Deliberately not
#: an empty string: "Cue " with a blank where the number should be looks like a
#: formatting bug, whereas a visible marker looks like missing data.
MISSING = "--"

#: ``{some.bus.key}`` inside a template string. Keys are dotted lowercase
#: identifiers; anything else is left alone so literal braces survive.
TEMPLATE_PATTERN = re.compile(r"\{([a-zA-Z0-9_][a-zA-Z0-9_.*\-]*)\}")

BusCallback = Callable[["BusEntry"], None]


@dataclass(frozen=True, slots=True)
class BusEntry:
    """One value on the bus, with everything needed to judge whether to trust it."""

    key: str
    value: Any
    #: Python type name at publish time. The monitor shows it, and it catches
    #: the case where a connection starts sending strings where it sent floats.
    type_name: str
    #: ``time.perf_counter()``, not wall clock: age arithmetic must not jump
    #: when the system clock is corrected mid-show. perf_counter rather than
    #: monotonic for two reasons - captured frames are stamped with
    #: perf_counter, and the sidecar log has to line bus changes up against
    #: those frame timestamps, which only works on one clock; and monotonic has
    #: ~15.6 ms granularity on Windows, too coarse to order events inside a
    #: single frame.
    updated_at: float
    source_connection_id: str
    #: Seconds after which this value should no longer be believed. ``None``
    #: means it never goes stale (a show name does not expire; a cue fade does).
    stale_after: float | None = None

    def age(self, now: float | None = None) -> float:
        """Seconds since this value was last published."""
        return (time.perf_counter() if now is None else now) - self.updated_at

    def is_stale(self, now: float | None = None) -> bool:
        if self.stale_after is None:
            return False
        return self.age(now) > self.stale_after


@dataclass(frozen=True, slots=True)
class Subscription:
    """Handle returned by :meth:`DataBus.subscribe`. Cancel via :meth:`cancel`."""

    pattern: str
    callback: BusCallback
    _bus: "DataBus"
    _token: int

    def cancel(self) -> None:
        self._bus.unsubscribe(self)


class DataBus:
    """Central store of every value any connection has published."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, BusEntry] = {}
        # Exact-key and glob subscriptions are kept apart so the common case -
        # a text widget bound to two specific keys - does not walk every glob
        # on every publish. Eos alone emits tens of keys per second.
        self._exact: dict[str, list[Subscription]] = {}
        self._globs: list[Subscription] = []
        self._next_token = 0
        self._change_listeners: list[Callable[[BusEntry], None]] = []
        #: How many times keys have been taken off the bus; see removals.
        self._removals = 0

    # ------------------------------------------------------------- publishing

    def publish(
        self,
        key: str,
        value: Any,
        *,
        source_connection_id: str,
        stale_after: float | None = None,
        force: bool = False,
    ) -> BusEntry:
        """Publish a value and notify subscribers.

        Unchanged values still refresh ``updated_at`` - that is what keeps a
        widget from going stale while a connection is healthily repeating
        itself - but they do **not** fire callbacks unless ``force`` is set.
        Eos re-sends its whole state on every change, so firing on every
        repeat would mean re-rendering every widget several times a second for
        no visible difference.

        The exception is a value coming back after it had expired. Everything
        that reads the bus shows an expired key as missing, so the same value
        returning is as much a change on screen as a new one: "--" becomes
        "40.0s" again. The overlay redraws a widget when a key it reads changes
        or expires, and on nothing else, so a desk that dropped mid-fade and
        came back re-sending the same fade time would have left "--" drawn into
        the recording until the value next changed. A healthy repeat arrives
        well inside its window, so this fires only on a genuine return.
        """
        with self._lock:
            # Stamped under the lock, not before taking it, because the test
            # below judges the previous entry's expiry at this moment. Stamped
            # first, a connection thread held up on the way in -- a thread
            # switch, another thread holding the lock -- judged it at a moment
            # already behind the capture thread. A frame drawn in that gap could
            # show the value as missing and, finding nothing left to wait for,
            # stop watching the clock; the value then came back unchanged,
            # looked unexpired at the early stamp, and nobody was told. "--"
            # stayed on the picture until the value next changed. Under the
            # lock, whoever read the old entry read it before this stamp, and an
            # entry that had expired by then has expired by now as well.
            entry = BusEntry(
                key=key,
                value=value,
                type_name=type(value).__name__,
                updated_at=time.perf_counter(),
                source_connection_id=source_connection_id,
                stale_after=stale_after,
            )
            previous = self._entries.get(key)
            self._entries[key] = entry
            changed = (
                force
                or previous is None
                or previous.value != value
                or previous.is_stale(entry.updated_at)
            )
            if not changed:
                return entry
            targets = list(self._exact.get(key, ()))
            targets += [s for s in self._globs if _matches(s.pattern, key)]
            listeners = list(self._change_listeners)

        # Outside the lock, deliberately - see the module docstring.
        for subscription in targets:
            _safe_call(subscription.callback, entry)
        for listener in listeners:
            _safe_call(listener, entry)
        return entry

    def publish_many(
        self,
        values: Mapping[str, Any],
        *,
        source_connection_id: str,
        stale_after: float | None = None,
    ) -> None:
        """Publish a batch. Convenience only; ordering is the mapping's."""
        for key, value in values.items():
            self.publish(
                key,
                value,
                source_connection_id=source_connection_id,
                stale_after=stale_after,
            )

    # --------------------------------------------------------------- reading

    def get(self, key: str) -> BusEntry | None:
        with self._lock:
            return self._entries.get(key)

    def value(self, key: str, default: Any = None, *, allow_stale: bool = False) -> Any:
        """The current value, or ``default`` if there is none or it has expired.

        Staleness is honoured by default, and that is the point. ``render()``
        has always substituted the missing-value marker for an expired key, so
        a template showed "--" while a widget reading the same key through
        this method got the expired number and drew it as live. The fade
        progress bar did exactly that: it kept interpolating against a
        duration the text beside it had already given up on.

        ``allow_stale=True`` asks for the last known value regardless, for the
        rare caller that genuinely wants it. Note that a key published with no
        expiry never goes stale, so this changes nothing for most of the bus.
        """
        entry = self.get(key)
        if entry is None:
            return default
        if not allow_stale and entry.is_stale():
            return default
        return entry.value

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._entries)

    def match(self, pattern: str) -> list[BusEntry]:
        """Every entry whose key matches a glob, sorted by key."""
        with self._lock:
            return sorted(
                (e for k, e in self._entries.items() if _matches(pattern, k)),
                key=lambda e: e.key,
            )

    def snapshot(self) -> list[BusEntry]:
        """Every entry, sorted by key. Backs the raw data monitor."""
        with self._lock:
            return sorted(self._entries.values(), key=lambda e: e.key)

    def is_stale(self, key: str) -> bool:
        """True if the key is missing or its value is past ``stale_after``."""
        entry = self.get(key)
        return entry is None or entry.is_stale()

    # ----------------------------------------------------------- subscribing

    def subscribe(self, pattern: str, callback: BusCallback) -> Subscription:
        """Subscribe to an exact key or a glob such as ``sacn.1.ch.*``.

        The callback fires on change only, on the publishing thread.
        """
        with self._lock:
            self._next_token += 1
            subscription = Subscription(pattern, callback, self, self._next_token)
            if _is_glob(pattern):
                self._globs.append(subscription)
            else:
                self._exact.setdefault(pattern, []).append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            if _is_glob(subscription.pattern):
                self._globs = [s for s in self._globs if s._token != subscription._token]
            else:
                bucket = self._exact.get(subscription.pattern)
                if bucket:
                    remaining = [s for s in bucket if s._token != subscription._token]
                    if remaining:
                        self._exact[subscription.pattern] = remaining
                    else:
                        del self._exact[subscription.pattern]

    def add_change_listener(self, listener: Callable[[BusEntry], None]) -> None:
        """Listen to *every* change regardless of key.

        This is what the sidecar log and the raw monitor use. Kept separate
        from ``subscribe`` so a firehose listener is never mistaken for a
        widget binding.
        """
        with self._lock:
            self._change_listeners.append(listener)

    def remove_change_listener(self, listener: Callable[[BusEntry], None]) -> None:
        with self._lock:
            if listener in self._change_listeners:
                self._change_listeners.remove(listener)

    # ------------------------------------------------------------- lifecycle

    @property
    def removals(self) -> int:
        """How many times keys have been taken off the bus, by clear_source or
        clear.

        Taking a key off notifies nobody. Subscribers are handed the entry that
        changed, and a removed key has none; announcing it some other way would
        also reach the sidecar log and the data monitor, as a row for every key
        a console switch clears. But anything that caches what it drew has to
        find out, or it goes on drawing a value the bus no longer holds -- so
        it watches this count instead. See Compositor.composite.
        """
        with self._lock:
            return self._removals

    def clear_source(self, source_connection_id: str) -> list[str]:
        """Forget everything a connection published. Returns the removed keys.

        Used when a connection is deleted, not when it merely drops: a dropped
        connection's last known values stay on the bus and go stale, which is
        what lets a widget say "stale" rather than blanking mid-show.

        Nobody is notified. The removal is counted instead; see removals.
        """
        with self._lock:
            removed = [
                key
                for key, entry in self._entries.items()
                if entry.source_connection_id == source_connection_id
            ]
            for key in removed:
                del self._entries[key]
            if removed:
                self._removals += 1
        return removed

    def touch_source(self, source_connection_id: str) -> int:
        """Refresh ``updated_at`` for one connection's entries without changing
        values, to record that the connection is alive and still asserting them.

        Returns how many entries were touched.

        It notifies nobody, even for an entry it brings back from expiry -- and
        that is a trap for anything that caches what it drew. A widget showing
        one of those keys as missing is not told to redraw, and goes on showing
        "--" over a value the bus believes again. Nothing calls this today. A
        caller reviving expired values should publish them, which does notify
        (see publish).
        """
        now = time.perf_counter()
        with self._lock:
            touched = 0
            for key, entry in self._entries.items():
                if entry.source_connection_id == source_connection_id:
                    self._entries[key] = replace(entry, updated_at=now)
                    touched += 1
        return touched

    def clear(self) -> None:
        """Forget everything. Notifies nobody, like clear_source; counted in
        removals the same way."""
        with self._lock:
            if self._entries:
                self._entries.clear()
                self._removals += 1

    # -------------------------------------------------------------- templates

    def render(self, template: str, missing: str = MISSING) -> str:
        """Resolve ``Cue {eos.cue.active.number}`` against the bus.

        A key that is missing *or stale* renders as ``missing``. The rule is
        explicit: a widget must visibly say so rather than showing a stale
        value, and silently keeping the last-known number on screen during a
        console dropout is exactly the failure this app exists to avoid.
        """

        def substitute(match: re.Match[str]) -> str:
            entry = self.get(match.group(1))
            if entry is None or entry.is_stale():
                return missing
            return "" if entry.value is None else str(entry.value)

        return TEMPLATE_PATTERN.sub(substitute, template)

    @staticmethod
    def template_keys(template: str) -> list[str]:
        """Keys referenced by a template, so a widget knows what to subscribe to."""
        seen: dict[str, None] = {}
        for match in TEMPLATE_PATTERN.finditer(template):
            seen.setdefault(match.group(1), None)
        return list(seen)

    def __iter__(self) -> Iterator[BusEntry]:
        return iter(self.snapshot())


# --------------------------------------------------------------------- helpers


def _is_glob(pattern: str) -> bool:
    return any(character in pattern for character in "*?[")


def _matches(pattern: str, key: str) -> bool:
    # fnmatchcase, not fnmatch: fnmatch normalises case on Windows, and bus
    # keys are case-sensitive by design.
    return fnmatch.fnmatchcase(key, pattern)


def _safe_call(callback: Callable[[BusEntry], None], entry: BusEntry) -> None:
    """Invoke a subscriber without letting it take down the publisher.

    A widget that throws must not kill the connection thread feeding it
    (no silent failures, but also no cascading ones).
    """
    try:
        callback(entry)
    except Exception:  # noqa: BLE001 - deliberately broad, this is a boundary
        import logging

        logging.getLogger(__name__).exception(
            "DataBus subscriber raised while handling %s", entry.key
        )


def keys_under(bus: DataBus, prefix: str) -> Iterable[BusEntry]:
    """All entries under a dotted prefix, e.g. ``eos`` or ``sacn.1``."""
    return bus.match(prefix.rstrip(".") + ".*")
