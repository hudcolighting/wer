"""Sending Wer's recording status, received back over loopback multicast.

Real sockets on this machine only: the sender is pointed at a loopback
"adapter", a spare port rather than 5568 and a high universe, so nothing on a
lighting network can see these packets and no sACN software on the machine
can be seen by the test. The receiver joins before the sender starts, because
Windows refuses to send multicast through 127.0.0.1 until something on it has
joined the group.

No Qt.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import replace

import pytest

from wer.connections.adapters import AdapterChoice, NetworkAdapter
from wer.connections.base import ConnectionState
from wer.connections.sacn import (
    DISCOVERY_UNIVERSE,
    MULTICAST_TTL,
    SacnDataPacket,
    SacnDiscoveryPacket,
    decode_packet,
    multicast_group,
    new_cid,
)
from wer.connections.sacn_sender import SacnSender, SacnSettings
from wer.core.databus import DataBus

UNIVERSE = 63001
LOOPBACK = NetworkAdapter("{loopback}", "Loopback", "127.0.0.1", True, "loopback")
WIRED = NetworkAdapter("{wired}", "Ethernet 2", "10.101.90.150", True, "ethernet")
WIFI = NetworkAdapter("{wifi}", "Wi-Fi", "192.168.1.20", True, "wifi")


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Receiver:
    """Joins the test universes and the discovery group on loopback, and keeps
    every packet with the moment it arrived."""

    def __init__(self, port: int, universes=(UNIVERSE, UNIVERSE + 1)) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        try:
            for universe in (*universes, DISCOVERY_UNIVERSE):
                self.sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    struct.pack("4s4s", socket.inet_aton(multicast_group(universe)),
                                socket.inet_aton("127.0.0.1")),
                )
        except OSError as exc:
            self.sock.close()
            pytest.skip(f"cannot join multicast on loopback here: {exc}")
        self.sock.settimeout(0.1)
        self.packets: list[tuple[float, object]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self.packets.append((time.perf_counter(), decode_packet(data)))

    def data(self, start_code: int = 0x00, *, universe: int = UNIVERSE) -> list[SacnDataPacket]:
        with self._lock:
            return [
                p for _, p in self.packets
                if isinstance(p, SacnDataPacket) and p.start_code == start_code
                and p.universe == universe
            ]

    def timed(self, start_code: int = 0x00) -> list[tuple[float, SacnDataPacket]]:
        with self._lock:
            return [
                (t, p) for t, p in self.packets
                if isinstance(p, SacnDataPacket) and p.start_code == start_code
            ]

    def discovery(self) -> list[SacnDiscoveryPacket]:
        with self._lock:
            return [p for _, p in self.packets if isinstance(p, SacnDiscoveryPacket)]

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


@pytest.fixture()
def port():
    return free_udp_port()


@pytest.fixture()
def receiver(port):
    listening = Receiver(port)
    yield listening
    listening.close()


def settings(**overrides) -> SacnSettings:
    fields = dict(
        universe=UNIVERSE, address=42, priority=77, per_address_priority=True,
        adapter="{loopback}", adapter_name="Loopback", source_name="Wer test",
        cid=new_cid(),
    )
    fields.update(overrides)
    return SacnSettings(**fields)


class Flag:
    def __init__(self, value: bool = False) -> None:
        self.value = value

    def __call__(self) -> bool:
        return self.value


def sender(port: int, chosen: SacnSettings, recording=None, adapters=None,
           list_adapters=None) -> SacnSender:
    listed = adapters if adapters is not None else [LOOPBACK]
    made = SacnSender(
        "sacn", DataBus(), chosen, is_recording=recording or Flag(),
        port=port, list_adapters=list_adapters or (lambda: list(listed)),
        route_source_ip=lambda: "127.0.0.1",
    )
    made.adapter_check_interval = 0.3
    return made


# ------------------------------------------------------------------ levels


def test_not_recording_sends_zero_at_the_address(port, receiver) -> None:
    out = sender(port, settings())
    out.start()
    try:
        assert wait_until(lambda: len(receiver.data()) >= 2), "nothing arrived"
        assert out.status.state is ConnectionState.LIVE, out.status.detail
        packet = receiver.data()[0]
        assert (packet.universe, packet.priority, packet.source_name) == (UNIVERSE, 77, "Wer test")
        assert packet.slots == bytes(512)
        sequences = [p.sequence for p in receiver.data()]
        assert sequences == sorted(sequences), "sequence numbers go up"
    finally:
        out.stop()


def test_recording_fades_the_address_up_and_nothing_else(port, receiver) -> None:
    recording = Flag(True)
    out = sender(port, settings(), recording)
    out.start()
    try:
        assert wait_until(lambda: any(p.level(42) > 200 for p in receiver.data()), 3.0), (
            "the level never came near full within a second and a half"
        )
        rising = [p.level(42) for p in receiver.data()][:10]
        assert rising[0] < 30 and rising == sorted(rising), f"not a fade up from 0: {rising}"
        assert all(sum(p.slots) == p.level(42) for p in receiver.data()), (
            "something other than the chosen address was sent above 0"
        )
        recording.value = False
        assert wait_until(lambda: receiver.data()[-1].level(42) == 0), "it did not go back to 0"
    finally:
        out.stop()


def test_a_level_that_stops_changing_is_sent_four_times_then_kept_alive(port, receiver) -> None:
    """Four in quick succession, the change and three more, as ETC's library
    sends them; then one every 850 ms."""
    out = sender(port, settings(per_address_priority=False))
    out.start()
    try:
        time.sleep(2.6)
        times = [t for t, _ in receiver.timed()]
        assert len(times) >= 6, f"{len(times)} packets in 2.6 s"
        quick = [b - a for a, b in zip(times[:4], times[1:4])]
        assert all(gap < 0.2 for gap in quick), f"the first four were not quick: {quick}"
        assert times[4] - times[3] >= 0.7, "a fifth packet came before the keep-alive"
        gaps = [b - a for a, b in zip(times[4:], times[5:])]
        assert gaps and all(0.7 <= g <= 1.1 for g in gaps), f"keep-alive gaps: {gaps}"
    finally:
        out.stop()


def test_packets_may_cross_routers(port) -> None:
    """Windows' default multicast TTL of 1 is dropped by the first router."""
    out = sender(port, settings())
    sock = out._open_socket("127.0.0.1")
    try:
        assert sock.getsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL) == MULTICAST_TTL
    finally:
        sock.close()


