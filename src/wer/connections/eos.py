"""Live ETC Eos connection over OSC.

No Qt. Runs on its own thread, reconnects on its own, and publishes to the
DataBus through :class:`~wer.connections.eos_parser.EosParser`.

Transport defaults come from measurement rather than documentation: TCP 3032
with OSC 1.0 length-prefix framing is what a default-configured Eos actually
talks, as measured against ETCnomad and a real Ion XE; the captures are in
``tests/fixtures/eos/``.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from wer.connections.base import Connection, ConnectionState
from wer.connections.eos_parser import CueFired, EosParser
from wer.connections.osc import (
    LengthPrefixFramer,
    OscBundle,
    OscDecodeError,
    OscMessage,
    SlipFramer,
    decode_packet,
    encode_message,
)
from wer.connections.osc_control import COMMAND_PREFIX, CommandDispatcher
from wer.core.databus import DataBus

log = logging.getLogger(__name__)

#: /eos/out/user/<n>/cmd -- which user the desk is actually driven by.
_OUT_USER_CMD = re.compile(r"^/eos/out/user/(\d+)/cmd$")
#: The ongoing cue-state stream -- the traffic the OSC user binding gates.
_CUE_STATE = re.compile(r"^/eos/out/(?:active|pending)/cue(?:/|$)")
#: A cue actually went. These arrive whatever user the desk is bound to.
#: The two groups are the cue list and the cue number; the binding watch needs
#: them to tell "the show is advancing" from "the same cue was re-fired".
_CUE_FIRE = re.compile(r"^/eos/out/event/cue/([\d.]+)/([\d.]+)/fire$")

#: Everything an Eos sends lives under this. Anything else on the socket is
#: somebody else's -- another OSC app, a projector keepalive, a stray broadcast.
EOS_PREFIX = "/eos/"

__all__ = [
    "EosTransport",
    "EosFraming",
    "EosSettings",
    "EosConnection",
    "is_console_traffic",
    "speaks_eos",
]


def is_console_traffic(decoded: OscMessage | OscBundle) -> bool:
    """True when a decoded packet is Eos traffic rather than someone else's."""
    messages = (
        decoded.messages() if isinstance(decoded, OscBundle) else (decoded,)
    )
    return any(m.address.startswith(EOS_PREFIX) for m in messages)


def speaks_eos(packet: bytes) -> bool:
    """True when these bytes decode as OSC in the Eos namespace.

    "Something arrived on the port" is not the same question, and answering the
    easy one is how a projector keepalive on UDP 8123 got saved as the venue's
    console. Deliberately silent about *why* bytes failed: the caller is a
    sweep trying every port on the list, and a failure there is ordinary.
    """
    try:
        return is_console_traffic(decode_packet(packet))
    except Exception:  # noqa: BLE001 - any decode failure means "not Eos"
        return False


class EosTransport(str, Enum):
    TCP = "TCP"
    UDP = "UDP"


class EosFraming(str, Enum):
    #: OSC 1.0, 4-byte big-endian length prefix. Eos port 3032.
    LENGTH = "OSC 1.0 (length prefix)"
    #: OSC 1.1, SLIP per RFC 1055. Eos port 3033, but see the reference doc --
    #: 3033 stayed silent in testing, so this is provided rather than proven.
    SLIP = "OSC 1.1 (SLIP)"


