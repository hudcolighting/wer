"""Connection framework: threading, status, reconnect, and the monitor buffer.

No Qt. A connection's only job is to publish to the DataBus; it never knows a
widget exists. Everything here runs on the connection's own thread and never
blocks the UI or the frame pipeline.

Why the status model has more than four states
----------------------------------------------
The plan was Disconnected / Connecting / Live / Stale. Capturing real Eos
traffic turned up a case those four cannot express: **Eos accepts a TCP
connection and sends nothing at all when its OSC output is disabled.** The
socket is open, there is no error, and no data will ever arrive. Reporting that
as `Live` is a lie, and reporting it as `Disconnected` is also a lie -- the user
would go hunting for a network fault when the actual problem is one checkbox on
the console.

So there is a fifth state, ``WAITING``: connected, but nothing has been heard
yet. It is the state that tells the user to go and look at the desk.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from wer.core.databus import DataBus

log = logging.getLogger(__name__)

__all__ = [
    "ConnectionState",
    "ConnectionStatus",
    "MonitorEntry",
    "Connection",
    "BACKOFF_SCHEDULE",
]

#: Reconnect delays in seconds. Fast at first so a momentary blip recovers
#: invisibly, then backing off so a console that is genuinely off does not get
#: hammered for four hours. The last value repeats.
#:
#: The rule: a console reboot mid-show must not require restarting the app.
#: An Eos reboot takes a couple of minutes, so the schedule has to stay
#: retrying well past that.
BACKOFF_SCHEDULE = (0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0)


class ConnectionState(str, Enum):
    DISCONNECTED = "Disconnected"
    CONNECTING = "Connecting"
    #: Transport is up but nothing has been received yet. For Eos this almost
    #: always means OSC output is switched off on the console.
    WAITING = "Waiting for data"
    LIVE = "Live"
    #: Was live, but nothing has arrived for longer than expected.
    STALE = "Stale"
    ERROR = "Error"

    @property
    def is_usable(self) -> bool:
        """True when data is actually flowing."""
        return self is ConnectionState.LIVE


@dataclass(frozen=True, slots=True)
class MonitorEntry:
    """One line in a connection's Monitor tab."""

    timestamp: float
    direction: str  # "rx" or "tx"
    summary: str
    raw: bytes | None = None
    #: This entry's place in everything the connection has recorded, from 1.
    #: A view asks for what came after the last one it showed; see
    #: Connection.monitor_since.
    seq: int = 0

    def age(self) -> float:
        return time.perf_counter() - self.timestamp


@dataclass
class ConnectionStatus:
    """Everything the UI needs to show about a connection's health."""

    state: ConnectionState = ConnectionState.DISCONNECTED
    detail: str = ""
    packets: int = 0
    bytes_received: int = 0
    last_packet_at: float = 0.0
    connected_at: float = 0.0
    reconnect_attempts: int = 0
    #: Packets that arrived on the socket but were not from the thing this
    #: connection speaks for -- another app's OSC, a projector keepalive.
    #: Counted apart from ``packets`` on purpose: ``packets`` has to stay the
    #: honest answer to "how much has the console sent", which is the number
    #: staleness and the Live light are decided on. But once foreign traffic
    #: stopped counting, a shared port reads "0 packets" on the Connections
    #: panel while the monitor pane below it scrolls rx rows, which looks like
    #: the panel contradicting itself. This is the number that explains it;
    #: whoever next touches ui/connection_panel.py's counter line should add
    #: it there ("N not from the console") -- describe() already carries it.
    noise_packets: int = 0
    noise_bytes: int = 0
    last_noise_at: float = 0.0
    _recent: deque[float] = field(default_factory=lambda: deque(maxlen=120))

    def note_packet(self, size: int = 0) -> None:
        now = time.perf_counter()
        self.packets += 1
        self.bytes_received += size
        self.last_packet_at = now
        self._recent.append(now)

    def note_noise(self, size: int = 0) -> None:
        """Count a packet that arrived but was not from this source."""
        self.noise_packets += 1
        self.noise_bytes += size
        self.last_noise_at = time.perf_counter()

    @property
    def packets_per_second(self) -> float:
        """Rate over the recent window."""
        if len(self._recent) < 2:
            return 0.0
        span = self._recent[-1] - self._recent[0]
        return (len(self._recent) - 1) / span if span > 0 else 0.0

    @property
    def seconds_since_packet(self) -> float | None:
        if self.last_packet_at == 0.0:
            return None
        return time.perf_counter() - self.last_packet_at

    def describe(self) -> str:
        """One line for a status bar."""
        parts = [self.state.value]
        if self.detail:
            parts.append(self.detail)
        since = self.seconds_since_packet
        if since is not None:
            parts.append(f"{since:.1f}s since last packet")
        if self.packets:
            parts.append(f"{self.packets_per_second:.1f} pkt/s")
        if self.noise_packets:
            parts.append(f"{self.noise_packets} not from this source")
        return " · ".join(parts)


