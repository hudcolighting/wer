"""The only sanctioned crossing from worker threads into Qt.

The rule: Qt UI on the main thread only. Never touch a widget from a worker
thread. DataBus callbacks run on whichever thread published -- a connection
thread, always. So something has to carry them across, and this is it. Nothing
else in the UI may subscribe to the bus directly.

Why it coalesces
----------------
A naive bridge emits one Qt signal per bus change. That is fine for a clock and
catastrophic for anything else:

- Eos publishes its command line on **every keystroke**, and re-dumps ~30 keys
  whenever a cue changes.
- A per-channel level feed would be worse again: one sACN universe is 512
  keys at up to 44 Hz, which is 22,000 signal emissions a second, each one
  a queued cross-thread event allocation.

So changes are accumulated into a dict keyed by bus key -- later values simply
overwrite earlier ones, which is exactly right for a display -- and flushed on a
timer running on the main thread. The UI sees the newest value of everything
that changed, a few times a second, which is as often as a human can read.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QTimer, Signal

from wer.core.databus import BusEntry, DataBus

log = logging.getLogger(__name__)

__all__ = ["BusBridge", "DEFAULT_FLUSH_MS"]

#: 20 Hz. Faster than a person can read a changing number, slower than the
#: frame rate, and cheap.
DEFAULT_FLUSH_MS = 50


class BusBridge(QObject):
    """Carries DataBus changes onto the Qt main thread, coalesced.

    Must be constructed on the main thread. ``batch`` carries a dict of
    ``{key: BusEntry}`` holding the newest value of everything that changed
    since the last flush.
    """

    #: Emitted on the main thread with {key: BusEntry} for the interval.
    batch = Signal(dict)

    def __init__(self, bus: DataBus, *, flush_ms: int = DEFAULT_FLUSH_MS,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.bus = bus
        self._pending: dict[str, BusEntry] = {}
        # A plain lock, not a Qt construct: this is contended from connection
        # threads that must never touch Qt at all.
        self._lock = threading.Lock()
        self._dropped_since_flush = 0

        self._timer = QTimer(self)
        self._timer.setInterval(flush_ms)
        self._timer.timeout.connect(self._flush)

        bus.add_change_listener(self._on_bus_change)

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.bus.remove_change_listener(self._on_bus_change)

    # Called on a CONNECTION thread. Must not touch Qt objects, must not block,
    # and must not raise - a throwing listener would otherwise propagate into
    # the connection's publish call.
    def _on_bus_change(self, entry: BusEntry) -> None:
        with self._lock:
            if entry.key in self._pending:
                self._dropped_since_flush += 1
            self._pending[entry.key] = entry

    # Runs on the main thread, driven by the timer.
    def _flush(self) -> None:
        with self._lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = {}
            self._dropped_since_flush = 0
        self.batch.emit(batch)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)