@dataclass(frozen=True, slots=True)
class EosSettings:
    """Connection settings.

    ``transport`` and ``framing`` are coerced from plain strings in
    ``__post_init__``, which is not defensive tidiness -- it fixes a real bug.
    Both enums subclass ``str``, and Qt's ``QComboBox.currentData()`` flattens
    such a value back to a bare ``str`` on the way out of a QVariant. The
    dispatch in ``_run_once`` compares with ``is``, so a flattened ``"TCP"``
    would silently take the UDP branch. Show files are JSON and will hand us
    bare strings too, so the coercion belongs here rather than at each call
    site.
    """

    host: str = "127.0.0.1"
    transport: EosTransport = EosTransport.TCP
    #: 3032 for length-prefix framing. For UDP this is the port Eos transmits
    #: to, which we listen on -- the console's own labels are written from its
    #: point of view and are the classic way to wire this backwards.
    port: int = 3032
    framing: EosFraming = EosFraming.LENGTH

    #: The OSC user Wer announces when it connects. Eos sends the ongoing cue
    #: state only to that user, so 0 ("any user") is what a recording wants; a
    #: specific number silences the cue state whenever someone else is driving
    #: the desk -- and since cue fires still arrive, the cue number keeps
    #: moving while the next cue and the fade time do not. Whose command line
    #: a widget shows is not decided here: that is "Command line from", set
    #: per widget on the Overlay tab.
    user: int = 0
    send_user: bool = True
    #: `/eos/subscribe 1`. Costs a duplicate state dump on connect, which is
    #: harmless, and is what makes the console volunteer parameter changes.
    subscribe: bool = True

    #: Bind address for UDP. 0.0.0.0 listens on every interface, which is right
    #: on a laptop with both Wi-Fi and a show network attached.
    bind: str = "0.0.0.0"

    def __post_init__(self) -> None:
        # frozen dataclass: normalise through object.__setattr__.
        object.__setattr__(self, "transport", EosTransport(self.transport))
        object.__setattr__(self, "framing", EosFraming(self.framing))


