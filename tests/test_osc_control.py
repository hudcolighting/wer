"""Commands from the console: what Wer answers to, and how a press is counted.

A macro on the desk sends them down the link Wer already holds to it, among the
console's own traffic. So they are exercised here through the Eos connection's
receive path and over a real TCP socket, and replayed from a real capture
(tests/fixtures/eos/macro-record-toggle.jsonl), not only by calling handlers.
The separate UDP listener this replaced passed every test it had and was one
the desk could never reach.

No Qt, except the test that builds a MainWindow.
"""

from __future__ import annotations

import json
import logging
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from wer.connections.base import ConnectionState
from wer.connections.eos import EosConnection, EosSettings
from wer.connections.osc import LengthPrefixFramer, OscMessage, encode_message
from wer.connections.osc_control import (
    COMMANDS,
    REPEAT_QUIET_SECONDS,
    UNKNOWN_ADDRESSES_SAID,
    CommandDispatcher,
    is_button_release,
)
from wer.core.databus import DataBus

FIXTURE = Path(__file__).parent / "fixtures" / "eos" / "macro-record-toggle.jsonl"


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class FakeClock:
    """A clock a test moves by hand, so a burst takes no real time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def captured() -> list[tuple[float, OscMessage]]:
    """The capture, as (seconds into it, message)."""
    records = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records.append((
                record["t"],
                OscMessage(record["address"], tuple(record["args"]), record["typetags"]),
            ))
    return records


def eos_link(**kwargs) -> EosConnection:
    """An Eos connection that has dialled nothing, for feeding packets to."""
    connection = EosConnection(
        "eos", DataBus(), EosSettings(send_user=False, subscribe=False), **kwargs
    )
    connection._set_state(ConnectionState.WAITING)
    return connection


def bundle(*messages: bytes) -> bytes:
    """An OSC bundle timetagged "immediately", holding these encoded messages."""
    packet = b"#bundle\x00" + struct.pack(">Q", 1)
    for message in messages:
        packet += struct.pack(">i", len(message)) + message
    return packet


# ------------------------------------------------------------------ the set


def test_the_four_documented_commands_exist() -> None:
    """The ones asked for by name."""
    addresses = {command.address for command in COMMANDS}
    assert {
        "/wer/record/start",
        "/wer/record/stop",
        "/wer/record/toggle",
        "/wer/record/snapshot",
    } <= addresses


def test_every_command_has_a_summary() -> None:
    for command in COMMANDS:
        assert command.address.startswith("/wer/")
        assert command.summary.strip()


def test_the_main_window_takes_every_documented_command_from_the_console(qt_app) -> None:
    """A command in the list that nothing acts on would be a lie in the help.
    And there is one way in: the console's own link, not a listener beside it."""
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        registered = set(window.eos.commands.handlers)
        documented = {command.address for command in COMMANDS}
        assert documented <= registered, (
            f"documented but unhandled: {sorted(documented - registered)}"
        )
        assert not hasattr(window, "osc_control"), "the separate listener is back"
    finally:
        window.close()
        window.deleteLater()


# ---------------------------------------------------------------- dispatching


def test_a_command_reaches_its_handler() -> None:
    commands = CommandDispatcher()
    seen: list[str] = []
    commands.on("/wer/record/start", lambda m: seen.append(m.address))

    assert commands.dispatch(OscMessage("/wer/record/start")) is True
    assert seen == ["/wer/record/start"]


def test_a_string_argument_arrives_intact() -> None:
    commands = CommandDispatcher()
    seen: list = []
    commands.on("/wer/marker", lambda m: seen.append(m.args))

    commands.dispatch(OscMessage("/wer/marker", ("followspot late",), "s"))
    assert seen == [("followspot late",)]


def test_an_unknown_address_is_counted_not_guessed_at() -> None:
    commands = CommandDispatcher()
    assert commands.dispatch(OscMessage("/wer/record/togle")) is False
    assert commands.unknown_count == 1


def _unknown_lines(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "not a Wer command" in r.message]


def test_an_unknown_address_is_said_in_the_log(caplog) -> None:
    """A macro with a typo does nothing, and this line is the only place that
    says why."""
    commands = CommandDispatcher()
    with caplog.at_level(logging.INFO, logger="wer.connections.osc_control"):
        commands.dispatch(OscMessage("/wer/record/togle"))
    assert [r.message for r in _unknown_lines(caplog)] == [
        "Ignoring /wer/record/togle from the console: it is not a Wer command"
    ]


