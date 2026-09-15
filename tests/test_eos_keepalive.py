"""A TCP console link notices a desk that vanished without closing the socket.

After the handshake Wer only reads from the desk. A desk that lost power or
crashed sends no FIN and no reset, so recv went on timing out quietly for the
rest of the tech: the link read Stale and stayed there, the rebooted desk does
not dial clients, and no cue fire, marker or chapter arrived after it -- while
the show carried on and nobody had any reason to look at Wer.

A peer on loopback always answers keepalive probes, so the vanishing itself
cannot be staged here. What is pinned is that the probes are asked for, with
timers that notice a dead desk inside a scene; that failing to ask is said out
loud; that the errors a read can get when the probes go unanswered end the run
and are logged, while Python's own quiet-second timeout does not; and that
every other way the link ends leaves a line in the log, except a Disconnect
somebody pressed.

Errors a real socket will not produce on demand -- the one keepalive gives up
with, the one a reader gets from a socket stop() has just shut -- are staged
with DeskSocket instead.
"""

from __future__ import annotations

import errno
import logging
import socket
import struct
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

import wer.connections.eos as eos
from wer.connections.base import ConnectionState
from wer.connections.eos import EosConnection, EosSettings, _enable_keepalive
from wer.core.databus import DataBus


def wait_until(predicate, timeout: float = 5.0) -> bool:
    """Poll until true. Threads make fixed sleeps either flaky or slow."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@contextmanager
def silent_console() -> Iterator[tuple[int, list[socket.socket]]]:
    """A TCP peer that accepts one connection and says nothing on it.

    Yields its port and the list the accepted socket lands in, so a test can
    do something to the desk's end.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    accepted: list[socket.socket] = []

    def accept() -> None:
        try:
            client, _ = server.accept()
            accepted.append(client)
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield server.getsockname()[1], accepted
    finally:
        for client in accepted:
            client.close()
        server.close()


def connect(port: int, *, start: bool = True) -> EosConnection:
    # No handshake: the peer is not a desk, and the socket is what matters.
    connection = EosConnection(
        "eos", DataBus(),
        EosSettings(host="127.0.0.1", port=port, send_user=False, subscribe=False),
    )
    if start:
        connection.start()
    return connection


def warnings_from_eos(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.name == "wer.connections.eos" and r.levelno >= logging.WARNING
    ]


def test_the_console_socket_asks_for_keepalive_probes() -> None:
    """Windows' own default is none at all, and two hours' silence before the
    first probe once they are switched on."""
    with silent_console() as (port, _accepted):
        connection = connect(port)
        try:
            assert wait_until(
                lambda: connection.status.state is ConnectionState.WAITING
            )
            sock = connection._socket
            assert sock is not None
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), (
                "nothing would ever notice a desk that lost power"
            )
            tcp = socket.IPPROTO_TCP
            assert sock.getsockopt(tcp, socket.TCP_KEEPIDLE) == connection.keepalive_idle
            assert (
                sock.getsockopt(tcp, socket.TCP_KEEPINTVL)
                == connection.keepalive_interval
            )
            assert sock.getsockopt(tcp, socket.TCP_KEEPCNT) == connection.keepalive_probes
        finally:
            connection.stop()

    gives_up_after = (
        connection.keepalive_idle
        + connection.keepalive_interval * connection.keepalive_probes
    )
    assert gives_up_after < connection.stale_after, (
        "a desk that is gone should be noticed before the link even reads Stale"
    )


class OlderWindowsSocket:
    """Stands in for a Windows that refuses the per-option keepalive calls.

    This machine accepts them, so the fallback cannot be reached with a real
    socket here.
    """

    def __init__(self) -> None:
        self.keepalive = 0
        self.ioctls: list[tuple[int, object]] = []

    def setsockopt(self, level: int, option: int, value: int) -> None:
        if level == socket.IPPROTO_TCP:
            raise OSError(10042, "An unknown, invalid or unsupported option")
        self.keepalive = value

    def ioctl(self, control: int, value: object) -> None:
        self.ioctls.append((control, value))


