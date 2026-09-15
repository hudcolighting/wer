"""Writes every DataBus change to disk during a recording.

No Qt.

``<name>.bus.jsonl`` holds one JSON object per bus change, timestamped relative
to the start of the recording. This matters more than it looks: it means the
recording is *re-renderable*. The video has the overlay burnt in, but the data
behind that overlay is preserved exactly, so a different overlay can be burnt
onto the same footage later without re-running a tech.

We do not build that re-render in v1, but we design for it anyway, and the
cost of doing so is this file.

Threading
---------
Bus changes arrive on connection threads, which must never block on disk. So
writes go through a queue to a dedicated thread. If the queue ever fills -- a
stalled disk, say -- changes are counted and dropped rather than allowed to
apply backpressure to a console connection. Losing a line of the log is a small
harm; stalling the Eos thread during a tech is a large one.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any

from wer.core.databus import BusEntry, DataBus

log = logging.getLogger(__name__)

__all__ = ["BusLogger"]

#: Roughly ten seconds of a busy Eos command line. Deep enough to ride out a
#: disk hiccup, shallow enough that a genuinely stuck disk is noticed.
QUEUE_DEPTH = 2000


class BusLogger:
    """Records bus changes to a JSONL file for the duration of a take."""

    def __init__(self, bus: DataBus) -> None:
        self.bus = bus
        self.path: Path | None = None
        self.lines_written = 0
        self.lines_dropped = 0

        self._queue: queue.Queue[tuple[float, BusEntry] | None] = queue.Queue(
            maxsize=QUEUE_DEPTH
        )
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self._active = False

    @property
    def is_active(self) -> bool:
        return self._active

    def start(self, path: Path, started_at: float) -> bool:
        """Begin logging. ``started_at`` is the recording's perf_counter origin."""
        if self._active:
            return False

        self.path = path
        self.lines_written = 0
        self.lines_dropped = 0
        self._started_at = started_at

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("w", encoding="utf-8", newline="\n")
        except OSError:
            log.exception("Could not open bus log %s", path)
            self.path = None
            return False

        self._active = True
        self._thread = threading.Thread(
            target=self._run, args=(handle,), name="bus-log", daemon=True
        )
        self._thread.start()

        # A header line, so the file is self-describing months later when
        # nobody remembers what produced it.
        self._write_direct({
            "type": "header",
            "t": 0.0,
            "wall_clock": time.time(),
            "note": "Wer bus log: one bus change per line, t is seconds from "
                    "the start of the recording",
        })

        # Everything already on the bus, so the log starts from a complete
        # picture rather than from whatever happened to change next. Without
        # this a re-render would have no show name until someone retyped it.
        for entry in bus_snapshot(self.bus):
            self._enqueue(entry)

        self.bus.add_change_listener(self._on_change)
        log.info("Bus log started: %s", path.name)
        return True

    def stop(self) -> Path | None:
        """Finish logging and flush. Returns the file path."""
        if not self._active:
            return None
        self._active = False
        self.bus.remove_change_listener(self._on_change)

        try:
            self._queue.put(None, timeout=5.0)
        except queue.Full:
            log.error("Bus log queue full while stopping; the tail may be lost")

        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None

        log.info(
            "Bus log finished: %d lines%s",
            self.lines_written,
            f", {self.lines_dropped} dropped" if self.lines_dropped else "",
        )
        return self.path

    # Called on connection threads. Must not block and must not raise.
    def _on_change(self, entry: BusEntry) -> None:
        if self._active:
            self._enqueue(entry)

    def _enqueue(self, entry: BusEntry) -> None:
        try:
            self._queue.put_nowait((time.perf_counter(), entry))
        except queue.Full:
            self.lines_dropped += 1
            if self.lines_dropped in (1, 100, 1000):
                log.error(
                    "Bus log queue full; %d change(s) dropped. The disk may be "
                    "struggling.", self.lines_dropped,
                )

    def _run(self, handle) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                received_at, entry = item
                handle.write(
                    json.dumps(
                        {
                            "t": round(received_at - self._started_at, 4),
                            "key": entry.key,
                            "value": _json_safe(entry.value),
                            "type": entry.type_name,
                            "source": entry.source_connection_id,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                self.lines_written += 1
        except Exception:  # noqa: BLE001 - a logging failure must not kill a take
            log.exception("Bus log writer failed; recording continues")
        finally:
            try:
                handle.flush()
                handle.close()
            except OSError:
                log.exception("Could not close the bus log cleanly")

    def _write_direct(self, payload: dict[str, Any]) -> None:
        """Header only, before the queue has anything in it."""
        try:
            self._queue.put_nowait((self._started_at, _HeaderEntry(payload)))
        except queue.Full:
            pass


class _HeaderEntry:
    """Adapts a plain dict to the BusEntry shape the writer expects."""

    __slots__ = ("key", "value", "type_name", "source_connection_id")

    def __init__(self, payload: dict[str, Any]) -> None:
        self.key = "__header__"
        self.value = payload
        self.type_name = "header"
        self.source_connection_id = "wer"


def bus_snapshot(bus: DataBus) -> list[BusEntry]:
    return bus.snapshot()


def _json_safe(value: Any) -> Any:
    """Make a bus value representable in JSON without losing information."""
    if isinstance(value, (str, int, bool, type(None))):
        return value
    if isinstance(value, float):
        # NaN and infinity are not valid JSON; json.dumps emits them anyway and
        # strict parsers then reject the file.
        if value != value or value in (float("inf"), float("-inf")):
            return {"__float__": repr(value)}
        return value
    if isinstance(value, bytes):
        return {"__blob__": value.hex()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)