def test_an_unknown_address_pressed_all_night_is_said_once(caplog) -> None:
    commands = CommandDispatcher()
    with caplog.at_level(logging.INFO, logger="wer.connections.osc_control"):
        for _ in range(40):
            commands.dispatch(OscMessage("/wer/record/togle"))
    assert len(_unknown_lines(caplog)) == 1
    assert commands.unknown_count == 40


def test_a_second_typo_later_in_the_night_is_still_said(caplog) -> None:
    """One allowance for the whole night used to be spent by the first typo."""
    commands = CommandDispatcher()
    with caplog.at_level(logging.INFO, logger="wer.connections.osc_control"):
        for _ in range(5):
            commands.dispatch(OscMessage("/wer/record/togle"))
        commands.dispatch(OscMessage("/wer/marker/act2"))
    assert any("/wer/marker/act2" in r.message for r in _unknown_lines(caplog))


def test_unknown_addresses_stop_flooding_the_log(caplog) -> None:
    commands = CommandDispatcher()
    with caplog.at_level(logging.INFO, logger="wer.connections.osc_control"):
        for i in range(60):
            commands.dispatch(OscMessage(f"/wer/nothing/{i}"))
    lines = _unknown_lines(caplog)
    assert len(lines) == UNKNOWN_ADDRESSES_SAID, f"logged {len(lines)} lines for 60 messages"
    assert commands.unknown_count == 60


def test_a_handler_that_throws_does_not_stop_the_next_command(caplog) -> None:
    commands = CommandDispatcher()
    survived: list[str] = []

    def explode(message) -> None:
        raise RuntimeError("bad handler")

    commands.on("/wer/record/start", explode)
    commands.on("/wer/record/stop", lambda m: survived.append(m.address))
    with caplog.at_level(logging.ERROR, logger="wer.connections.osc_control"):
        assert commands.dispatch(OscMessage("/wer/record/start")) is False
    assert any("/wer/record/start" in r.message for r in caplog.records)
    commands.dispatch(OscMessage("/wer/record/stop"))
    assert survived == ["/wer/record/stop"]


# ------------------------------------------------------------ button release


@pytest.mark.parametrize(
    ("args", "is_release"),
    [
        ((), False),
        ((1.0,), False),
        ((1,), False),
        ((0.0,), True),
        ((0,), True),
        ((True,), False),
        ((False,), True),
        (("go",), False),
    ],
)
def test_button_release_detection(args, is_release: bool) -> None:
    assert is_button_release(OscMessage("/wer/record/toggle", args)) is is_release


def test_a_press_and_release_pair_fires_once() -> None:
    """With the quiet second long gone, so the repeat rule cannot be what
    stops the release."""
    clock = FakeClock()
    commands = CommandDispatcher(clock=clock)
    fired: list[float] = []
    commands.on("/wer/record/toggle", lambda m: fired.append(clock.now))

    commands.dispatch(OscMessage("/wer/record/toggle", (1.0,), "f"))
    clock.now += 10 * REPEAT_QUIET_SECONDS
    commands.dispatch(OscMessage("/wer/record/toggle", (0.0,), "f"))
    assert len(fired) == 1, f"fired {len(fired)} times; the release was acted on"


# ------------------------------------------------------------------ repeats


def _toggles(clock: FakeClock) -> tuple[CommandDispatcher, list[float]]:
    commands = CommandDispatcher(clock=clock)
    fired: list[float] = []
    commands.on("/wer/record/toggle", lambda m: fired.append(clock.now))
    return commands, fired


def test_a_burst_of_taps_is_one_press() -> None:
    clock = FakeClock()
    commands, fired = _toggles(clock)
    for _ in range(17):
        commands.dispatch(OscMessage("/wer/record/toggle"))
        clock.now += 0.18
    assert len(fired) == 1
    assert commands.repeats_ignored == 16


def test_every_ignored_repeat_restarts_the_quiet_second() -> None:
    """Taps 0.9 s apart for five seconds never leave a quiet second, so they
    are still one press. The rule is quiet since the last tap, not since the
    last one acted on."""
    clock = FakeClock()
    commands, fired = _toggles(clock)
    for _ in range(6):
        commands.dispatch(OscMessage("/wer/record/toggle"))
        clock.now += 0.9
    assert len(fired) == 1


