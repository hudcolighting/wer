"""Wer's recording status, sent over sACN.

No Qt. Hudson's spec, 13 Sep 2026: one address in one universe sits at 0 while
Wer is not recording, and fades smoothly from 0 to 255 and back every 2 s
while it is. Universe, address, priority, per-address priority and the network
to send on are his to set, starting from universe 101, address 1. Multicast
only. Nothing is sent until switched on.

The sender asks whether Wer is recording before every packet rather than being
told. The answer changes on three different threads, and a pushed update can
arrive out of order; asking cannot.

Packets (see wer.connections.sacn for their layout and sources):

- Levels: all 512 slots, 0 except the chosen address. 30 a second while the
  level moves; once it stops changing, four in all in quick succession, then
  one every KEEPALIVE_INTERVAL.
- Per-address priorities (START code 0xDD), when switched on: the priority at
  the chosen address and 0 everywhere else, so receivers that understand them
  let Wer have that one address and nothing more. The framing priority carries
  the same number in both kinds of packet, as ETC's library does.
- Universe discovery every DISCOVERY_INTERVAL.
- Whenever the stream ends -- switched off, a new universe or network, Wer
  closing -- the address at 0, then three stream-terminated packets.

The network is looked at again on a thread of its own (_NetworkWatch): Windows'
adapter table is slow to read, and reading it in the send loop froze the fade.

Problems -- the network gone, a send failing -- are waited out here and said in
the connection's status, which reads Live only once a packet has actually gone.
Returning from _run_once would read as "connection closed" and back off for up
to 30 s.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from wer.connections import adapters as network
from wer.connections.adapters import AdapterChoice, NetworkAdapter, choose_adapter
from wer.connections.base import Connection, ConnectionState
from wer.connections.sacn import (
    DEFAULT_PRIORITY,
    DISCOVERY_INTERVAL,
    DISCOVERY_UNIVERSE,
    KEEPALIVE_INTERVAL,
    MULTICAST_TTL,
    OPTION_STREAM_TERMINATED,
    PRIORITY_KEEPALIVE_INTERVAL,
    PRIORITY_MAX,
    REPEATS_AFTER_CHANGE,
    SACN_PORT,
    SLOT_COUNT,
    START_CODE_PER_ADDRESS_PRIORITY,
    TERMINATION_PACKETS,
    UNIVERSE_MAX,
    UNIVERSE_MIN,
    cid_bytes,
    encode_data_packet,
    encode_discovery_packet,
    multicast_group,
    pulse_level,
)
from wer.core.databus import DataBus

log = logging.getLogger(__name__)

__all__ = ["SacnSender", "SacnSettings", "default_source_name"]

# How a stream came to an end; see SacnSender._pump.
_STOPPED = "stopped"
_CHANGED = "changed"
_FAILED = "failed"


def default_source_name() -> str:
    """What receivers list this Wer as, unless a name was typed."""
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    return f"Wer on {host}" if host else "Wer"


@dataclass(frozen=True, slots=True)
class SacnSettings:
    #: 0 is not a universe; nothing is sent until there is one.
    universe: int = 0
    #: Numbered 1-512, as a console numbers them. 0 sends nothing.
    address: int = 0
    priority: int = DEFAULT_PRIORITY
    per_address_priority: bool = True
    #: The adapter's Windows key, "" for Automatic.
    adapter: str = ""
    #: What that adapter was called when it was chosen, to name it when it is
    #: missing.
    adapter_name: str = ""
    #: "" sends default_source_name().
    source_name: str = ""
    #: This Wer's source identity.
    cid: str = ""

    def problem(self) -> str:
        """Why these settings cannot be sent, or "" when they can."""
        if not UNIVERSE_MIN <= self.universe <= UNIVERSE_MAX:
            return f"Choose a universe to send on ({UNIVERSE_MIN}-{UNIVERSE_MAX})."
        if not 1 <= self.address <= SLOT_COUNT:
            return f"Choose an address in universe {self.universe} (1-{SLOT_COUNT})."
        if not 1 <= self.priority <= PRIORITY_MAX:
            return f"Choose a priority from 1 to {PRIORITY_MAX}."
        try:
            cid_bytes(self.cid)
        except (ValueError, TypeError, AttributeError):
            return "This Wer has no sACN source identity yet."
        return ""

    @property
    def name_to_send(self) -> str:
        return self.source_name or default_source_name()


def _describe(exc: OSError) -> str:
    winerror = getattr(exc, "winerror", None)
    text = exc.strerror or str(exc)
    return f"{text} (Windows error {winerror})" if winerror else text


class _NetworkWatch:
    """Looks at the network again every adapter_check_interval, on its own thread.

    Windows' adapter table is slow to read, and reading it in the send loop held
    up every packet while it did: a stall in the fade every 2 s, and a constant
    one if a read ever took longer than the interval. A read that fails leaves
    the stream as it is. Only an answer can move or stop it: a failed look at
    the adapters is not the same as the adapter having gone.
    """

    def __init__(
        self, sender: SacnSender, settings: SacnSettings, choice: AdapterChoice
    ) -> None:
        self._sender = sender
        self._settings = settings
        self._choice = choice
        self._stop = threading.Event()
        #: Set once the network the stream is on has gone or changed.
        self.changed = threading.Event()
        self.fresh: AdapterChoice | None = None
        self._thread = threading.Thread(
            target=self._run, name=f"conn-{sender.id}-network", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        # Not joined: a read already under way finishes on its own and its
        # answer is thrown away.
        self._stop.set()

    def _run(self) -> None:
        said_failure = False
        while not self._stop.wait(self._sender.adapter_check_interval):
            try:
                fresh = self._sender._choose(self._settings)
            except Exception as exc:  # noqa: BLE001
                if not said_failure:
                    said_failure = True
                    log.warning(
                        "sACN output could not look at this computer's networks "
                        "again (%s); carrying on as before", exc,
                    )
                continue
            said_failure = False
            if self._stop.is_set():
                return
            if fresh.problem or fresh.ip != self._choice.ip:
                self.fresh = fresh
                self.changed.set()
                return


class SacnSender(Connection):
    """Sends Wer's recording status to one address in one universe."""

    #: Keep-alives go out every KEEPALIVE_INTERVAL at the slowest, so a send
    #: loop that has stopped shows as STALE within a few seconds.
    stale_after = 3.0
    #: Between packets while the level is moving.
    send_interval = 1.0 / 30
    #: How often the network is looked at again, and how long a problem is
    #: waited on before it is looked at again.
    adapter_check_interval = 2.0
    #: A send that cannot finish in this long is a failure, not a hang.
    socket_timeout = 1.0
    #: How long start() waits for a sender thread that outlived stop().
    previous_stop_wait = 5.0
    #: Between the packets that end a stream.
    ending_gap = 0.01

    def __init__(
        self,
        connection_id: str,
        bus: DataBus,
        settings: SacnSettings | None = None,
        *,
        is_recording: Callable[[], bool],
        port: int = SACN_PORT,
        list_adapters: Callable[[], Sequence[NetworkAdapter]] = network.read_adapters,
        route_source_ip: Callable[[], str | None] = network.route_source_ip,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(connection_id, bus, monitor_capacity=200)
        self._settings = settings or SacnSettings()
        self._settings_lock = threading.Lock()
        self._is_recording = is_recording
        self.port = port
        self._list_adapters = list_adapters
        self._route_source_ip = route_source_ip
        self._clock = clock
        #: Where the last look at the network said to send, for the tab.
        self.choice: AdapterChoice | None = None
        #: How many times Automatic has moved a running stream to another
        #: network, and the last move in words, for the status bar.
        self.moves = 0
        self.last_move = ""
        self._sequence = 0
        #: The problem last logged, so one that persists is said once.
        self._said_problem = ""
        #: The stream last said to be sending, so one that restarts is not said
        #: again unless something about it changed.
        self._said_sending: tuple | None = None
        self._said_is_recording_failed = False
        #: A sender thread that outlived stop(); see stop() and start().
        self._lingering: threading.Thread | None = None

    @property
    def display_name(self) -> str:
        return "sACN output"

    # --------------------------------------------------------------- settings

    @property
    def settings(self) -> SacnSettings:
        with self._settings_lock:
            return self._settings

    @settings.setter
    def settings(self, value: SacnSettings) -> None:
        with self._settings_lock:
            self._settings = value

    def update_settings(self, settings: SacnSettings) -> bool:
        """Use new settings from the next packet, where a running stream can.

        False, changing nothing, when the universe, the network or the source
        identity changed on a running sender: that is a different stream, and
        the caller stops and starts the sender so the old one is ended properly.
        """
        with self._settings_lock:
            old = self._settings
            if self.is_running and (
                (settings.universe, settings.adapter, settings.cid)
                != (old.universe, old.adapter, old.cid)
            ):
                return False
            self._settings = settings
        return True

    # -------------------------------------------------------------- lifecycle

    def stop(self, timeout: float = 3.0) -> None:
        thread = self._thread
        super().stop(timeout)
        if thread is not None and thread.is_alive():
            # Connection.stop forgets a thread that outlived its join, and the
            # next start() would clear the stop flag it is still waiting to
            # see: two senders on one CID and one sequence counter.
            self._lingering = thread

    def start(self) -> None:
        lingering = self._lingering
        if lingering is not None and lingering.is_alive():
            lingering.join(self.previous_stop_wait)
            if lingering.is_alive():
                log.error(
                    "sACN output: the last sender has still not stopped, so a "
                    "second is not being started beside it"
                )
                self._set_state(
                    ConnectionState.ERROR,
                    "The last sACN stream has not finished stopping. Switch sACN "
                    "output off and on again.",
                )
                return
        self._lingering = None
        super().start()

    # ---------------------------------------------------------------- running

    def _run_once(self) -> None:
        while not self._stop.is_set():
            ready = self._wait_until_ready()
            if ready is None:
                return
            self._stream(*ready)

    def _wait_until_ready(self) -> tuple[SacnSettings, AdapterChoice] | None:
        """Settings that can be sent and a network to send them on, or None on stop."""
        while not self._stop.is_set():
            settings = self.settings
            problem = settings.problem()
            choice = None
            if not problem:
                try:
                    choice = self._choose(settings)
                except Exception as exc:  # noqa: BLE001
                    problem = f"Could not look at this computer's networks: {exc}"
                else:
                    problem = choice.problem
            if not problem:
                return settings, choice
            self._say_problem(problem)
            if self._stop.wait(self.adapter_check_interval):
                return None
        return None

    def _choose(self, settings: SacnSettings) -> AdapterChoice:
        """Where these settings would send now. Raises if the networks cannot be read."""
        listed = list(self._list_adapters())
        route = None if settings.adapter else self._route_source_ip()
        choice = choose_adapter(
            listed, settings.adapter, route_ip=route, saved_name=settings.adapter_name
        )
        self.choice = choice
        return choice

    def _open_socket(self, ip: str) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(self.socket_timeout)
            # Windows' default multicast TTL is 1, which the first router
            # drops; see MULTICAST_TTL.
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MULTICAST_TTL)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
        except OSError:
            sock.close()
            raise
        return sock

    def _stream(self, settings: SacnSettings, choice: AdapterChoice) -> None:
        destination = (multicast_group(settings.universe), self.port)
        cid = cid_bytes(settings.cid)
        try:
            sock = self._open_socket(choice.ip)
        except OSError as exc:
            self._fail(f"Could not send sACN on {choice.label}: {_describe(exc)}")
            return
        watch = _NetworkWatch(self, settings, choice)
        watch.start()
        outcome = _FAILED
        try:
            outcome = self._pump(sock, destination, cid, settings, choice, watch)
        finally:
            watch.stop()
            if outcome != _FAILED:
                self._end_stream(sock, destination, cid, settings.universe)
            sock.close()

    def _pump(
        self,
        sock: socket.socket,
        destination: tuple[str, int],
        cid: bytes,
        started_with: SacnSettings,
        choice: AdapterChoice,
        watch: _NetworkWatch,
    ) -> str:
        """Send until stopped, until the stream has to change, or until a send fails."""
        stream = (started_with.universe, started_with.adapter, started_with.cid)
        discovery = (multicast_group(DISCOVERY_UNIVERSE), self.port)
        announced = False
        now = self._clock()
        next_levels = next_priorities = next_discovery = now
        levels_repeats = priority_repeats = 0
        levels_sent: tuple | None = None
        priorities_sent: tuple | None = None
        pulse_from: float | None = None

        while not self._stop.is_set():
            if watch.changed.is_set():
                self._network_changed(choice, watch.fresh)
                return _CHANGED
            now = self._clock()
            settings = self.settings
            if (settings.universe, settings.adapter, settings.cid) != stream:
                self.record("universe or network changed: ending this stream", direction="tx")
                return _CHANGED

            recording = self._recording_now()
            if recording and pulse_from is None:
                pulse_from = now
                self.record("recording: pulsing", direction="tx")
            elif not recording and pulse_from is not None:
                pulse_from = None
                self.record("not recording: 0", direction="tx")
            level = 0 if pulse_from is None else pulse_level(now - pulse_from)
            name = settings.name_to_send

            # A change goes out at once and then three more times in quick
            # succession, four in all, before keep-alives take over: ETC's
            # NUM_PRE_SUPPRESSION_PACKETS.
            wanted = (settings.address, level, settings.priority, name)
            if wanted != levels_sent:
                levels_sent = wanted
                levels_repeats = REPEATS_AFTER_CHANGE
                next_levels = now
            if now >= next_levels:
                slots = bytearray(SLOT_COUNT)
                slots[settings.address - 1] = level
                packet = encode_data_packet(
                    cid=cid, source_name=name, priority=settings.priority,
                    sequence=self._next_sequence(), universe=started_with.universe,
                    slots=slots,
                )
                if not self._send(sock, destination, packet, choice):
                    return _FAILED
                if not announced:
                    announced = True
                    self._announce(settings, choice)
                if levels_repeats > 0:
                    levels_repeats -= 1
                    next_levels = now + self.send_interval
                else:
                    next_levels = now + KEEPALIVE_INTERVAL

            if settings.per_address_priority:
                wanted_priorities = (settings.address, settings.priority, name)
                if wanted_priorities != priorities_sent:
                    priorities_sent = wanted_priorities
                    priority_repeats = REPEATS_AFTER_CHANGE
                    next_priorities = now
                if now >= next_priorities:
                    slots = bytearray(SLOT_COUNT)
                    slots[settings.address - 1] = settings.priority
                    packet = encode_data_packet(
                        cid=cid, source_name=name, priority=settings.priority,
                        sequence=self._next_sequence(), universe=started_with.universe,
                        slots=slots, start_code=START_CODE_PER_ADDRESS_PRIORITY,
                    )
                    if not self._send(sock, destination, packet, choice):
                        return _FAILED
                    if priority_repeats > 0:
                        priority_repeats -= 1
                        next_priorities = now + self.send_interval
                    else:
                        next_priorities = now + PRIORITY_KEEPALIVE_INTERVAL
            else:
                priorities_sent = None

            if now >= next_discovery:
                packet = encode_discovery_packet(
                    cid=cid, source_name=name, universes=[started_with.universe]
                )
                if not self._send(sock, discovery, packet, choice):
                    return _FAILED
                next_discovery = now + DISCOVERY_INTERVAL

            if announced:
                self._set_state(ConnectionState.LIVE, self._detail(settings, choice))
            due = min(next_levels, next_discovery)
            if settings.per_address_priority:
                due = min(due, next_priorities)
            wait = min(max(0.0, due - self._clock()), self.send_interval)
            if self._stop.wait(wait):
                break
        return _STOPPED

    def _send(
        self,
        sock: socket.socket,
        destination: tuple[str, int],
        packet: bytes,
        choice: AdapterChoice,
    ) -> bool:
        try:
            sock.sendto(packet, destination)
        except OSError as exc:
            self._fail(f"Could not send sACN on {choice.label}: {_describe(exc)}")
            return False
        self.status.note_packet(len(packet))
        return True

    def _announce(self, settings: SacnSettings, choice: AdapterChoice) -> None:
        """Say the stream is sending, now that a packet has gone.

        Not before. A network that took the socket and then refused every send
        read "sending universe ..." in the log every two seconds all night, and
        the status flicked to Live between failures.
        """
        key = (settings.universe, settings.address, choice.label)
        if self._said_problem:
            log.info("sACN output is sending again on %s", choice.label)
        elif key != self._said_sending:
            log.info(
                "sACN output sending universe %d, address %d, priority %d%s on %s",
                settings.universe, settings.address, settings.priority,
                " with per-address priority" if settings.per_address_priority else "",
                choice.label,
            )
        if key != self._said_sending or self._said_problem:
            self.record(
                f"sending universe {settings.universe} on {choice.label}", direction="tx"
            )
        self._said_sending = key
        self._said_problem = ""
        self._set_state(ConnectionState.LIVE, self._detail(settings, choice))

    def _network_changed(
        self, old: AdapterChoice, fresh: AdapterChoice | None
    ) -> None:
        if fresh is None:
            return
        if fresh.problem:
            reason = fresh.problem
        else:
            reason = f"the network to send on moved from {old.label} to {fresh.label}"
            if fresh.automatic:
                # Nothing about this is an error, and the stream carries on, so
                # without saying so the status bar would read "sending" while
                # every receiver on the first network lost it.
                self.moves += 1
                self.last_move = (
                    f"Automatic moved it from {old.label} to {fresh.label}, so "
                    "receivers on the first no longer get it."
                )
        log.warning("sACN output: %s", reason)
        self.record(f"network: {reason}", direction="tx")

    def _end_stream(
        self, sock: socket.socket, destination: tuple[str, int], cid: bytes,
        universe: int,
    ) -> None:
        """Send the address at 0, then end the stream.

        The 0 first, as ordinary levels. Receivers ignore the values in a
        stream-terminated packet (E1.31; ETC's receiver_state.c returns before
        using them), and what one does once a source has gone is its own
        setting: a receiver that holds its last look would otherwise hold
        wherever the pulse had got to. ``universe`` is the stream's own, not the
        settings': a changed universe is often why the stream is ending.
        """
        settings = self.settings
        name = settings.name_to_send
        priority = min(max(settings.priority, 1), PRIORITY_MAX)
        zeros = bytes(SLOT_COUNT)
        endings = [0] * (REPEATS_AFTER_CHANGE + 1) + [OPTION_STREAM_TERMINATED] * TERMINATION_PACKETS
        for options in endings:
            packet = encode_data_packet(
                cid=cid, source_name=name, priority=priority,
                sequence=self._next_sequence(), universe=universe, slots=zeros,
                options=options,
            )
            try:
                sock.sendto(packet, destination)
            except OSError as exc:
                log.info("sACN output could not send the end of its stream: %s", _describe(exc))
                return
            time.sleep(self.ending_gap)
        self.record("stream ended at 0", direction="tx")

    def _recording_now(self) -> bool:
        try:
            return bool(self._is_recording())
        except Exception:  # noqa: BLE001
            if not self._said_is_recording_failed:
                self._said_is_recording_failed = True
                log.exception("sACN output could not tell whether Wer is recording; sending 0")
            return False

    def _say_problem(self, problem: str) -> None:
        self._set_state(ConnectionState.ERROR, problem)
        if problem != self._said_problem:
            self._said_problem = problem
            log.warning("sACN output is not sending: %s", problem)
            self.record(f"not sending: {problem}", direction="tx")

    def _fail(self, problem: str) -> None:
        self._say_problem(problem)
        self._stop.wait(self.adapter_check_interval)

    def _next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0xFF
        return self._sequence

    @staticmethod
    def _detail(settings: SacnSettings, choice: AdapterChoice) -> str:
        return (
            f"Universe {settings.universe}, address {settings.address}, "
            f"priority {settings.priority}"
            f"{' per address' if settings.per_address_priority else ' for the whole universe'}"
            f", on {choice.label}"
        )

    def _wake(self) -> None:
        pass  # _stop.wait() already returns at once