def test_an_older_windows_still_gets_the_same_timers() -> None:
    sock = OlderWindowsSocket()
    assert _enable_keepalive(sock, 15, 3, 10) is None
    assert sock.keepalive == 1
    assert sock.ioctls == [(socket.SIO_KEEPALIVE_VALS, (1, 15000, 3000))]


class OlderWindowsRefusingTheIoctlToo(OlderWindowsSocket):
    def ioctl(self, control: int, value: object) -> None:
        raise OSError(10045, "The attempted operation is not supported")


def test_timers_refused_both_ways_come_back_as_a_failure_naming_both() -> None:
    """Keepalive switched on but left at Windows' two hours before the first
    probe is as good as off, so this has to be a failure -- and whoever reads
    the warning needs both refusals, not just the last."""
    reason = _enable_keepalive(OlderWindowsRefusingTheIoctlToo(), 15, 3, 10)
    assert reason is not None, "timers nobody accepted were reported as set"
    assert "10042" in reason and "10045" in reason


class KeepaliveRefusedSocket:
    def setsockopt(self, level: int, option: int, value: int) -> None:
        raise OSError(10042, "An unknown, invalid or unsupported option")


def test_keepalive_refused_outright_comes_back_as_a_failure() -> None:
    reason = _enable_keepalive(KeepaliveRefusedSocket(), 15, 3, 10)
    assert reason is not None
    assert reason.startswith("SO_KEEPALIVE refused")


def test_a_socket_that_refuses_keepalive_is_said_out_loud(
    monkeypatch, caplog
) -> None:
    """Not fatal: the link works as it always did. But without probes a desk
    that loses power leaves Wer stuck until someone reconnects by hand, and
    nobody is watching Wer to notice. The log has to say so, and say what to
    do."""
    monkeypatch.setattr(eos, "_enable_keepalive", lambda *_args: "refused here")
    with (
        silent_console() as (port, _accepted),
        caplog.at_level(logging.WARNING, logger="wer.connections.eos"),
    ):
        connection = connect(port)
        try:
            assert wait_until(
                lambda: connection.status.state is ConnectionState.WAITING
            ), "a refused keepalive must not stop the connection working"
        finally:
            connection.stop()

    said = [r.getMessage() for r in caplog.records if "keepalive" in r.getMessage()]
    assert len(said) == 1
    assert "refused here" in said[0]
    assert "Disconnect" in said[0]


class DeskSocket:
    """Stands in for the console socket when a read has to fail a given way.

    Given ``fails_with``, every read raises it, as a socket the operating
    system has given up on does. Otherwise the desk has nothing to say: a read
    blocks, as a real one does, until stop() shuts the socket, and then gives
    back ``woken_by_stop`` -- an empty read or an error.
    """

    def __init__(
        self,
        *,
        fails_with: OSError | None = None,
        woken_by_stop: bytes | OSError = b"",
    ) -> None:
        self.fails_with = fails_with
        self.woken_by_stop = woken_by_stop
        self.reads = 0
        self._shut = threading.Event()

    def setsockopt(self, *_args: object) -> None:
        """Keepalive is accepted; there is no network here to probe."""

    def settimeout(self, _timeout: float) -> None:
        pass

    def sendall(self, _data: bytes) -> None:
        pass

    def recv(self, _size: int) -> bytes:
        self.reads += 1
        if self.fails_with is not None:
            # Only so that a build which goes on reading the dead socket does
            # not spin a core for the whole of the test's wait.
            time.sleep(0.01)
            raise self.fails_with
        self._shut.wait()
        if isinstance(self.woken_by_stop, OSError):
            raise self.woken_by_stop
        return self.woken_by_stop

    def shutdown(self, _how: int) -> None:
        self._shut.set()

    def close(self) -> None:
        self._shut.set()