def test_a_press_after_a_quiet_second_counts_again() -> None:
    clock = FakeClock()
    commands, fired = _toggles(clock)
    commands.dispatch(OscMessage("/wer/record/toggle"))
    clock.now += REPEAT_QUIET_SECONDS + 0.01
    commands.dispatch(OscMessage("/wer/record/toggle"))
    assert len(fired) == 2


def test_a_burst_is_said_once_in_the_log(caplog) -> None:
    clock = FakeClock()
    commands, _fired = _toggles(clock)
    with caplog.at_level(logging.INFO, logger="wer.connections.osc_control"):
        for _ in range(10):
            commands.dispatch(OscMessage("/wer/record/toggle"))
            clock.now += 0.1
    said = [r for r in caplog.records if "counts as one" in r.message]
    assert len(said) == 1


def test_stop_then_start_from_one_macro_both_act() -> None:
    """Only the same command is folded. The window deliberately holds a start
    that lands while the take is closing, and this must not drop it first."""
    clock = FakeClock()
    commands = CommandDispatcher(clock=clock)
    fired: list[str] = []
    commands.on("/wer/record/stop", lambda m: fired.append(m.address))
    commands.on("/wer/record/start", lambda m: fired.append(m.address))

    commands.dispatch(OscMessage("/wer/record/stop"))
    clock.now += 0.02
    commands.dispatch(OscMessage("/wer/record/start"))
    assert fired == ["/wer/record/stop", "/wer/record/start"]


@pytest.mark.parametrize("address", ["/wer/layout/next", "/wer/marker", "/wer/record/snapshot"])
def test_commands_meant_to_be_pressed_repeatedly_are_not_folded(address: str) -> None:
    clock = FakeClock()
    commands = CommandDispatcher(clock=clock)
    fired: list[str] = []
    commands.on(address, lambda m: fired.append(m.address))
    for _ in range(3):
        commands.dispatch(OscMessage(address))
        clock.now += 0.1
    assert len(fired) == 3


# ------------------------------------------------------------- the capture


def test_the_capture_is_what_the_console_sends() -> None:
    """What the rest of this file leans on, checked against the file itself:
    twenty toggles, no arguments, each straight after the console's own macro
    event."""
    records = captured()
    commands = [(i, m) for i, (_t, m) in enumerate(records) if m.address.startswith("/wer/")]
    assert len(commands) == 20
    for index, message in commands:
        assert (message.address, message.args, message.typetags) == ("/wer/record/toggle", (), "")
        assert records[index - 1][1].address == "/eos/out/event/macro/1"


def test_the_captured_taps_are_two_presses() -> None:
    """Hudson tapped macro 1 fast: a run of 17, 1.15 s of nothing, then 3.
    Replayed at the captured times, that starts a take and stops it, once."""
    clock = FakeClock()
    commands, fired = _toggles(clock)
    for t, message in captured():
        if message.address.startswith("/wer/"):
            clock.now = t
            commands.dispatch(message)
    assert len(fired) == 2


# ------------------------------------------------ arriving on the Eos link


def test_a_command_on_the_eos_connection_reaches_its_handler() -> None:
    link = eos_link()
    seen: list[str] = []
    link.commands.on("/wer/record/toggle", lambda m: seen.append(m.address))

    link._consume(encode_message("/wer/record/toggle"))
    assert seen == ["/wer/record/toggle"]


def test_a_command_is_shown_but_is_neither_noise_nor_proof_the_desk_is_alive(caplog) -> None:
    """Before this, /wer/record/toggle scrolled past on the Eos tab as "not Eos
    OSC", counted as noise, and nothing acted on it. Nor may it swing the other
    way: over UDP anything on the network can send one."""
    link = eos_link()
    link.commands.on("/wer/record/toggle", lambda m: None)
    with caplog.at_level(logging.INFO, logger="wer.connections.eos"):
        link._consume(encode_message("/wer/record/toggle"))

    assert "/wer/record/toggle" in link.monitor_entries()[-1].summary
    assert link.status.noise_packets == 0
    assert link.status.packets == 0
    assert link.status.state is ConnectionState.WAITING
    assert link.status.seconds_since_packet is None
    assert "not from this source" not in link.status.describe()
    assert not [r for r in caplog.records if "not Eos OSC" in r.message]