class EosConnection(Connection):
    """One console. Publishes under ``eos.*`` by default."""

    #: Eos is event-driven: it dumps state on connect and then says nothing at
    #: all until something changes. A quiet console during a scene change is
    #: normal, so this is generous. Going STALE after 30 seconds would have the
    #: status light flickering all night for no reason.
    stale_after = 120.0

    #: How long cue fires may keep arriving with no cue-state traffic behind
    #: them before the OSC user binding is blamed for it. The measurement it
    #: comes from is 25 seconds of fires with zero cue state; this is under
    #: that so a real mismatch is caught within one scene, and far enough above
    #: the ~1 Hz stream that a healthy desk can never trip it.
    user_mismatch_silence = 20.0

    #: TCP keepalive on the console socket: seconds of silence before the
    #: first probe, seconds between unanswered probes, and how many go
    #: unanswered before the socket gives up. 15 + 3 x 10 = 45 s.
    #:
    #: After the handshake Wer only reads this socket, so a desk that vanished
    #: without closing its end -- a power cut, a hung desk power-cycled, a blue
    #: screen -- left recv timing out quietly for ever. The link read Stale and
    #: stayed there; the rebooted desk does not dial clients, so no cue fire,
    #: marker or chapter arrived for the rest of the tech, with the show
    #: carrying on and nobody looking at Wer. An unanswered probe turns that
    #: silence into an error, and a desk that has rebooted in the meantime
    #: answers one with a reset. Either way recv raises and the supervisor
    #: redials.
    #:
    #: This is the operating system's TCP, not OSC: Eos never sees a probe.
    #: Assumed, and not yet observed against a real desk: that its TCP stack
    #: answers probes (RFC 1122 requires it) and resets one for a connection
    #: it no longer has, and that Windows then fails the next recv. Nor is it
    #: settled which error that recv fails with, and one of the candidates
    #: looks just like an ordinary timeout; see _is_read_timeout.
    #:
    #: Windows' own defaults do not help, measured on the development machine:
    #: keepalive off, and once on, 7200 s of silence before the first probe.
    #: A blip shorter than the ten probes (30 s) costs no reconnect; a desk
    #: gone for good is noticed well inside the 120 s it takes to read Stale.
    keepalive_idle = 15
    keepalive_interval = 3
    keepalive_probes = 10

    def __init__(
        self,
        connection_id: str,
        bus: DataBus,
        settings: EosSettings | None = None,
        *,
        namespace: str = "eos",
        on_cue_fired: Callable[[CueFired], None] | None = None,
    ) -> None:
        super().__init__(connection_id, bus)
        self.settings = settings or EosSettings()
        self.namespace = namespace
        self.parser = EosParser(namespace=namespace)
        self._on_cue_fired = on_cue_fired
        #: The /wer/ commands a macro on the desk sends. They come down this
        #: link with the console's own traffic; see connections/osc_control.py.
        self.commands = CommandDispatcher()
        #: Set once the binding is caught starving the cue-state stream.
        self.user_mismatch_seen = False
        self.driving_user = 0
        self._cue_state_at: float | None = None
        self._binding_watch_from: float | None = None
        #: Which cues have fired with no cue state behind them. A set, not a
        #: count, because re-firing one cue is the healthy case -- see
        #: _check_user_binding.
        self._starved_cues: set[str] = set()
        #: How many times the decoder has raised something other than
        #: OscDecodeError on this connection run. Only the first gets a
        #: traceback; see _consume.
        self._decoder_faults = 0
        self._decoder_fault_said_at = 0.0
        #: Whether this connection run has already said that somebody else is
        #: sending to the port. See _record_noise.
        self._said_who_else_is_here = False
        #: Only the cue-state watch reads this. Overridable so a captured
        #: session can be replayed against its own recorded timestamps rather
        #: than against a threshold shrunk to make a test quick.
        self._clock = time.perf_counter
        self._socket: socket.socket | None = None

    def forget_published(self) -> list[str]:
        """Drop every key this connection put on the bus, and reset the parser.

        Deliberately NOT called when the link merely drops. A console reboot
        mid-show should leave the last known cue on screen going stale, not
        blank the overlay -- that is the existing design and it is the right
        one. This is for when the data is about to be answered for by a
        *different* console, where keeping the old desk's values would be
        straightforwardly wrong.
        """
        self.parser.reset()
        removed = self.bus.clear_source(self.id)
        if removed:
            log.info("Forgot %d key(s) from the previous console", len(removed))
        return removed

    def _check_user_binding(self, message: OscMessage | OscBundle) -> None:
        """Say so when the OSC user binding is starving the cue-state stream.

        Eos sends the ongoing cue-state stream only to the user it is bound
        to, or to everyone when that is 0. Cue FIRE events arrive regardless,
        which is what makes this so hard to spot: the live cue limps forward
        on fires alone while the next cue, the fade time and everything else
        never move at all. Measured on a real desk -- announcing user 1 to a
        console being run by user 2 gave 0 ongoing cue-state messages in 25
        seconds; user 2 or user 0 gave 26.

        That silence is the test, and it has to be. The obvious check --
        "another user's command line arrived, so we are bound to the wrong
        one" -- is simply wrong, and it fired on our own captures: a desk
        broadcasts EVERY user's command line to every client, so
        captures/slow-fade.raw, taken bound to user 1 with a perfect 1 Hz cue
        stream running, carries twenty /eos/out/user/2/cmd messages. It warned
        that "the next cue will never update" about a session where the next
        cue was updating fine, and blamed whichever other operator happened to
        touch a keypad first.

        The starved fires must name DIFFERENT cues. A healthy desk produces a
        fire with no cue state behind it every time the operator does "Go To
        Cue <the cue that is already live>": the state has not changed, so the
        desk sends nothing. tests/fixtures/eos/cmdline-and-cue-fire.jsonl is
        exactly that press, and two of them after any quiet stretch used to
        trip this warning on a correctly bound desk. A show advancing through
        different cues with no state behind it is the real signature; the same
        cue re-fired is not.
        """
        if self.user_mismatch_seen or self.settings.user == 0:
            return

        now = self._clock()
        if self._binding_watch_from is None:
            self._binding_watch_from = now

        messages = (
            message.messages() if isinstance(message, OscBundle) else (message,)
        )
        fired: set[str] = set()
        for element in messages:
            address = element.address
            if _CUE_STATE.match(address):
                # The stream is arriving. Whatever else is going on, the
                # binding is not the problem.
                self._cue_state_at = now
                self._starved_cues.clear()
                return
            if (fire := _CUE_FIRE.match(address)) is not None:
                fired.add(f"{fire.group(1)}/{fire.group(2)}")
                continue
            if (match := _OUT_USER_CMD.match(address)) is not None:
                driving = int(match.group(1))
                if driving not in (0, self.settings.user, self.driving_user):
                    self.driving_user = driving

        if not fired:
            return
        self._starved_cues |= fired
        silent_for = now - (self._cue_state_at or self._binding_watch_from)
        if len(self._starved_cues) < 2 or silent_for < self.user_mismatch_silence:
            return

        self.user_mismatch_seen = True
        log.warning(
            "%d different cues have fired in the last %.0f seconds and this "
            "console has sent no cue state at all. Eos sends cue updates only "
            "to the user it is bound to, so with Wer bound to user %d the "
            "overlay will limp along on cue-fire events and the next cue will "
            "never update. "
            "Set OSC user to 0 (any) on the Eos tab, which is what a recording "
            "wants.%s",
            len(self._starved_cues), silent_for, self.settings.user,
            f" The desk is being driven by user {self.driving_user}."
            if self.driving_user else "",
        )
        self.publish(f"{self.namespace}.user.mismatch", self.driving_user)

    @property
    def display_name(self) -> str:
        s = self.settings
        if s.transport is EosTransport.UDP:
            return f"Eos (UDP {s.port})"
        return f"Eos ({s.host}:{s.port})"

    # ---------------------------------------------------------------- running

    def _run_once(self) -> None:
        self.publish(f"{self.namespace}.connected", False)
        # A reconnect starts the cue-state clock again. Carrying the silence
        # across would blame the binding for a console that was merely off.
        self._cue_state_at = None
        self._binding_watch_from = None
        self._starved_cues.clear()
        # A fresh link gets a fresh traceback too: the first decoder fault of
        # each run is worth a full one, the rest are counted (see _consume).
        self._decoder_faults = 0
        self._decoder_fault_said_at = 0.0
        self._said_who_else_is_here = False
        if self.settings.transport is EosTransport.TCP:
            self._run_tcp()
        else:
            self._run_udp()

    def _run_tcp(self) -> None:
        settings = self.settings
        framer = (
            SlipFramer() if settings.framing is EosFraming.SLIP else LengthPrefixFramer()
        )

        log.info("Eos: connecting to %s:%d", settings.host, settings.port)
        try:
            sock = socket.create_connection((settings.host, settings.port), timeout=5.0)
        except OSError as exc:
            self._set_state(ConnectionState.ERROR, _explain_connect_error(exc))
            # Return rather than raise: the supervisor treats a return as a
            # normal end and applies backoff, which is exactly what a console
            # that is switched off should get.
            time.sleep(0.2)
            return

        try:
            why_not = _enable_keepalive(
                sock, self.keepalive_idle, self.keepalive_interval,
                self.keepalive_probes,
            )
            if why_not is not None:
                # The link still works as it always did, but a desk that loses
                # power would go unnoticed, and nobody is watching Wer to
                # notice for it. Say so, and say what to do about it.
                log.warning(
                    "Eos: could not set TCP keepalive on the console socket "
                    "(%s). If the desk loses power or crashes, Wer will not "
                    "notice and will not reconnect by itself; press Disconnect "
                    "and then Connect on the Eos tab once the desk is back.",
                    why_not,
                )
            self._socket = sock
            # Connected is NOT Live. Eos accepts a socket and sends nothing at
            # all when its OSC output is disabled, so Live has to wait for
            # real data.
            self._set_state(
                ConnectionState.WAITING,
                "connected; waiting for OSC. Is OSC TX enabled on the console?",
            )
            self._handshake(sock, framer)
            sock.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(65536)
                except OSError as exc:
                    if _is_read_timeout(exc):
                        # A quiet second: Eos says nothing between changes.
                        self.refresh_staleness()
                        continue
                    if self._stop.is_set():
                        # stop() shut the socket to wake this recv.
                        return
                    # With keepalive on, this is where a desk that vanished
                    # without closing its end finally shows itself. Worth more
                    # than a state change: the morning-after question is "why
                    # are there no chapters after 21:40?"
                    log.warning(
                        "Eos: lost the connection to %s:%d (%s). Reconnecting.",
                        settings.host, settings.port, exc,
                    )
                    self._set_state(ConnectionState.ERROR, str(exc))
                    return
                if not chunk:
                    if self._stop.is_set():
                        # A shut socket can wake this recv with an empty read
                        # as well as with an error.
                        return
                    # Eos quitting or restarting closes its end properly. That
                    # used to leave only a DEBUG state change, so the log had
                    # a line for a desk that vanished and none for one that
                    # hung up.
                    log.warning(
                        "Eos: the console at %s:%d closed the connection. "
                        "Reconnecting.",
                        settings.host, settings.port,
                    )
                    self._set_state(
                        ConnectionState.DISCONNECTED, "console closed the connection"
                    )
                    return
                try:
                    packets = framer.feed(chunk)
                except OscDecodeError as exc:
                    # Almost always a framing mismatch rather than corruption,
                    # and a mismatch ends every redial the same way, so the log
                    # has to name the setting to look at. That is the console's
                    # OSC TCP Format, not the port: a real Ion XE answered 3032
                    # in OSC 1.1, so the line must not send anyone to the port
                    # number to work out which framing is right.
                    log.warning(
                        "Eos: could not read the stream from %s:%d (%s). Check "
                        "the console's OSC TCP Format against the TCP framing "
                        "set for this console; the port does not decide it. "
                        "Reconnecting.",
                        settings.host, settings.port, exc,
                    )
                    self._set_state(ConnectionState.ERROR, f"framing: {exc}")
                    return
                for packet in packets:
                    self._consume(packet)
        finally:
            self.publish(f"{self.namespace}.connected", False)
            self._socket = None
            try:
                sock.close()
            except OSError:
                pass

    def _run_udp(self) -> None:
        settings = self.settings
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((settings.bind, settings.port))
        except OSError as exc:
            self._set_state(ConnectionState.ERROR, _explain_bind_error(exc, settings.port))
            time.sleep(0.5)
            return

        self._socket = sock
        self._set_state(
            ConnectionState.WAITING,
            f"listening on UDP {settings.port}; "
            "the console must be set to transmit here",
        )
        try:
            sock.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    datagram, _peer = sock.recvfrom(65536)
                except socket.timeout:
                    self.refresh_staleness()
                    continue
                except OSError as exc:
                    self._set_state(ConnectionState.ERROR, str(exc))
                    return
                self._consume(datagram)
        finally:
            self.publish(f"{self.namespace}.connected", False)
            self._socket = None
            try:
                sock.close()
            except OSError:
                pass

    def _handshake(self, sock: socket.socket, framer: object) -> None:
        """Bind the OSC user and subscribe. Both are opt-in.

        Wer never drives the console. Neither of these does: they select whose
        data we receive. No cue is fired, no level is set.
        """
        def send(raw: bytes) -> None:
            framed = (
                SlipFramer.frame(raw)
                if isinstance(framer, SlipFramer)
                else LengthPrefixFramer.frame(raw)
            )
            sock.sendall(framed)

        if self.settings.send_user:
            send(encode_message("/eos/user", self.settings.user))
            self.record(f"/eos/user {self.settings.user}", direction="tx")
        if self.settings.subscribe:
            send(encode_message("/eos/subscribe", 1))
            self.record("/eos/subscribe 1", direction="tx")

    # -------------------------------------------------------------- consuming

    def _consume(self, packet: bytes) -> None:
        try:
            decoded = decode_packet(packet)
        except OscDecodeError as exc:
            # One bad packet must not drop the connection. Record it so it is
            # visible in the monitor and carry on.
            self._record_noise(f"[undecodable] {exc}", packet)
            return
        except Exception as exc:  # noqa: BLE001
            # A decoder that raises anything else is a bug in this program, and
            # the log should say so loudly -- but not by taking the console off
            # the air for the rest of the act. This clause exists because
            # struct.error used to escape from a packet truncated inside a
            # numeric argument, and the supervisor read it as a dead console.
            #
            # Loudly ONCE. Whatever provokes a decoder fault is a property of
            # the sender, so it repeats: a peer resending it turns this into a
            # log firehose. Measured at 180 KiB of traceback per packet, which
            # at a few dozen packets a second rolls all five 5 MiB log files
            # (core/logging_setup.py) inside ten seconds -- destroying the
            # session's diagnostics on the one night you would want to read
            # them. So: one traceback per connection run, then a counted
            # one-liner no more than once every few seconds.
            self._note_decoder_fault(exc, len(packet))
            self._record_noise(f"[undecodable] {exc}", packet)
            return

        messages = (
            list(decoded.messages()) if isinstance(decoded, OscBundle) else [decoded]
        )
        commands = [m for m in messages if m.address.startswith(COMMAND_PREFIX)]
        console = is_console_traffic(decoded)
        if not console and not commands:
            # Somebody else's OSC. Worth seeing in the monitor -- it is the
            # evidence when a port is being shared -- but it is not the desk,
            # and treating it as such kept a silent console reading Live.
            self._record_noise(str(decoded), packet)
            return

        if console:
            self.record(str(decoded), raw=packet, size=len(packet))
        else:
            # Only /wer/ commands, which is what a macro on the desk sends.
            # Acted on and shown, but neither proof that the desk is alive nor
            # noise. Over UDP anything on the network can send one, and counting
            # it would let a phone keep a silent console reading Live -- the
            # failure _record_noise exists for. In the capture the desk's own
            # /eos/out/event/macro/N arrived just before each command, and that
            # counts. These used to take the branch above, so a macro's command
            # scrolled past as somebody else's traffic and nothing acted on it.
            self.record(
                str(decoded), raw=packet, size=len(packet),
                liveness=False, noise=False,
            )
        for message in commands:
            self.commands.dispatch(message, source=self.display_name)

        if console:
            self._check_user_binding(decoded)
            result = self.parser.handle(decoded)
            for update in result.updates:
                self.publish(update.key, update.value, stale_after=update.stale_after)
            for event in result.events:
                log.info("Cue fired: %s", event.display)
                if self._on_cue_fired is not None:
                    try:
                        self._on_cue_fired(event)
                    except Exception:  # noqa: BLE001
                        log.exception("Cue-fired handler raised")

        if self.status.state is ConnectionState.LIVE:
            self.publish(f"{self.namespace}.connected", True)

    #: Seconds between the counted follow-up lines for repeated decoder
    #: faults. The first fault always gets its traceback; this only limits how
    #: often the running total is restated.
    decoder_fault_report_every = 5.0

    def _note_decoder_fault(self, exc: BaseException, size: int) -> None:
        """Log a decoder bug in full once, then count it.

        Every packet is still recorded in the monitor with its own reason, so
        nothing is hidden -- the raw bytes of number 4,000 are there to look
        at. What is bounded is the traceback, which is the expensive part.
        """
        self._decoder_faults += 1
        if self._decoder_faults == 1:
            log.exception("OSC decoder raised on a %d-byte packet", size)
            self._decoder_fault_said_at = time.perf_counter()
            return
        now = time.perf_counter()
        if now - self._decoder_fault_said_at < self.decoder_fault_report_every:
            return
        self._decoder_fault_said_at = now
        log.error(
            "OSC decoder has now raised on %d packets from this connection "
            "(latest: %s: %s). Traceback logged once, above; the packets "
            "themselves are in the Monitor tab.",
            self._decoder_faults, type(exc).__name__, exc,
        )

    def _record_noise(self, summary: str, packet: bytes) -> None:
        """Monitor it, but do not let it pass for the console being alive.

        Say once per connection that this is happening. The packet counter on
        the Connections panel deliberately ignores foreign traffic, so a
        shared port shows "0 packets" beside a monitor pane full of rx rows;
        without this line the only place that discrepancy is explained is the
        raw bytes themselves. Once per run, not per packet -- the whole point
        of a port clash is that it repeats.
        """
        self.record(summary, raw=packet, size=len(packet), liveness=False)
        if not self._said_who_else_is_here:
            self._said_who_else_is_here = True
            log.info(
                "Traffic that is not Eos OSC is arriving on %s. It shows in "
                "the Monitor tab but does not count as the desk talking, so "
                "the packet counter stays at the console's own traffic -- a "
                "shared port reads 0 packets with the monitor scrolling. "
                "First one: %s",
                self.display_name, summary,
            )

    def refresh_staleness(self) -> None:
        """Re-evaluate LIVE vs STALE, and keep eos.connected honest with it.

        The base class handles the state; this keeps the bus key in step. It
        used to be published True on every packet and False only when the
        socket actually broke, so a console that stopped sending while the
        connection stayed open left it true indefinitely -- and any widget
        conditioned on it stayed lit over data that had stopped arriving.

        That is the failure worth engineering against here. A console that
        dies is loud: there is a board operator sitting at it who will know
        within seconds. A network blip while the desk is perfectly fine is
        silent -- the operator keeps calling cues, nobody has any reason to
        look at Wer, and the recording quietly claims the last cue it heard
        for the rest of the act.
        """
        was = self.status.state
        super().refresh_staleness()
        now = self.status.state
        if now is was:
            return
        if now is ConnectionState.STALE:
            self.publish(f"{self.namespace}.connected", False)
        elif now is ConnectionState.LIVE:
            self.publish(f"{self.namespace}.connected", True)

    def _wake(self) -> None:
        """Break a blocking recv so stop() returns promptly."""
        sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _is_read_timeout(exc: OSError) -> bool:
    """True only for the read timeout Python raises by itself.

    That timeout is the ordinary state of a desk between cues. It is a
    TimeoutError (socket.timeout is another name for the class) with no error
    code, because Python raised it when its own wait ran out, not because the
    operating system reported anything.

    An error the operating system reports can be a TimeoutError too: Python
    builds WSAETIMEDOUT (10060) as one, with the code set. Which error a read
    gets on Windows when keepalive gives up on a desk is not known for
    certain -- Microsoft's SO_KEEPALIVE page says WSAENETRESET, reports from
    the field say WSAETIMEDOUT -- so both must end the run. Catching
    socket.timeout took the second for a quiet second: nothing logged, no
    redial, and the dead socket read again for the rest of the tech, which is
    the failure keepalive was switched on to end.

    Checked on the development machine's Python 3.12: the one-second timeout
    comes back as TimeoutError('timed out') with errno None, and
    OSError(None, ..., None, 10060) as TimeoutError with errno 10060.
    """
    return isinstance(exc, TimeoutError) and exc.errno is None