# ------------------------------------------------------------ priorities


def test_per_address_priority_gives_wer_its_address_and_nothing_more(port, receiver) -> None:
    out = sender(port, settings())
    out.start()
    try:
        assert wait_until(lambda: bool(receiver.data(0xDD))), "no per-address priority packet"
        packet = receiver.data(0xDD)[0]
        assert packet.level(42) == 77
        assert sum(packet.slots) == 77, "a priority other than 0 away from the address"
        assert packet.priority == 77, "the framing priority carries the same number"
    finally:
        out.stop()


def test_without_per_address_priority_none_are_sent(port, receiver) -> None:
    out = sender(port, settings(per_address_priority=False))
    out.start()
    try:
        assert wait_until(lambda: len(receiver.data()) >= 4)
        time.sleep(0.5)
        assert receiver.data(0xDD) == []
    finally:
        out.stop()


# ------------------------------------------------------- start and finish


def test_stopping_ends_the_stream_with_three_terminated_packets(port, receiver) -> None:
    out = sender(port, settings())
    out.start()
    assert wait_until(lambda: len(receiver.data()) >= 2)
    out.stop()
    assert wait_until(lambda: sum(p.stream_terminated for p in receiver.data()) == 3, 2.0)
    time.sleep(0.2)
    assert sum(p.stream_terminated for p in receiver.data()) == 3
    assert receiver.data(0xDD) and not any(p.stream_terminated for p in receiver.data(0xDD))


def test_stopping_mid_pulse_sends_the_address_at_zero_before_the_end(port, receiver) -> None:
    """Receivers ignore the values in a terminated packet, and one that holds
    its last look would otherwise hold wherever the pulse had got to."""
    out = sender(port, settings(per_address_priority=False), Flag(True))
    out.start()
    assert wait_until(lambda: any(p.level(42) > 100 for p in receiver.data()), 3.0)
    out.stop()
    assert wait_until(lambda: sum(p.stream_terminated for p in receiver.data()) == 3, 2.0)
    packets = receiver.data()
    end = next(i for i, p in enumerate(packets) if p.stream_terminated)
    assert end >= 1 and packets[end - 1].level(42) == 0, (
        f"the last level before the end was {packets[end - 1].level(42)}"
    )