def desks_dialled(monkeypatch, **behaviour: object) -> list[DeskSocket]:
    """Make every dial reach a new DeskSocket behaving like this.

    Returns the list they land in, so a test can count the redials.
    """
    dialled: list[DeskSocket] = []

    def create_connection(_address: object, timeout: float | None = None) -> DeskSocket:
        dialled.append(DeskSocket(**behaviour))
        return dialled[-1]

    monkeypatch.setattr(eos.socket, "create_connection", create_connection)
    return dialled


@pytest.mark.parametrize(
    "code",
    [
        pytest.param(errno.ETIMEDOUT, id="WSAETIMEDOUT"),
        pytest.param(errno.ENETRESET, id="WSAENETRESET"),
    ],
)
def test_a_desk_keepalive_gives_up_on_is_logged_as_lost_and_redialled(
    monkeypatch, caplog, code: int
) -> None:
    """Which error a read gets on Windows when keepalive gives up is not
    settled: Microsoft documents WSAENETRESET, field reports give
    WSAETIMEDOUT. Both have to end the run.

    WSAETIMEDOUT is the one that got through. Python builds it as a
    TimeoutError, the class of its own one-second read timeout, and the read
    loop caught socket.timeout and took it for a quiet second: nothing logged,
    no redial, and the dead socket read over and over for the rest of the
    tech -- the very failure keepalive was switched on to end.
    """
    # On Windows the code is taken as the winerror, elsewhere as the errno.
    error = OSError(code, "keepalive gave up", None, code)
    dialled = desks_dialled(monkeypatch, fails_with=error)
    with caplog.at_level(logging.WARNING, logger="wer.connections.eos"):
        connection = connect(3032)
        try:
            assert wait_until(lambda: len(dialled) >= 2), (
                "the supervisor never redialled a desk keepalive had given up on"
            )
        finally:
            connection.stop()

    assert dialled[0].reads == 1, "the dead socket was read again, not given up"
    assert any(
        "lost the connection" in r.getMessage() for r in warnings_from_eos(caplog)
    ), "a desk keepalive gave up on went unlogged"


def test_a_desk_that_is_only_quiet_is_not_taken_for_a_lost_one(caplog) -> None:
    """The other side of that line. Python's own read timeout is a
    TimeoutError too, and it is the ordinary state of a desk between cues: on
    a real socket it has to go on meaning "nothing yet", not end the run."""
    with (
        silent_console() as (port, accepted),
        caplog.at_level(logging.WARNING, logger="wer.connections.eos"),
    ):
        connection = connect(port, start=False)
        quiet_seconds: list[float] = []
        refresh = connection.refresh_staleness

        def counted() -> None:
            quiet_seconds.append(time.perf_counter())
            refresh()

        connection.refresh_staleness = counted  # type: ignore[method-assign]
        connection.start()
        try:
            assert wait_until(lambda: len(quiet_seconds) >= 2), (
                "quiet seconds were not waited out"
            )
            assert connection.status.state is ConnectionState.WAITING
            assert connection.status.reconnect_attempts == 0
            assert len(accepted) == 1
        finally:
            connection.stop()

    assert not warnings_from_eos(caplog)


def test_a_link_that_breaks_is_logged_as_lost(caplog) -> None:
    """A desk that rebooted answers a keepalive probe with a reset, and that
    has to leave a trace worth reading the morning after. It used to be a
    state change logged at DEBUG."""
    with (
        silent_console() as (port, accepted),
        caplog.at_level(logging.WARNING, logger="wer.connections.eos"),
    ):
        connection = connect(port)
        try:
            assert wait_until(
                lambda: connection.status.state is ConnectionState.WAITING
                and accepted
            )
            # Abort the desk's end: close with a zero linger sends a reset.
            desk = accepted.pop()
            desk.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0)
            )
            desk.close()
            assert wait_until(
                lambda: any("lost the connection" in r.getMessage()
                            for r in caplog.records)
            ), "a broken link went unlogged"
        finally:
            connection.stop()


