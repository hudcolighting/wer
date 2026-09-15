"""The two connections that need no hardware.

**System** -- wall clock, date, record elapsed, take counter.
**Manual** -- operator-typed fields: show name, act, scene, take, note.

Both are "virtual devices": they produce data, they publish to the bus, and
widgets bind to them exactly as they would to a console. That symmetry is the
point of the DataBus, and it is why a clock widget needs no special case
anywhere.

No Qt.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from wer.connections.base import Connection, ConnectionState
from wer.core.databus import DataBus

log = logging.getLogger(__name__)

__all__ = ["SystemConnection", "ManualConnection", "format_duration"]


def format_duration(seconds: float, *, show_hours: bool = True) -> str:
    """H:MM:SS or M:SS. Negative clamps to zero rather than showing a minus."""
    seconds = max(0.0, seconds)
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if show_hours or hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class SystemConnection(Connection):
    """Clock, date, and record elapsed.

    Ticks four times a second. A seconds display needs faster than 1 Hz or it
    visibly stutters against the video, but faster than 4 Hz just burns CPU for
    a value that only changes once a second.
    """

    stale_after = 5.0
    #: There is nothing to reconnect to.
    auto_reconnect = False

    TICK = 0.25

    def __init__(self, connection_id: str, bus: DataBus, *, namespace: str = "clock") -> None:
        super().__init__(connection_id, bus)
        self.ns = namespace
        self._record_started: float | None = None
        self._take = 1

    @property
    def display_name(self) -> str:
        return "System (clock, elapsed, take)"

    # ------------------------------------------------------ recording state

    def start_recording(self) -> None:
        self._record_started = time.perf_counter()

    def stop_recording(self) -> None:
        self._record_started = None
        self._take += 1

    @property
    def take(self) -> int:
        return self._take

    @take.setter
    def take(self, value: int) -> None:
        self._take = max(1, int(value))

    @property
    def record_elapsed(self) -> float:
        if self._record_started is None:
            return 0.0
        return time.perf_counter() - self._record_started

    # ------------------------------------------------------------- pumping

    def _run_once(self) -> None:
        self._set_state(ConnectionState.LIVE)
        while not self._stop.is_set():
            now = datetime.now()
            elapsed = self.record_elapsed

            self.publish(f"{self.ns}.wall", now.strftime("%H:%M:%S"), stale_after=2.0)
            self.publish(f"{self.ns}.wall12", now.strftime("%I:%M:%S %p").lstrip("0"),
                         stale_after=2.0)
            self.publish(f"{self.ns}.date", now.strftime("%Y-%m-%d"), stale_after=None)
            self.publish(f"{self.ns}.date_long", now.strftime("%A %d %B %Y"),
                         stale_after=None)
            self.publish(f"{self.ns}.elapsed", format_duration(elapsed), stale_after=2.0)
            self.publish(f"{self.ns}.elapsed_seconds", round(elapsed, 2), stale_after=2.0)
            self.publish(f"{self.ns}.recording", self._record_started is not None,
                         stale_after=2.0)
            self.publish(f"{self.ns}.take", self._take, stale_after=None)

            # note_packet keeps pkt/s and "time since last packet" meaningful
            # for this connection too, so the status panel is uniform.
            self.status.note_packet()

            if self._stop.wait(self.TICK):
                break

    def _wake(self) -> None:
        pass  # _stop.wait() already returns immediately


class ManualConnection(Connection):
    """Operator-typed fields, editable while recording.

    Not threaded: there is nothing to poll. Values are pushed in from the UI and
    republished so that a widget bound to ``manual.act`` behaves identically to
    one bound to ``eos.cue.active.number``.
    """

    auto_reconnect = False

    #: Offered in the UI as a starting point. Any key may be added; these are
    #: simply the ones a tech recording usually wants.
    DEFAULT_FIELDS = ("show", "act", "scene", "take", "note", "designer", "venue")

    def __init__(self, connection_id: str, bus: DataBus, *, namespace: str = "manual") -> None:
        super().__init__(connection_id, bus)
        self.ns = namespace
        self._fields: dict[str, str] = {}
        self._fields_lock = threading.RLock()

    @property
    def display_name(self) -> str:
        return "Manual (typed fields)"

    def _run_once(self) -> None:
        """Nothing to pump. Stay live until stopped."""
        self._set_state(ConnectionState.LIVE)
        self.status.note_packet()
        self._republish_all()
        self._stop.wait()

    def set_field(self, name: str, value: str) -> None:
        """Set one field. Safe to call from the UI thread while recording."""
        name = name.strip().lower().replace(" ", "_")
        if not name:
            return
        with self._fields_lock:
            self._fields[name] = value
        self.publish(f"{self.ns}.{name}", value)
        self.status.note_packet()
        self.record(f"{name} = {value!r}", direction="tx")
        if self.status.state is not ConnectionState.LIVE:
            self._set_state(ConnectionState.LIVE)

    def clear_field(self, name: str) -> None:
        with self._fields_lock:
            self._fields.pop(name, None)
        # Publish empty rather than removing the key: a widget bound to it
        # should show "nothing typed", not fall back to a missing-key marker
        # that looks like a fault.
        self.publish(f"{self.ns}.{name}", "")

    def fields(self) -> dict[str, str]:
        with self._fields_lock:
            return dict(self._fields)

    def _republish_all(self) -> None:
        for name, value in self.fields().items():
            self.publish(f"{self.ns}.{name}", value)

    def _wake(self) -> None:
        pass