def test_commands_from_something_else_cannot_hide_a_desk_that_went_quiet() -> None:
    """UDP binds 0.0.0.0 and takes datagrams from anyone. A stream deck
    sending /wer/marker must not flip a stale console back to Live, and the
    overlay must not be told the desk is connected."""
    from wer.connections.eos import EosTransport

    bus = DataBus()
    link = EosConnection("eos", bus, EosSettings(transport=EosTransport.UDP, port=8123))
    link._set_state(ConnectionState.WAITING)
    link.stale_after = 0.2
    seen: list = []
    link.commands.on("/wer/marker", lambda m: seen.append(m.args))

    link._consume(encode_message("/eos/out/active/cue/1/58", 1.0))
    assert bus.value("eos.connected") is True
    time.sleep(0.3)
    link.refresh_staleness()
    assert link.status.state is ConnectionState.STALE

    for _ in range(3):
        link._consume(encode_message("/wer/marker", "from a phone"))
    link.refresh_staleness()
    assert link.status.state is ConnectionState.STALE
    assert bus.value("eos.connected") is False
    assert len(seen) == 3, "the commands themselves must still be acted on"


def test_another_apps_osc_is_still_somebody_elses() -> None:
    """The carve-out is the /wer/ namespace, nothing wider."""
    link = eos_link()
    link._consume(encode_message("/1/fader1", 0.5))
    assert link.status.noise_packets == 1
    assert link.status.state is ConnectionState.WAITING


def test_a_command_in_a_bundle_with_console_traffic_is_not_lost() -> None:
    """Eos sent each message in a packet of its own in the capture. A bundle
    holding both goes to the cue parser whole, and the command must not
    disappear inside it."""
    fires: list = []
    link = eos_link(on_cue_fired=fires.append)
    seen: list[str] = []
    link.commands.on("/wer/record/toggle", lambda m: seen.append(m.address))

    link._consume(bundle(
        encode_message("/eos/out/event/cue/1/250/fire", "Officer Line"),
        encode_message("/wer/record/toggle"),
    ))
    assert seen == ["/wer/record/toggle"]
    assert len(fires) == 1, "the console traffic in the same bundle was lost"


def test_the_captured_session_replays_through_the_eos_connection() -> None:
    """The real traffic, packet by packet: the cue fires still parse around the
    taps, and the twenty taps are two presses."""
    clock = FakeClock()
    fires: list = []
    link = eos_link(on_cue_fired=fires.append)
    link.commands = CommandDispatcher(clock=clock)
    toggles: list[float] = []
    link.commands.on("/wer/record/toggle", lambda m: toggles.append(clock.now))

    for t, message in captured():
        clock.now = t
        link._consume(encode_message(message.address, *message.args))

    assert len(toggles) == 2
    assert len(fires) == 3
    assert link.status.noise_packets == 0


def test_a_command_sent_down_a_real_tcp_link_is_acted_on() -> None:
    """The path Hudson's desk used: Wer dials the console, and the macro's
    event and command come back down that socket, length-prefixed."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    accepted: list[socket.socket] = []

    def accept() -> None:
        try:
            client, _ = server.accept()
            accepted.append(client)
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    link = EosConnection(
        "eos", DataBus(),
        EosSettings(host="127.0.0.1", port=server.getsockname()[1],
                    send_user=False, subscribe=False),
    )
    seen: list[str] = []
    link.commands.on("/wer/record/toggle", lambda m: seen.append(m.address))
    link.start()
    try:
        assert wait_until(lambda: bool(accepted)), "Wer never dialled"
        accepted[0].sendall(
            LengthPrefixFramer.frame(encode_message("/eos/out/event/macro/1"))
            + LengthPrefixFramer.frame(encode_message("/wer/record/toggle"))
        )
        assert wait_until(lambda: seen == ["/wer/record/toggle"]), "nothing acted on it"
    finally:
        link.stop()
        for client in accepted:
            client.close()
        server.close()


# ------------------------------------------------------------- show files


def test_a_show_file_with_the_old_listener_settings_still_loads(tmp_path) -> None:
    """Its osc_control section belonged to the listener that was removed. It is
    read past, and gone the next time the file is saved."""
    from wer.core.showfile import ShowFile, load_show, save_show

    path = tmp_path / "before.wer"
    save_show(ShowFile(), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["osc_control"] = {"enabled": True, "bind": "0.0.0.0", "port": 8001}
    path.write_text(json.dumps(data), encoding="utf-8")

    save_show(load_show(path), path)
    assert "osc_control" not in json.loads(path.read_text(encoding="utf-8"))