def _enable_keepalive(
    sock: socket.socket, idle: int, interval: int, probes: int
) -> str | None:
    """Switch TCP keepalive on with these timers. Returns why not, or None.

    The per-option calls come first because they set the probe count as well,
    and can be read back. An older Windows may refuse them, so the older
    SIO_KEEPALIVE_VALS ioctl follows. That cannot set a count, which is then
    assumed to stay at the system default of 10 -- what this machine reports
    -- so the timings match.

    Keepalive switched on with its timers refused is reported as a failure:
    Windows' default of two hours before the first probe is as good as off.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError as exc:
        return f"SO_KEEPALIVE refused: {exc}"
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, probes)
        return None
    except (AttributeError, OSError) as first:
        ioctl = getattr(sock, "ioctl", None)
        control = getattr(socket, "SIO_KEEPALIVE_VALS", None)
        if ioctl is None or control is None:
            return f"keepalive timers refused: {first}"
        try:
            ioctl(control, (1, idle * 1000, interval * 1000))
        except OSError as second:
            return f"keepalive timers refused: {first}; then {second}"
        return None


def _explain_connect_error(exc: OSError) -> str:
    """Turn a socket errno into something a lighting designer can act on."""
    text = str(exc)
    if isinstance(exc, socket.timeout) or "timed out" in text:
        return "no response — check the IP address and that both machines are on the same subnet"
    if getattr(exc, "winerror", None) == 10061 or "refused" in text.lower():
        return "connection refused — Eos may not be running, or OSC TCP may be disabled"
    if getattr(exc, "winerror", None) == 10065 or "unreachable" in text.lower():
        return "host unreachable — check the network and the static IP"
    return text


def _explain_bind_error(exc: OSError, port: int) -> str:
    if getattr(exc, "winerror", None) in (10048, 98):
        return f"UDP port {port} is already in use by another application"
    if getattr(exc, "winerror", None) in (10013, 13):
        return f"permission denied binding UDP port {port}"
    return str(exc)