class Connection(ABC):
    """Base class for everything that produces data.

    Subclasses implement :meth:`_run_once`, which should connect, pump data
    until it fails or is asked to stop, and return. Reconnection, backoff,
    status bookkeeping and the monitor buffer are handled here so that every
    connection behaves the same way when things go wrong.
    """

    #: Seconds without data before a live connection is reported STALE.
    #: Overridden per protocol: Eos is event-driven and can legitimately be
    #: silent for minutes, whereas sACN streams continuously.
    stale_after: float = 30.0

    #: Whether to keep retrying after a failure.
    auto_reconnect: bool = True

    #: How long a run has to stay up before the link counts as recovered, so
    #: that its next drop starts the backoff schedule from the top again.
    #:
    #: Without this the schedule only ever climbed. Six ended runs of any kind
    #: in a session -- one desk reboot, or Wer opened before the desk was
    #: powered -- left every later retry waiting 30 s, so a blip two hours
    #: into a tech that should have recovered in half a second cost 30 s of
    #: cue fires with no markers instead, and so did every drop after it.
    #:
    #: The longest delay in the schedule rather than "the run delivered data",
    #: because it bounds the other direction too. A link that drops sooner
    #: than this goes on backing off, and one that lasts longer can never be
    #: re-dialled more often than a console that is switched off -- so a desk
    #: that accepts, sends its state dump and hangs up straight away does not
    #: become a reconnect loop twice a second all night.
    backoff_resets_after: float = BACKOFF_SCHEDULE[-1]

    def __init__(
        self,
        connection_id: str,
        bus: DataBus,
        *,
        monitor_capacity: int = 500,
    ) -> None:
        self.id = connection_id
        self.bus = bus
        self.status = ConnectionStatus()

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

        # Capped deliberately: an Eos command line at full tilt would otherwise
        # grow this without bound over a four-hour tech.
        self._monitor: deque[MonitorEntry] = deque(maxlen=monitor_capacity)
        self._monitor_paused = False
        #: Entries ever recorded into the monitor. Never goes back down, not
        #: even on clear, so a position taken from it stays meaningful.
        self._monitor_seq = 0
        #: Where the monitor was last cleared. Entries up to here went because
        #: someone pressed Clear, not because the buffer overflowed, and are
        #: not reported as lost.
        self._monitor_cleared_through = 0

        self._status_listeners: list[Callable[[ConnectionStatus], None]] = []

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._supervise, name=f"conn-{self.id}", daemon=True
        )
        self._thread.start()
        log.info("Connection %s started", self.id)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                log.warning("Connection %s did not stop within %gs", self.id, timeout)
        self._thread = None
        self._set_state(ConnectionState.DISCONNECTED)
        log.info("Connection %s stopped", self.id)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _supervise(self) -> None:
        """Run, and keep re-running, until told to stop."""
        attempt = 0
        while not self._stop.is_set():
            self._set_state(ConnectionState.CONNECTING)
            started = time.perf_counter()
            try:
                self._run_once()
            except Exception as exc:  # noqa: BLE001 - boundary; must not escape
                log.exception("Connection %s failed", self.id)
                self._set_state(ConnectionState.ERROR, str(exc))
            else:
                if self._stop.is_set():
                    break
                self._set_state(ConnectionState.DISCONNECTED, "connection closed")

            if not self.auto_reconnect or self._stop.is_set():
                break

            if time.perf_counter() - started >= self.backoff_resets_after:
                # This run outlived the last outage, so this drop is a new one
                # and gets the fast retries. See backoff_resets_after.
                attempt = 0
            delay = BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]
            attempt += 1
            self.status.reconnect_attempts = attempt
            self._notify()
            log.info(
                "Connection %s reconnecting in %gs (attempt %d)", self.id, delay, attempt
            )
            # Waiting on the stop event rather than sleeping means shutdown is
            # immediate even in the middle of a 30-second backoff.
            if self._stop.wait(delay):
                break

        self._set_state(ConnectionState.DISCONNECTED)

    @abstractmethod
    def _run_once(self) -> None:
        """Connect and pump until failure or stop. Return to trigger a retry."""

    def _wake(self) -> None:
        """Hook for subclasses to interrupt a blocking read on stop."""

    # ---------------------------------------------------------------- status

    def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        with self._lock:
            if self.status.state is state and self.status.detail == detail:
                return
            self.status.state = state
            self.status.detail = detail
            if state is ConnectionState.WAITING:
                self.status.connected_at = time.perf_counter()
        log.debug("Connection %s -> %s %s", self.id, state.value, detail)
        self._notify()

    def refresh_staleness(self) -> None:
        """Re-evaluate LIVE vs STALE. Call periodically from the UI.

        Staleness is time-based, so nothing will move a connection into STALE
        on its own -- no packet arrives to trigger it. That is the whole point:
        the transition happens precisely when packets stop.
        """
        with self._lock:
            if self.status.state not in (ConnectionState.LIVE, ConnectionState.STALE):
                return
            since = self.status.seconds_since_packet
            if since is None:
                return
            should_be_stale = since > self.stale_after
            if should_be_stale and self.status.state is ConnectionState.LIVE:
                self._set_state(
                    ConnectionState.STALE, f"no data for {since:.0f}s"
                )
            elif not should_be_stale and self.status.state is ConnectionState.STALE:
                self._set_state(ConnectionState.LIVE)

    def add_status_listener(self, listener: Callable[[ConnectionStatus], None]) -> None:
        self._status_listeners.append(listener)

    def remove_status_listener(
        self, listener: Callable[[ConnectionStatus], None]
    ) -> None:
        if listener in self._status_listeners:
            self._status_listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self._status_listeners):
            try:
                listener(self.status)
            except Exception:  # noqa: BLE001
                log.exception("Connection status listener raised")

    # --------------------------------------------------------------- monitor

    def record(self, summary: str, *, direction: str = "rx", raw: bytes | None = None,
               size: int = 0, liveness: bool = True, noise: bool = True) -> None:
        """Note one packet: updates status and appends to the monitor.

        ``liveness=False`` puts the packet in the monitor without letting it
        count as the source being alive. It exists because arrival on a socket
        is not proof that the thing you care about is talking: a UDP connection
        binds 0.0.0.0 and hears everything aimed at that port. Two identical
        connections were left with a desk that had gone silent; the control
        went STALE after 119 s, and the one with a second OSC app sending to
        the same port never did -- it held at Live, with ``eos.connected``
        true, over a cue the desk had left minutes earlier.

        Such a packet is still counted, in ``noise_packets`` rather than
        ``packets``, so that "nothing has arrived at all" and "plenty has
        arrived, none of it the console" stay tellable apart by anyone
        debugging a port clash.

        ``noise=False`` as well counts it as neither: a packet that is meant
        for this connection but is no evidence the source is alive. The Eos
        connection's /wer/ commands are that, since over UDP anything on the
        network can send one.
        """
        if direction == "rx":
            if liveness:
                self.status.note_packet(size)
                if self.status.state in (
                    ConnectionState.WAITING,
                    ConnectionState.STALE,
                    ConnectionState.CONNECTING,
                ):
                    self._set_state(ConnectionState.LIVE)
            elif noise:
                self.status.note_noise(size)
        with self._lock:
            if not self._monitor_paused:
                self._monitor_seq += 1
                self._monitor.append(
                    MonitorEntry(
                        time.perf_counter(), direction, summary, raw,
                        seq=self._monitor_seq,
                    )
                )

    def monitor_entries(self, limit: int | None = None) -> list[MonitorEntry]:
        with self._lock:
            entries = list(self._monitor)
        return entries[-limit:] if limit else entries

    def monitor_since(self, seq: int) -> tuple[list[MonitorEntry], int]:
        """Entries recorded after ``seq``, and how many of those are gone.

        For a view that appends lines as they arrive: pass 0 the first time,
        then the ``seq`` of the last entry it showed. The second number counts
        entries after ``seq`` that the capped buffer dropped before they could
        be read, so the view can say lines are missing rather than look
        complete. Entries removed by clear_monitor are not counted -- throwing
        those away was the point.

        monitor_entries() cannot answer this. Its length stops changing once
        the buffer is full, and the Connections tab, which took that length as
        "lines so far", stopped adding lines there for good.
        """
        with self._lock:
            entries = list(self._monitor)
            cleared_through = self._monitor_cleared_through
            latest = self._monitor_seq
        first_kept = entries[0].seq if entries else latest + 1
        dropped = first_kept - max(seq, cleared_through) - 1
        return [entry for entry in entries if entry.seq > seq], max(0, dropped)

    def clear_monitor(self) -> None:
        with self._lock:
            self._monitor.clear()
            self._monitor_cleared_through = self._monitor_seq

    @property
    def monitor_paused(self) -> bool:
        return self._monitor_paused

    @monitor_paused.setter
    def monitor_paused(self, value: bool) -> None:
        self._monitor_paused = value

    # ------------------------------------------------------------ publishing

    def publish(self, key: str, value: object, *, stale_after: float | None = None) -> None:
        self.bus.publish(
            key, value, source_connection_id=self.id, stale_after=stale_after
        )

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for the UI."""
