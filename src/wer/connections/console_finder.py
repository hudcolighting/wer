"""Find a working console by trying the saved ones in turn.

No Qt. Runs on its own thread and reports progress through callbacks.

Why "connected" is not good enough
----------------------------------
The obvious implementation connects to each console and keeps the first one
that accepts. That does not work, and the reason is the single most important
thing learned from testing against a real desk:

**Eos accepts a TCP connection whether or not its OSC output is enabled, and
then sends nothing.** A console sitting in the corner with OSC switched off
looks exactly like a working one for as long as you only look at the socket. An
auto-connect that stopped there would attach to the wrong desk and sit silent
all night.

So a console counts as found only once it has actually **sent** something. That
is a reliable test rather than a hopeful one, because Eos dumps its whole state
within about a second of a client connecting -- roughly thirty messages -- so a
live console announces itself immediately without anyone touching it.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from wer.connections.eos import speaks_eos
from wer.connections.osc import (
    LengthPrefixFramer,
    OscDecodeError,
    SlipFramer,
    encode_message,
)

log = logging.getLogger(__name__)

__all__ = ["ProbeResult", "ConsoleFinder", "probe_console"]


@dataclass
class ProbeResult:
    """What happened when one console was tried."""

    name: str
    reachable: bool
    #: True only if the console actually sent OSC. This is the one that counts.
    talking: bool
    detail: str = ""
    seconds: float = 0.0

    @property
    def usable(self) -> bool:
        return self.talking

    def describe(self) -> str:
        if self.talking:
            return f"{self.name}: talking"
        if self.reachable:
            # Say what actually happened when we know. Overriding a specific
            # detail with the usual guess is how a desk that was transmitting
            # perfectly well got diagnosed as having OSC switched off.
            if self.detail:
                return f"{self.name}: {self.detail}"
            return (
                f"{self.name}: connected but silent — OSC output is probably "
                "switched off on the desk"
            )
        return f"{self.name}: {self.detail or 'no answer'}"


def probe_console(
    host: str,
    port: int,
    *,
    transport: str = "TCP",
    framing: str = "OSC 1.0 (length prefix)",
    user: int = 1,
    subscribe: bool = True,
    name: str = "",
    connect_timeout: float = 2.0,
    listen_seconds: float = 4.0,
    should_stop: Callable[[], bool] | None = None,
) -> ProbeResult:
    """Try one console and report whether it actually talks.

    Deliberately short-lived: it opens a connection, listens briefly, and closes
    again. The real connection is made separately once a console has been
    chosen, so a probe can never be mistaken for the live link.
    """
    label = name or f"{host}:{port}"
    started = time.perf_counter()

    if transport.upper() == "UDP":
        # Nothing to connect to: a UDP console is told where to send, and
        # either it is sending or it is not. Listening is the whole test --
        # but listening for Eos, not for noise; the socket is bound to
        # 0.0.0.0 and hears whatever else the network aims at that port.
        return _probe_udp(label, port, listen_seconds, started, should_stop)

    try:
        sock = socket.create_connection((host, port), timeout=connect_timeout)
    except OSError as exc:
        return ProbeResult(
            label, reachable=False, talking=False,
            detail=_explain(exc), seconds=time.perf_counter() - started,
        )

    framer = SlipFramer() if "SLIP" in framing else LengthPrefixFramer()
    try:
        # The same handshake the real connection makes. Without it a console
        # may have nothing to say, and the probe would wrongly call it silent.
        def send(raw: bytes) -> None:
            sock.sendall(
                SlipFramer.frame(raw) if isinstance(framer, SlipFramer)
                else LengthPrefixFramer.frame(raw)
            )

        try:
            send(encode_message("/eos/user", user))
            if subscribe:
                send(encode_message("/eos/subscribe", 1))
        except OSError as exc:
            return ProbeResult(
                label, reachable=True, talking=False,
                detail=f"connected, then the link dropped: {exc}",
                seconds=time.perf_counter() - started,
            )

        sock.settimeout(0.5)
        deadline = time.perf_counter() + listen_seconds
        while time.perf_counter() < deadline:
            if should_stop is not None and should_stop():
                break
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                return ProbeResult(
                    label, reachable=True, talking=False, detail=str(exc),
                    seconds=time.perf_counter() - started,
                )
            if not chunk:
                return ProbeResult(
                    label, reachable=True, talking=False,
                    detail="the console closed the connection",
                    seconds=time.perf_counter() - started,
                )
            try:
                if framer.feed(chunk):
                    return ProbeResult(
                        label, reachable=True, talking=True,
                        seconds=time.perf_counter() - started,
                    )
            except OscDecodeError as exc:
                # Bytes arrived but did not frame: something is there, but not
                # speaking the framing we chose. Worth saying precisely.
                return ProbeResult(
                    label, reachable=True, talking=False,
                    detail=f"answered, but not in this framing ({exc})",
                    seconds=time.perf_counter() - started,
                )

        # Deliberately no detail. "Connected but sent nothing" is true and
        # useless; leaving it empty lets describe() give the hint that is
        # actually actionable -- go and check OSC output on the desk.
        return ProbeResult(
            label, reachable=True, talking=False, detail="",
            seconds=time.perf_counter() - started,
        )
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _probe_udp(
    label: str, port: int, listen_seconds: float, started: float,
    should_stop: Callable[[], bool] | None,
) -> ProbeResult:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
    except OSError as exc:
        return ProbeResult(label, False, False, str(exc), time.perf_counter() - started)
    heard_something = False
    try:
        sock.settimeout(0.5)
        deadline = time.perf_counter() + listen_seconds
        while time.perf_counter() < deadline:
            if should_stop is not None and should_stop():
                break
            try:
                datagram, _peer = sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not datagram:
                continue
            if speaks_eos(datagram):
                return ProbeResult(
                    label, True, True, seconds=time.perf_counter() - started
                )
            # Something is here, but it is not a console. Keep listening: the
            # desk may be sharing the port with it.
            #
            # That patience is not free, and it is deliberate. A port with
            # chatter on it now costs the full listen_seconds instead of
            # returning on the first datagram (measured: 2.01 s against
            # listen_seconds=2.0, versus 0.00 s when any datagram counted), so
            # a venue with noise on both UDP endpoints adds up to 2 x
            # listen_seconds per profile to the sweep, and the loop turns over
            # without sleeping for as long as datagrams keep arriving. A real
            # desk is still found on the first packet, in 0.00 s, and
            # should_stop is checked every pass so Cancel still answers.
            #
            # Any datagram at all used to count, and this is the one place in
            # the app where a wrong answer is written down and kept -- _try
            # copies the guessed transport and port onto the saved profile, so
            # a projector keepalive on UDP 8123 became the venue's console
            # permanently, was found again on pass one at every later launch,
            # and the real desk was never probed after that.
            heard_something = True
        return ProbeResult(
            label, True, False,
            "something is sending to this port, but it is not Eos"
            if heard_something else "nothing arrived on this port",
            time.perf_counter() - started,
        )
    finally:
        try:
            sock.close()
        except OSError:
            pass


class ConsoleFinder:
    """Tries saved consoles in order until one is actually talking."""

    def __init__(
        self,
        *,
        on_progress: Callable[[str], None] | None = None,
        on_finished: Callable[[object | None, list[ProbeResult]], None] | None = None,
        sweep: bool = True,
    ) -> None:
        self._on_progress = on_progress
        self._on_finished = on_finished
        #: Also try the other endpoints a desk is known to use, not just the
        #: one saved against each profile.
        self.sweep = sweep
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.results: list[ProbeResult] = []

    @staticmethod
    def order(profiles: Sequence) -> list:
        """Most recently used first, then the rest.

        Ordering by when a console last actually worked is what makes "the one
        I used last" correct without anyone marking a favourite. A console that
        has never connected sorts last, but is still tried -- it may be a rig
        set up in advance for tonight.
        """
        enabled = [p for p in profiles if getattr(p, "enabled", True)]
        return sorted(enabled, key=lambda p: p.last_connected, reverse=True)

    #: Every way an Eos has actually been seen to transmit, in the order worth
    #: trying. This list is evidence, not documentation: a real Ion XE was
    #: found sending UDP to 8123 while both TCP defaults sat silent, and the
    #: same desk later answered 3032 in SLIP rather than length-prefix. Trust
    #: the sweep, not the manual.
    COMMON_ENDPOINTS: tuple[tuple[str, str, int, str], ...] = (
        ("TCP 3032, OSC 1.0", "TCP", 3032, "OSC 1.0 (length prefix)"),
        ("TCP 3032, OSC 1.1", "TCP", 3032, "OSC 1.1 (SLIP)"),
        ("TCP 3033, OSC 1.1", "TCP", 3033, "OSC 1.1 (SLIP)"),
        ("UDP 8000", "UDP", 8000, "OSC 1.0 (length prefix)"),
        ("UDP 8123", "UDP", 8123, "OSC 1.0 (length prefix)"),
    )

    @classmethod
    def endpoints_for(cls, profile) -> list[tuple[str, str, int, str]]:
        """The profile's own settings first, then the others worth trying.

        Whatever the user configured is tried before anything is guessed --
        so a working setup is never slowed down by the sweep.
        """
        own = (
            f"{profile.transport} {profile.port}",
            str(profile.transport), int(profile.port), str(profile.framing),
        )
        rest = [
            e for e in cls.COMMON_ENDPOINTS
            if (e[1], e[2], e[3]) != (own[1], own[2], own[3])
        ]
        return [own, *rest]

    def start(self, profiles: Sequence, *, listen_seconds: float = 4.0) -> None:
        """Begin searching. Returns immediately."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.results = []
        self._thread = threading.Thread(
            target=self._run, args=(list(profiles), listen_seconds),
            name="console-finder", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=8.0)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _progress(self, message: str) -> None:
        log.info("Console search: %s", message)
        if self._on_progress is not None:
            try:
                self._on_progress(message)
            except Exception:  # noqa: BLE001
                log.exception("Console search progress handler raised")

    def _run(self, profiles: list, listen_seconds: float) -> None:
        ordered = self.order(profiles)
        if not ordered:
            self._progress("No consoles saved")
            self._finish(None)
            return

        # Pass one: exactly what each profile says, most recent first. This is
        # the whole search on a night when nothing has moved, and it stays
        # predictable -- a profile connects to the desk it names, or not at all.
        for index, profile in enumerate(ordered, start=1):
            if self._stop.is_set():
                break
            self._progress(
                f"Trying {profile.name} ({profile.summary}) — "
                f"{index} of {len(ordered)}"
            )
            if self._try(profile, str(profile.transport), int(profile.port),
                         str(profile.framing), profile.name, listen_seconds):
                return

        # Pass two only happens when pass one found nothing at all. A desk can
        # move between transports and ports without moving machines -- a real
        # Ion XE was found transmitting UDP to 8123 while both TCP defaults sat
        # silent -- and guessing is worth doing once every honest attempt has
        # failed. Never before: it must not quietly redirect a profile that
        # names a specific desk to a different one on the same host.
        if not self.sweep or self._stop.is_set():
            self._progress("No console answered")
            self._finish(None)
            return

        self._progress("Nothing on the saved settings; trying other ports")
        for profile in ordered:
            if self._stop.is_set():
                break
            for label, transport, port, framing in self.COMMON_ENDPOINTS:
                if self._stop.is_set():
                    break
                if (transport, port, framing) == (
                    str(profile.transport), int(profile.port), str(profile.framing)
                ):
                    continue                      # already tried in pass one
                where = f"{profile.name} on {label}"
                self._progress(f"Trying {where}")
                if self._try(profile, transport, port, framing, where,
                             listen_seconds):
                    return

        self._progress("No console answered")
        self._finish(None)

    def _try(self, profile, transport: str, port: int, framing: str,
             name: str, listen_seconds: float) -> bool:
        """Probe one endpoint. On success, write it back onto the profile.

        Writing back matters: finding the desk and then dialling the saved
        port would leave the search technically correct and practically
        useless.
        """
        result = probe_console(
            profile.host, port,
            transport=transport,
            framing=framing,
            user=profile.user,
            subscribe=profile.subscribe,
            name=name,
            listen_seconds=listen_seconds,
            should_stop=self._stop.is_set,
        )
        self.results.append(result)
        self._progress(result.describe())
        if not result.usable:
            return False
        profile.transport = transport
        profile.port = port
        profile.framing = framing
        self._finish(profile)
        return True

    def _finish(self, profile) -> None:
        if self._on_finished is None:
            return
        try:
            self._on_finished(profile, list(self.results))
        except Exception:  # noqa: BLE001
            log.exception("Console search completion handler raised")


def _explain(exc: OSError) -> str:
    text = str(exc)
    if isinstance(exc, socket.timeout) or "timed out" in text:
        return "no answer — check the address and that both are on the same subnet"
    if getattr(exc, "winerror", None) == 10061 or "refused" in text.lower():
        return "refused — Eos may not be running, or OSC TCP may be off"
    if getattr(exc, "winerror", None) == 10065 or "unreachable" in text.lower():
        return "unreachable — check the network"
    return text
