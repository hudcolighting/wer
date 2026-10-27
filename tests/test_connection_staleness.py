"""eos.connected must say whether the console can still be heard.

The failure this guards is deliberately the quiet one. A console that dies is
loud -- there is a board operator sitting at it who knows within seconds. A
network blip while the desk is perfectly fine is silent: the operator keeps
calling cues, nobody has any reason to look at Wer, and the recording quietly
claims the last cue it heard for the rest of the act.

How it is told differs by transport. Over UDP silence is all there is to go
on, so two minutes of it reads Stale. Over TCP the socket's keepalive probes
are the check, and silence decides nothing: Eos says nothing at all between
cues while nobody touches the desk, and on two full-dress takes the
two-minute rule read a healthy TCP link Stale again and again, blanking the
time in cue each time. A TCP socket that refused keepalive has only silence
to go on, and keeps the rule.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import wer.connections.eos as eos
from wer.connections.base import ConnectionState
from wer.connections.eos import EosConnection, EosSettings, EosTransport
from wer.connections.osc import LengthPrefixFramer, encode_message
from wer.core.databus import DataBus


def _live_connection(stale_after: float = 0.2):
    """A UDP link that has heard the desk once."""
    bus = DataBus()
    connection = EosConnection(
        "eos", bus, EosSettings(transport=EosTransport.UDP)
    )
    connection.stale_after = stale_after
    connection._set_state(ConnectionState.LIVE)
    connection.status.note_packet(1)
    connection.publish("eos.connected", True)
    return connection, bus


def test_connected_goes_false_when_the_desk_stops_talking() -> None:
    connection, bus = _live_connection()
    assert bus.value("eos.connected") is True

    time.sleep(0.35)
    connection.refresh_staleness()

    assert connection.status.state is ConnectionState.STALE
    assert bus.value("eos.connected") is False


def test_connected_comes_back_when_the_desk_does() -> None:
    connection, bus = _live_connection()
    time.sleep(0.35)
    connection.refresh_staleness()
    assert bus.value("eos.connected") is False

    connection.status.note_packet(1)
    connection.refresh_staleness()

    assert connection.status.state is ConnectionState.LIVE
    assert bus.value("eos.connected") is True


def test_a_healthy_connection_is_left_alone() -> None:
    """refresh_staleness runs twice a second from the UI. It must not churn
    the bus, or every subscriber repaints for nothing."""
    connection, bus = _live_connection(stale_after=30.0)
    changes = []
    bus.subscribe("eos.connected", lambda entry: changes.append(entry.value))

    for _ in range(10):
        connection.refresh_staleness()

    assert connection.status.state is ConnectionState.LIVE
    assert changes == [], "published on a connection that had not changed state"


def test_nothing_is_published_before_a_connection_exists() -> None:
    """A connection that was never live has nothing to say about staleness."""
    bus = DataBus()
    connection = EosConnection("eos", bus, EosSettings())
    connection.refresh_staleness()
    assert bus.value("eos.connected") is None


# ------------------------------------------------------------------ over TCP


def wait_until(predicate, timeout: float = 5.0) -> bool:
    """Poll until true. Threads make fixed sleeps either flaky or slow."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@contextmanager
def desk_that_speaks_once() -> Iterator[int]:
    """A TCP peer that accepts, sends one Eos message, and then says nothing,
    as a desk does between cues. Yields its port."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    accepted: list[socket.socket] = []

    def serve() -> None:
        try:
            client, _ = server.accept()
        except OSError:
            return
        accepted.append(client)
        client.sendall(
            LengthPrefixFramer.frame(encode_message("/eos/out/show/name", "Comedy EOS"))
        )

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield server.getsockname()[1]
    finally:
        for client in accepted:
            client.close()
        server.close()


def _quiet_tcp_link(port: int, bus: DataBus) -> tuple[EosConnection, list[float]]:
    """A TCP connection to the desk with a two-minute rule shrunk to 0.2 s,
    counting the quiet seconds its own read loop waits out."""
    connection = EosConnection(
        "eos", bus,
        EosSettings(host="127.0.0.1", port=port, send_user=False, subscribe=False),
    )
    connection.stale_after = 0.2
    quiet_seconds: list[float] = []
    refresh = connection.refresh_staleness

    def counted() -> None:
        quiet_seconds.append(time.perf_counter())
        refresh()

    connection.refresh_staleness = counted  # type: ignore[method-assign]
    return connection, quiet_seconds


def test_a_quiet_tcp_link_stays_live_while_keepalive_watches_it() -> None:
    """Hudson's report: on long cues the time in cue lost its number, and
    typing on the desk sometimes brought it back. The desk had only gone
    quiet; the socket was fine, and over two full-dress takes it never
    dropped in three and a half hours."""
    bus = DataBus()
    with desk_that_speaks_once() as port:
        connection, quiet_seconds = _quiet_tcp_link(port, bus)
        connection.start()
        try:
            assert wait_until(lambda: connection.status.state is ConnectionState.LIVE)
            assert bus.value("eos.connected") is True
            heard = len(quiet_seconds)
            assert wait_until(lambda: len(quiet_seconds) > heard), (
                "the read loop never waited out a quiet second"
            )
            # And from another thread, as the UI calls it twice a second.
            connection.refresh_staleness()

            assert connection.status.state is ConnectionState.LIVE, (
                "a quiet desk on an open, probed socket was taken for a lost one"
            )
            assert bus.value("eos.connected") is True
        finally:
            connection.stop()

    assert bus.value("eos.connected") is False, "the link ended and still read connected"
    assert not connection._keepalive_watching.is_set(), (
        "the next socket would inherit this one's keepalive"
    )


def test_a_tcp_link_that_refused_keepalive_still_goes_stale_when_quiet(
    monkeypatch,
) -> None:
    """Without probes nothing else would ever notice a desk that lost power,
    so silence has to go on counting."""
    monkeypatch.setattr(eos, "_enable_keepalive", lambda *_args: "refused here")
    bus = DataBus()
    with desk_that_speaks_once() as port:
        connection, _quiet_seconds = _quiet_tcp_link(port, bus)
        connection.start()
        try:
            assert wait_until(lambda: bus.value("eos.connected") is True)
            assert wait_until(
                lambda: connection.status.state is ConnectionState.STALE
            ), "a socket with no keepalive never read Stale"
            assert bus.value("eos.connected") is False
        finally:
            connection.stop()


# ------------------------------------------------------------------ over UDP


def test_a_quiet_udp_link_still_reads_stale_while_it_runs() -> None:
    """The tests above judge a UDP link that was never started. A running one
    has to keep the rule as well: datagrams simply stop, there is nothing to
    probe, and silence is the only sign that the desk has gone."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    bus = DataBus()
    connection = EosConnection(
        "eos", bus,
        EosSettings(transport=EosTransport.UDP, port=port, bind="127.0.0.1"),
    )
    connection.stale_after = 0.2
    desk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    connection.start()
    try:
        assert wait_until(
            lambda: connection.status.state is ConnectionState.WAITING
        ), "precondition: the link is listening"
        desk.sendto(
            encode_message("/eos/out/show/name", "Comedy EOS"), ("127.0.0.1", port)
        )
        assert wait_until(lambda: bus.value("eos.connected") is True)
        assert not connection._keepalive_watching.is_set(), (
            "a UDP link was given the exemption only keepalive earns"
        )
        assert wait_until(
            lambda: connection.status.state is ConnectionState.STALE
        ), "a running UDP link that went quiet never read Stale"
        assert bus.value("eos.connected") is False
    finally:
        connection.stop()
        desk.close()