def test_the_universe_is_announced_for_discovery(port, receiver) -> None:
    out = sender(port, settings())
    out.start()
    try:
        assert wait_until(lambda: bool(receiver.discovery())), "no universe discovery packet"
        assert receiver.discovery()[0].universes == (UNIVERSE,)
        assert receiver.discovery()[0].source_name == "Wer test"
    finally:
        out.stop()


def test_nothing_is_sent_while_the_universe_or_address_is_not_one_that_exists(
    port, receiver
) -> None:
    """The show file never holds one (it falls back to 101 and 1), but the
    sender must not rely on that."""
    chosen = settings(universe=0)
    out = sender(port, chosen)
    out.start()
    try:
        assert wait_until(lambda: out.status.state is ConnectionState.ERROR)
        assert "Choose a universe" in out.status.detail
        time.sleep(0.5)
        assert receiver.packets == []

        # A new universe is a new stream, which update_settings refuses on a
        # running sender; the window restarts it. Set directly here: the
        # waiting sender looks at its settings again each time round.
        out.settings = replace(chosen, universe=UNIVERSE, address=0)
        assert wait_until(lambda: "Choose an address" in out.status.detail, 2.0)
        assert receiver.packets == []
    finally:
        out.stop()


# -------------------------------------------------------------- networks


def test_a_missing_network_is_named_and_waited_for(port, receiver) -> None:
    listed: list[NetworkAdapter] = []
    out = sender(port, settings(), adapters=listed)
    out.start()
    try:
        assert wait_until(lambda: out.status.state is ConnectionState.ERROR)
        assert "Loopback" in out.status.detail, out.status.detail
        time.sleep(0.4)
        assert receiver.packets == []

        listed.append(LOOPBACK)
        assert wait_until(lambda: out.status.state is ConnectionState.LIVE, 3.0), (
            "it did not start sending once the network came back"
        )
        assert wait_until(lambda: bool(receiver.data()))
    finally:
        out.stop()


def test_a_network_that_goes_away_mid_stream_stops_the_sending(port, receiver) -> None:
    listed = [LOOPBACK]
    out = sender(port, settings(), adapters=listed)
    out.start()
    try:
        assert wait_until(lambda: bool(receiver.data()))
        listed.clear()
        assert wait_until(lambda: out.status.state is ConnectionState.ERROR, 3.0)
        assert "not on this computer" in out.status.detail
    finally:
        out.stop()


def test_a_network_that_cannot_send_never_reads_as_sending(port, receiver, caplog) -> None:
    """A network that takes the socket or not, and refuses to send: the status
    must never flick to Live between failures, and the log must not say it is
    sending every two seconds all night."""
    unreachable = NetworkAdapter("{bad}", "Unreachable", "10.255.255.254", True, "ethernet")
    out = sender(
        port, settings(adapter="{bad}", adapter_name="Unreachable"), adapters=[unreachable]
    )
    out.adapter_check_interval = 0.2
    states = []
    with caplog.at_level(logging.INFO, logger="wer.connections.sacn_sender"):
        out.start()
        try:
            deadline = time.perf_counter() + 1.5
            while time.perf_counter() < deadline:
                states.append(out.status.state)
                time.sleep(0.01)
        finally:
            out.stop()
    assert ConnectionState.ERROR in states
    assert ConnectionState.LIVE not in states
    assert not [r for r in caplog.records if "sending universe" in r.message]
    assert len([r for r in caplog.records if "is not sending" in r.message]) == 1


def test_a_slow_look_at_the_network_does_not_hold_up_the_fade(port, receiver) -> None:
    calls = [0]

    def slow() -> list[NetworkAdapter]:
        calls[0] += 1
        if calls[0] > 1:
            time.sleep(1.5)
        return [LOOPBACK]

    out = sender(port, settings(per_address_priority=False), Flag(True), list_adapters=slow)
    out.adapter_check_interval = 0.2
    out.start()
    try:
        assert wait_until(lambda: out.status.state is ConnectionState.LIVE)
        time.sleep(0.4)
        before = len(receiver.data())
        time.sleep(1.0)
        sent = len(receiver.data()) - before
        assert sent >= 20, f"{sent} level packets in a second of pulsing"
    finally:
        out.stop()