def test_a_console_that_hangs_up_is_logged(caplog) -> None:
    """Eos quitting or restarting closes its end properly. That used to be a
    DEBUG state change, so the log had a line for a desk that vanished and
    none for one that hung up."""
    with (
        silent_console() as (port, accepted),
        caplog.at_level(logging.WARNING, logger="wer.connections.eos"),
    ):
        connection = connect(port)
        try:
            assert wait_until(
                lambda: connection.status.state is ConnectionState.WAITING
                and accepted
            )
            accepted.pop().close()
            assert wait_until(
                lambda: any("closed the connection" in r.getMessage()
                            for r in warnings_from_eos(caplog))
            ), "a console that hung up went unlogged"
        finally:
            connection.stop()


def test_a_stream_that_cannot_be_framed_is_logged_with_what_to_check(
    caplog,
) -> None:
    """The usual cause is Wer's TCP framing not matching the console's OSC TCP
    Format, which ends every redial the same way, so the log has to name that
    setting. It used to be a state change logged at DEBUG -- and then a line
    that sent whoever read it to the port number ("OSC 1.0 for 3032"), which
    a real Ion XE answering 3032 in OSC 1.1 showed to be wrong."""
    with (
        silent_console() as (port, accepted),
        caplog.at_level(logging.WARNING, logger="wer.connections.eos"),
    ):
        connection = connect(port)
        try:
            assert wait_until(
                lambda: connection.status.state is ConnectionState.WAITING
                and accepted
            )
            # SLIP's END bytes read as an OSC 1.0 length prefix: far past
            # anything a desk sends in one packet.
            accepted[0].sendall(b"\xc0\xc0\xc0\xc0")
            assert wait_until(
                lambda: any("framing" in r.getMessage()
                            for r in warnings_from_eos(caplog))
            ), "a stream that could not be framed went unlogged"
        finally:
            connection.stop()

    said = next(r.getMessage() for r in warnings_from_eos(caplog)
                if "framing" in r.getMessage())
    assert "OSC TCP Format" in said, "the console setting to check is not named"
    assert "for 3032" not in said and "for 3033" not in said, (
        "the port does not decide the framing"
    )


def test_disconnecting_on_purpose_is_not_logged_as_a_lost_desk(caplog) -> None:
    """stop() shuts the socket to wake the reader. That is not the desk
    going away, and a warning on every Disconnect would bury the real ones."""
    with (
        silent_console() as (port, _accepted),
        caplog.at_level(logging.WARNING, logger="wer.connections.eos"),
    ):
        connection = connect(port)
        assert wait_until(lambda: connection.status.state is ConnectionState.WAITING)
        connection.stop()

    assert not warnings_from_eos(caplog)


@pytest.mark.parametrize(
    "woken_by_stop",
    [
        pytest.param(
            OSError(errno.ESHUTDOWN, "shut down", None, errno.ESHUTDOWN),
            id="WSAESHUTDOWN",
        ),
        pytest.param(b"", id="empty-read"),
    ],
)
def test_no_way_a_disconnect_wakes_the_reader_is_logged_as_the_desk(
    monkeypatch, caplog, woken_by_stop: bytes | OSError
) -> None:
    """Which way a real shut socket wakes the reader depends on timing.
    Deleting the error path's guard left the loopback test above still
    passing, because the socket there never woke the reader with an error.
    Each way is forced here."""
    dialled = desks_dialled(monkeypatch, woken_by_stop=woken_by_stop)
    with caplog.at_level(logging.WARNING, logger="wer.connections.eos"):
        connection = connect(3032)
        assert wait_until(
            lambda: connection.status.state is ConnectionState.WAITING
            and bool(dialled) and dialled[0].reads == 1
        )
        connection.stop()

    assert not warnings_from_eos(caplog)
    assert len(dialled) == 1, "a Disconnect was redialled"