def test_a_failed_look_at_the_network_leaves_the_stream_alone(port, receiver, caplog) -> None:
    calls = [0]

    def flaky() -> list[NetworkAdapter]:
        calls[0] += 1
        if calls[0] > 1:
            raise OSError(5, "Access is denied")
        return [LOOPBACK]

    out = sender(port, settings(), list_adapters=flaky)
    out.adapter_check_interval = 0.2
    with caplog.at_level(logging.WARNING, logger="wer.connections.sacn_sender"):
        out.start()
        try:
            assert wait_until(lambda: out.status.state is ConnectionState.LIVE)
            time.sleep(1.0)
            assert out.status.state is ConnectionState.LIVE, out.status.detail
        finally:
            out.stop()
    assert len([r for r in caplog.records if "could not look" in r.message]) == 1
    assert sum(p.stream_terminated for p in receiver.data()) == 3, "the stream was restarted"


def test_automatic_moving_a_stream_is_counted_and_said(port) -> None:
    out = sender(port, settings(adapter=""))
    old = AdapterChoice(WIRED, WIRED.ipv4, automatic=True)
    out._network_changed(old, AdapterChoice(WIFI, WIFI.ipv4, automatic=True))
    assert out.moves == 1
    assert "Ethernet 2" in out.last_move and "Wi-Fi" in out.last_move
    out._network_changed(old, AdapterChoice(None, None, automatic=True, problem="gone"))
    assert out.moves == 1, "a network that has gone is an error, not a move"


def test_a_sender_that_will_not_stop_in_time_is_not_joined_by_a_second(port) -> None:
    gate = threading.Event()
    entered = threading.Event()
    block = [False]

    def listing() -> list[NetworkAdapter]:
        if block[0]:
            entered.set()
            gate.wait(10)
        return []

    out = sender(port, settings(), list_adapters=listing)
    out.adapter_check_interval = 0.05
    out.previous_stop_wait = 0.2
    out.start()
    try:
        assert wait_until(lambda: out.status.state is ConnectionState.ERROR)
        block[0] = True
        assert entered.wait(3)
        out.stop(timeout=0.2)
        out.start()
        assert not out.is_running
        assert "not finished stopping" in out.status.detail

        block[0] = False
        gate.set()
        assert wait_until(lambda: out._lingering is None or not out._lingering.is_alive(), 3)
        out.start()
        assert out.is_running
        running = [t for t in threading.enumerate() if t.name == "conn-sacn" and t.is_alive()]
        assert len(running) == 1
    finally:
        gate.set()
        out.stop()


# ---------------------------------------------------------- live changes


def test_address_and_priority_change_on_a_running_stream(port, receiver) -> None:
    chosen = settings()
    recording = Flag(True)
    out = sender(port, chosen, recording)
    out.start()
    try:
        assert wait_until(lambda: any(p.level(42) > 0 for p in receiver.data()))
        assert out.update_settings(replace(chosen, address=43, priority=90)) is True
        assert wait_until(lambda: any(p.level(43) > 0 for p in receiver.data()), 2.0)
        after = [p for p in receiver.data() if p.level(43) > 0]
        assert all(p.level(42) == 0 and p.priority == 90 for p in after)
        assert wait_until(lambda: any(p.level(43) == 90 for p in receiver.data(0xDD)), 2.0)
        assert not any(p.stream_terminated for p in receiver.data()), (
            "changing the address ended the stream"
        )
    finally:
        out.stop()


def test_a_new_universe_is_a_new_stream_for_the_caller_to_restart(port, receiver) -> None:
    chosen = settings()
    out = sender(port, chosen)
    out.start()
    try:
        assert wait_until(lambda: bool(receiver.data()))
        assert out.update_settings(replace(chosen, universe=UNIVERSE + 1)) is False
        assert out.settings.universe == UNIVERSE, "a refused change must change nothing"
    finally:
        out.stop()
    out.settings = replace(chosen, universe=UNIVERSE + 1)
    out.start()
    try:
        assert wait_until(lambda: bool(receiver.data(universe=UNIVERSE + 1)))
    finally:
        out.stop()


def test_asking_whether_wer_is_recording_can_fail_without_stopping_the_status(
    port, receiver, caplog
) -> None:
    def broken() -> bool:
        raise RuntimeError("recorder gone")

    out = sender(port, settings(), broken)
    with caplog.at_level(logging.ERROR, logger="wer.connections.sacn_sender"):
        out.start()
        try:
            assert wait_until(lambda: len(receiver.data()) >= 3)
            assert all(p.level(42) == 0 for p in receiver.data())
        finally:
            out.stop()
    assert len([r for r in caplog.records if "recording" in r.message]) == 1
