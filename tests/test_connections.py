"""Connection framework: threading, status, reconnect, and the built-ins.

No Qt in this process -- that is the point of keeping connections Qt-free.
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

from wer.connections.base import BACKOFF_SCHEDULE, Connection, ConnectionState
from wer.connections.builtin import (
    ManualConnection,
    SystemConnection,
    format_duration,
)
from wer.connections.eos import EosConnection, EosSettings
from wer.core.databus import DataBus


@pytest.fixture()
def bus() -> DataBus:
    return DataBus()


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Poll until true. Threads make fixed sleeps either flaky or slow."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class ScriptedConnection(Connection):
    """A connection whose _run_once behaviour the test decides."""

    def __init__(self, bus: DataBus, *, fail: bool = False) -> None:
        super().__init__("scripted", bus)
        self.runs = 0
        self.fail = fail
        self.released = threading.Event()

    @property
    def display_name(self) -> str:
        return "Scripted"

    def _run_once(self) -> None:
        self.runs += 1
        if self.fail:
            raise RuntimeError("boom")
        self._set_state(ConnectionState.LIVE)
        self.released.wait(timeout=5.0)


# --------------------------------------------------------------------- lifecycle


def test_start_and_stop(bus: DataBus) -> None:
    conn = ScriptedConnection(bus)
    conn.start()
    assert wait_until(lambda: conn.status.state is ConnectionState.LIVE)
    conn.released.set()
    conn.stop()
    assert not conn.is_running
    assert conn.status.state is ConnectionState.DISCONNECTED


def test_starting_twice_is_harmless(bus: DataBus) -> None:
    conn = ScriptedConnection(bus)
    conn.start()
    conn.start()
    assert wait_until(lambda: conn.status.state is ConnectionState.LIVE)
    conn.released.set()
    conn.stop()
    assert conn.runs == 1


def test_failure_is_reported_and_retried(bus: DataBus) -> None:
    """A console reboot must not require restarting the app."""
    conn = ScriptedConnection(bus, fail=True)
    conn.start()
    assert wait_until(lambda: conn.runs >= 2, timeout=6.0), "did not retry"
    assert conn.status.reconnect_attempts >= 1
    conn.stop()


def test_stop_during_backoff_returns_promptly(bus: DataBus) -> None:
    """Backoff waits on the stop event, not on sleep, so shutdown is immediate."""
    conn = ScriptedConnection(bus, fail=True)
    conn.start()
    assert wait_until(lambda: conn.status.reconnect_attempts >= 1, timeout=5.0)
    started = time.perf_counter()
    conn.stop(timeout=3.0)
    assert time.perf_counter() - started < 2.0, "stop blocked for the backoff delay"


class BackoffRecorder(threading.Event):
    """A stop event that writes down each backoff it is asked for, and skips it.

    The schedule's real delays add up to over a minute before it tops out, so
    the test reads what the supervisor chose rather than sitting through it.
    A wait with no timeout still blocks until stop, as the real one does.
    """

    def __init__(self) -> None:
        super().__init__()
        self.delays: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        if timeout is None:
            return super().wait()
        self.delays.append(timeout)
        return self.is_set()


class OutageScript(Connection):
    """Plays one scripted run per _run_once, then holds until stopped.

    "refused" returns at once, as dialling a switched-off console does. "up"
    connects, delivers a packet, stays up for ``up_for`` seconds and drops.
    """

    def __init__(self, bus: DataBus, steps: list[str], *, up_for: float) -> None:
        super().__init__("outage", bus)
        self._stop = BackoffRecorder()
        self.steps = list(steps)
        self.up_for = up_for
        self.script_done = threading.Event()

    @property
    def display_name(self) -> str:
        return "Outage script"

    def _run_once(self) -> None:
        if not self.steps:
            self.script_done.set()
            self._stop.wait()
            return
        if self.steps.pop(0) == "up":
            self._set_state(ConnectionState.WAITING)
            self.record("state dump", size=64)
            time.sleep(self.up_for)


def _play(conn: OutageScript) -> list[float]:
    conn.start()
    try:
        assert conn.script_done.wait(10.0), "the script never finished"
    finally:
        conn.stop()
    return conn._stop.delays


def test_a_link_that_recovered_gets_the_fast_retry_again(bus: DataBus) -> None:
    """The schedule used to climb for the whole session and never come down.

    One desk reboot that used up the fast steps left every later drop waiting
    30 s for its first retry, where a blip is meant to recover in half a
    second -- 30 s of cue fires with no markers, on every drop, until Wer was
    restarted. The attempt counter on the panel counted the whole session too.
    """
    conn = OutageScript(bus, ["refused"] * 8 + ["up", "refused"], up_for=0.8)
    conn.backoff_resets_after = 0.5

    delays = _play(conn)

    reboot = [*BACKOFF_SCHEDULE, BACKOFF_SCHEDULE[-1]]
    assert delays[:8] == reboot
    assert delays[8:] == [BACKOFF_SCHEDULE[0], BACKOFF_SCHEDULE[1]], (
        "a drop after hours of good link waited as long as the reboot did"
    )
    assert conn.status.reconnect_attempts == 2


def test_a_link_that_keeps_dropping_goes_on_backing_off(bus: DataBus) -> None:
    """The reset must not turn a flapping desk into a reconnect loop.

    A console that accepts, sends its state dump and hangs up straight away
    has delivered data on every run. If that counted as recovered it would be
    re-dialled every half second all night.
    """
    conn = OutageScript(bus, ["up"] * 5, up_for=0.0)
    conn.backoff_resets_after = 0.5

    assert _play(conn) == list(BACKOFF_SCHEDULE[:5])


# ------------------------------------------------------------------ status model


def test_a_stopped_connection_never_reports_itself_live(bus: DataBus) -> None:
    """record() must not promote a DISCONNECTED connection.

    Otherwise a stray late packet from a socket being torn down would light the
    status lamp green on a connection the user has just switched off.
    """
    conn = ScriptedConnection(bus)
    assert conn.status.state is ConnectionState.DISCONNECTED
    conn.record("a late straggler")
    assert conn.status.state is ConnectionState.DISCONNECTED


def test_connected_is_not_live_until_data_arrives(bus: DataBus) -> None:
    """The state that exists because Eos accepts a socket and says nothing.

    Reporting that as Live would send the user hunting for a network fault when
    the real problem is one checkbox on the console.
    """
    conn = ScriptedConnection(bus)
    conn._set_state(ConnectionState.WAITING)
    assert conn.status.state is ConnectionState.WAITING
    assert not conn.status.state.is_usable

    conn.record("first packet", size=10)
    assert conn.status.state is ConnectionState.LIVE
    assert conn.status.state.is_usable


def test_live_goes_stale_when_data_stops(bus: DataBus) -> None:
    conn = ScriptedConnection(bus)
    conn.stale_after = 0.05
    # Follow the real path: transport up, then first packet. record() will not
    # promote a DISCONNECTED connection to Live, deliberately - a stopped
    # connection must never report itself as healthy.
    conn._set_state(ConnectionState.WAITING)
    conn.record("packet")
    assert conn.status.state is ConnectionState.LIVE

    time.sleep(0.1)
    conn.refresh_staleness()
    assert conn.status.state is ConnectionState.STALE


def test_stale_recovers_to_live_when_data_resumes(bus: DataBus) -> None:
    conn = ScriptedConnection(bus)
    conn.stale_after = 0.05
    conn._set_state(ConnectionState.WAITING)
    conn.record("packet")
    time.sleep(0.1)
    conn.refresh_staleness()
    assert conn.status.state is ConnectionState.STALE

    conn.record("another packet")
    assert conn.status.state is ConnectionState.LIVE


def test_packets_per_second_is_measured(bus: DataBus) -> None:
    conn = ScriptedConnection(bus)
    conn._set_state(ConnectionState.WAITING)
    for _ in range(10):
        conn.record("p", size=4)
        time.sleep(0.005)
    assert conn.status.packets == 10
    assert conn.status.packets_per_second > 0
    assert conn.status.bytes_received == 40


# ----------------------------------------------------------------------- monitor


def test_monitor_is_capped(bus: DataBus) -> None:
    """Capped: an Eos command line would otherwise grow without bound."""
    conn = ScriptedConnection(bus)
    conn._monitor = type(conn._monitor)(maxlen=10)
    for i in range(50):
        conn.record(f"packet {i}")
    entries = conn.monitor_entries()
    assert len(entries) == 10
    assert entries[-1].summary == "packet 49"


def test_monitor_can_be_paused(bus: DataBus) -> None:
    conn = ScriptedConnection(bus)
    conn.record("before")
    conn.monitor_paused = True
    conn.record("during")
    conn.monitor_paused = False
    conn.record("after")

    summaries = [e.summary for e in conn.monitor_entries()]
    assert summaries == ["before", "after"]


def test_pausing_the_monitor_does_not_pause_the_counters(bus: DataBus) -> None:
    """The view freezes; the connection does not."""
    conn = ScriptedConnection(bus)
    conn.monitor_paused = True
    conn.record("hidden")
    assert conn.status.packets == 1


def test_a_full_monitor_still_tells_a_reader_what_is_new(bus: DataBus) -> None:
    """Once the buffer is full its length stops changing, so length cannot say
    what is new. The Connections tab read it that way and froze at 500 lines."""
    conn = ScriptedConnection(bus)
    for i in range(500):
        conn.record(f"packet {i}")
    seen = conn.monitor_entries()[-1].seq
    for i in range(500, 600):
        conn.record(f"packet {i}")

    entries, dropped = conn.monitor_since(seen)
    assert [e.summary for e in entries] == [f"packet {i}" for i in range(500, 600)]
    assert dropped == 0


def test_a_reader_that_fell_behind_is_told_how_far(bus: DataBus) -> None:
    """Lines the buffer dropped before anyone read them are counted, so a view
    can say so instead of passing off what is left as everything."""
    conn = ScriptedConnection(bus)
    for i in range(1200):
        conn.record(f"packet {i}")

    entries, dropped = conn.monitor_since(0)
    assert dropped == 700
    assert entries[0].summary == "packet 700"
    assert len(entries) == 500


# ------------------------------------------------------------------ System


def test_system_connection_publishes_clock_keys(bus: DataBus) -> None:
    conn = SystemConnection("system", bus)
    conn.start()
    assert wait_until(lambda: "clock.wall" in bus)
    conn.stop()

    assert bus.value("clock.take") == 1
    assert bus.value("clock.recording") is False
    assert isinstance(bus.value("clock.wall"), str)
    assert len(bus.value("clock.date")) == 10


def test_record_elapsed_tracks_recording(bus: DataBus) -> None:
    conn = SystemConnection("system", bus)
    assert conn.record_elapsed == 0.0
    conn.start_recording()
    time.sleep(0.05)
    assert conn.record_elapsed > 0.0
    conn.stop_recording()
    assert conn.record_elapsed == 0.0
    assert conn.take == 2, "stopping should advance the take counter"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0:00:00"), (61, "0:01:01"), (3661, "1:01:01"), (-5, "0:00:00")],
)
def test_format_duration(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected


# ------------------------------------------------------------------ Manual


def test_manual_fields_publish_to_the_bus(bus: DataBus) -> None:
    conn = ManualConnection("manual", bus)
    conn.set_field("act", "Two")
    assert bus.value("manual.act") == "Two"


def test_manual_field_names_are_normalised(bus: DataBus) -> None:
    """A field typed as 'Stage Manager' must not become an unusable bus key."""
    conn = ManualConnection("manual", bus)
    conn.set_field("Stage Manager", "Alex")
    assert bus.value("manual.stage_manager") == "Alex"


def test_clearing_a_manual_field_publishes_empty_not_missing(bus: DataBus) -> None:
    """A cleared field means "nothing typed", not "no data"."""
    conn = ManualConnection("manual", bus)
    conn.set_field("note", "something")
    conn.clear_field("note")
    assert bus.value("manual.note") == ""
    assert "manual.note" in bus


def test_manual_fields_render_in_a_template(bus: DataBus) -> None:
    conn = ManualConnection("manual", bus)
    conn.set_field("show", "The Comedy of Errors")
    conn.set_field("act", "Two")
    assert bus.render("{manual.show} — Act {manual.act}") == (
        "The Comedy of Errors — Act Two"
    )


# ------------------------------------------------------------------ Eos


def test_eos_reports_a_helpful_error_when_nothing_is_listening(bus: DataBus) -> None:
    """No silent failures. The message must say what to check."""
    # Port 1 on localhost: reliably refused, and fast about it.
    conn = EosConnection("eos", bus, EosSettings(host="127.0.0.1", port=1))
    conn.start()
    assert wait_until(
        lambda: conn.status.state is ConnectionState.ERROR and conn.status.detail,
        timeout=6.0,
    )
    detail = conn.status.detail.lower()
    conn.stop()
    assert any(word in detail for word in ("refused", "unreachable", "response")), (
        f"unhelpful error detail: {conn.status.detail!r}"
    )


def test_eos_socket_open_but_silent_is_waiting_not_live(bus: DataBus) -> None:
    """The exact behaviour of a real Eos with OSC output disabled.

    A server that accepts and says nothing must show as Waiting, so the user is
    pointed at the console's OSC settings rather than at the network.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    accepted: list[socket.socket] = []

    def accept_and_stay_silent() -> None:
        try:
            client, _ = server.accept()
            accepted.append(client)
        except OSError:
            pass

    thread = threading.Thread(target=accept_and_stay_silent, daemon=True)
    thread.start()

    conn = EosConnection(
        "eos", bus,
        EosSettings(host="127.0.0.1", port=port, subscribe=False, send_user=False),
    )
    conn.start()
    try:
        assert wait_until(
            lambda: conn.status.state is ConnectionState.WAITING, timeout=5.0
        ), f"expected WAITING, got {conn.status.state}"
        assert "OSC" in conn.status.detail
    finally:
        conn.stop()
        for sock in accepted:
            sock.close()
        server.close()


def test_eos_settings_round_trip_through_the_panel() -> None:
    """Loading a show file sets settings programmatically; the UI must follow.

    Needs a QApplication, so it is skipped when Qt is unavailable. It lives here
    rather than in a UI test module because what it guards is the settings
    round-trip, not the widget.
    """
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from wer.connections.eos import EosFraming, EosTransport
    from wer.ui.connection_panel import EosConnectionPanel

    app = QApplication.instance() or QApplication([])
    bus = DataBus()
    conn = EosConnection("eos", bus, EosSettings())
    panel = EosConnectionPanel(conn)

    conn.settings = EosSettings(
        host="10.101.90.101", port=8000, transport=EosTransport.UDP,
        framing=EosFraming.SLIP, user=3, subscribe=False,
    )
    panel.load_settings()
    panel.apply_settings()

    assert conn.settings.host == "10.101.90.101"
    assert conn.settings.port == 8000
    assert conn.settings.transport is EosTransport.UDP
    assert conn.settings.user == 3
    assert conn.settings.subscribe is False
    panel.deleteLater()


def test_settings_coerce_bare_strings_to_enums() -> None:
    """Qt flattens str-subclassing enums out of QVariant, and JSON has no enums.

    Both dispatch on `is`, so a bare "TCP" would silently take the UDP branch.
    """
    from wer.connections.eos import EosFraming, EosTransport

    settings = EosSettings(transport="UDP", framing="OSC 1.1 (SLIP)")
    assert settings.transport is EosTransport.UDP
    assert settings.framing is EosFraming.SLIP


def test_settings_reject_a_nonsense_transport() -> None:
    """Better a loud failure at load time than a silent wrong branch at showtime."""
    with pytest.raises(ValueError):
        EosSettings(transport="carrier pigeon")


# ------------------------------ the OSC user gates the whole cue-state stream


def _eos(user: int):
    from wer.connections.eos import EosConnection, EosSettings
    from wer.core.databus import DataBus

    return EosConnection("eos", DataBus(), EosSettings(user=user))


def test_the_default_osc_user_is_any() -> None:
    """Measured on a real desk: announcing user 1 to a console being driven by
    user 2 produced ZERO ongoing cue-state messages in 25 seconds. User 2 or
    user 0 produced 26. Cue fires arrive regardless, which is what makes the
    misconfiguration so hard to see -- the live cue limps forward while the
    next cue never moves at all. A recorder wants every user's traffic."""
    from wer.connections.eos import EosSettings
    from wer.core.showfile import EosConfig

    assert EosSettings().user == 0
    assert EosConfig().user == 0


def test_the_osc_user_box_says_what_the_user_gates() -> None:
    """The tooltip used to call the OSC user "whose command line to follow",
    which is what the layout editor's "Command line from" box does. Someone
    who set the user to their own number on that advice silenced the cue data
    for every night another operator drove the desk, and nothing on screen
    said why. The box has to say what the number really gates, and where the
    command-line choice actually lives.

    Needs a QApplication, like the settings round-trip above.
    """
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from wer.ui.connection_panel import EosConnectionPanel

    app = QApplication.instance() or QApplication([])
    panel = EosConnectionPanel(_eos(user=0))
    tip = panel._user.toolTip()
    panel.deleteLater()

    assert "cue state" in tip, "does not say the user gates the cue state"
    assert "Command line from" in tip, "does not point at the per-widget choice"
    assert "command line to follow" not in tip.lower()


def _fire(number: str = "58"):
    from wer.connections.osc import OscMessage

    return OscMessage(f"/eos/out/event/cue/1/{number}/fire", ("Adriana Xs Center",))


def _other_users_cmd(user: int = 2):
    from wer.connections.osc import OscMessage

    return OscMessage(f"/eos/out/user/{user}/cmd", ("LIVE: Cue 8", 0))


def _cue_state():
    from wer.connections.osc import OscMessage

    return OscMessage("/eos/out/active/cue/text", ("1/58 Adriana Xs Center 3.0 100%",))


def _at(connection, seconds: float):
    """Move the connection's cue-state clock to a given moment."""
    connection._clock = lambda: seconds
    return connection


def test_a_desk_that_never_sends_cue_state_is_noticed() -> None:
    """The measured signature of the wrong binding: cue fires keep arriving,
    the ongoing cue-state stream does not. 25 seconds of it on a real desk."""
    connection = _eos(user=1)
    assert connection.user_mismatch_seen is False

    _at(connection, 0.0)._check_user_binding(_other_users_cmd(2))
    _at(connection, 1.0)._check_user_binding(_fire("58"))
    _at(connection, 25.0)._check_user_binding(_fire("62"))

    assert connection.user_mismatch_seen is True
    assert connection.driving_user == 2


def test_a_second_operator_on_a_healthy_desk_is_not_a_mismatch() -> None:
    """The false positive this replaced.

    A real desk sends EVERY user's command line to every client, so the old
    check -- "another user's command line arrived" -- warned about a session
    where nothing whatsoever was wrong, and sent the operator to change a
    setting that was already correct.
    """
    connection = _eos(user=1)

    for minute in range(10):
        _at(connection, minute * 60.0)
        connection._check_user_binding(_other_users_cmd(2))
        connection._check_user_binding(_fire())
        connection._check_user_binding(_cue_state())

    assert connection.user_mismatch_seen is False


@pytest.mark.parametrize(
    "fixture",
    ["fade-progress.jsonl", "cue-advance.jsonl", "cmdline-and-cue-fire.jsonl"],
)
def test_a_real_capture_raises_nothing(fixture: str) -> None:
    """The captures that exposed the old check, replayed message for message on
    their own recorded clock, against the threshold that actually ships.

    fade-progress.jsonl was taken bound to user 1 on a real desk and carries
    twenty of user 2's command lines alongside a complete cue-state stream. The
    old check warned about it -- and named user 2 purely because user 2 spoke
    first.
    """
    from wer.connections.osc import OscMessage

    path = Path(__file__).parent / "fixtures" / "eos" / fixture
    connection = _eos(user=1)
    clock = {"t": 0.0}
    connection._clock = lambda: clock["t"]

    users_seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        clock["t"] = record["t"]
        if record["address"].startswith("/eos/out/user/"):
            users_seen.add(record["address"].split("/")[4])
        connection._check_user_binding(
            OscMessage(record["address"], tuple(record["args"]))
        )

    assert connection.user_mismatch_seen is False
    if fixture == "fade-progress.jsonl":
        assert {"1", "2"} <= users_seen, "fixture lost its second operator"


def test_binding_to_any_user_never_reports_a_mismatch() -> None:
    """0 means "everything", so there is nothing to disagree with."""
    connection = _eos(user=0)
    _at(connection, 0.0)._check_user_binding(_other_users_cmd(2))
    _at(connection, 600.0)._check_user_binding(_fire("58"))
    connection._check_user_binding(_fire("62"))
    assert connection.user_mismatch_seen is False


def test_nobody_is_blamed_when_only_the_bound_user_is_talking() -> None:
    """The symptom can appear with no other operator in sight -- a desk bound
    to a user that is not driving anything. Say the cue state is missing
    without inventing a culprit."""
    connection = _eos(user=2)
    _at(connection, 0.0)._check_user_binding(_other_users_cmd(2))
    _at(connection, 30.0)._check_user_binding(_fire("58"))
    connection._check_user_binding(_fire("62"))

    assert connection.user_mismatch_seen is True
    assert connection.driving_user == 0


def test_a_quiet_desk_is_not_blamed_on_the_binding() -> None:
    """No cue fires means nobody is running cues. Silence is then just silence,
    and the connection's own STALE state is what says so."""
    connection = _eos(user=1)
    for minute in range(30):
        _at(connection, minute * 60.0)
        connection._check_user_binding(_other_users_cmd(2))
    assert connection.user_mismatch_seen is False


def test_it_is_said_once_not_per_message(caplog) -> None:
    connection = _eos(user=1)
    with caplog.at_level(logging.WARNING, logger="wer.connections.eos"):
        for number in range(30):
            _at(connection, number * 30.0)
            connection._check_user_binding(_other_users_cmd(2))
            connection._check_user_binding(_fire(str(number)))
    assert connection.driving_user == 2
    assert len([r for r in caplog.records if "OSC user" in r.message]) == 1


def test_re_firing_the_same_cue_is_not_a_mismatch() -> None:
    """The healthy case that looks exactly like the broken one.

    "Go To Cue <the cue that is already live>" fires a cue and produces NO cue
    state, because the state has not changed -- so a couple of those presses
    after any quiet stretch have the same shape as a starved binding. What a
    real mismatch looks like is the SHOW advancing: different cues going by
    with nothing behind them.
    """
    connection = _eos(user=1)
    _at(connection, 0.0)._check_user_binding(_other_users_cmd(2))
    for minute in range(1, 10):
        _at(connection, minute * 60.0)._check_user_binding(_fire("58"))

    assert connection.user_mismatch_seen is False, (
        "nine presses of Go To Cue 58 on a correctly bound desk"
    )

    # The same desk, now actually starved: a second, different cue.
    _at(connection, 600.0)._check_user_binding(_fire("62"))
    assert connection.user_mismatch_seen is True


def test_a_real_capture_replayed_twice_raises_nothing() -> None:
    """cmdline-and-cue-fire.jsonl IS that press: 85 s of an idle desk, then a
    real "Go To Cue 58 #" with softkeys and command lines and no cue state at
    all behind it. One pass through it was quiet; a second pass -- the operator
    pressing it again -- warned about a desk with nothing wrong with it."""
    from wer.connections.osc import OscMessage

    path = Path(__file__).parent / "fixtures" / "eos" / "cmdline-and-cue-fire.jsonl"
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any("/fire" in r["address"] for r in records), "fixture lost its cue fire"

    connection = _eos(user=1)
    clock = {"t": 0.0}
    connection._clock = lambda: clock["t"]
    for pass_number in range(2):
        offset = pass_number * (records[-1]["t"] + 5.0)
        for record in records:
            clock["t"] = offset + record["t"]
            connection._check_user_binding(
                OscMessage(record["address"], tuple(record["args"]))
            )

    assert connection.user_mismatch_seen is False


# ---------------------------------- one bad packet must not drop the connection


def _pad(raw: bytes) -> bytes:
    """OSC pads every element to a multiple of four, terminator included."""
    return raw + b"\x00" * (4 - len(raw) % 4)


def _truncated_float() -> bytes:
    """A packet cut off inside its float argument -- the shape that escaped."""
    from wer.connections.osc import encode_message

    return encode_message("/eos/out/active/cue", 0.5)[:-2]


def _out_of_range_character() -> bytes:
    """Type tag 'c' whose code point is not a character. chr() raises."""
    return _pad(b"/eos/out/thing") + _pad(b",c") + struct.pack(">I", 0xFFFFFFFF)


@pytest.mark.parametrize(
    "packet, why",
    [
        (_truncated_float(), "float cut short"),
        (b"/eos/out/x\x00\x00,i\x00\x00\x00\x00\x00", "int cut short"),
        (_out_of_range_character(), "character out of range"),
    ],
)
def test_a_malformed_packet_is_an_osc_error_not_a_raw_exception(
    packet: bytes, why: str
) -> None:
    """struct.error is NOT a ValueError, so it was not an OscDecodeError either.

    It escaped the decoder, escaped _consume's except, escaped the recv loop,
    and was caught by the supervisor as a dead console -- which on TCP threw
    away every other packet sitting in the same recv buffer.
    """
    from wer.connections.osc import OscDecodeError, decode_packet

    with pytest.raises(OscDecodeError):
        decode_packet(packet)


def test_one_bad_packet_does_not_drop_the_connection(bus: DataBus) -> None:
    connection = EosConnection("eos", bus, EosSettings())
    connection._consume(_truncated_float())
    assert any(
        "undecodable" in entry.summary for entry in connection.monitor_entries()
    ), "a packet that could not be decoded should still show in the monitor"


def test_the_cues_behind_a_bad_packet_still_arrive(bus: DataBus) -> None:
    """A whole recv buffer used to go in the bin with the one bad packet in it."""
    from wer.connections.osc import LengthPrefixFramer, encode_message

    connection = EosConnection("eos", bus, EosSettings())
    stream = b"".join(
        LengthPrefixFramer.frame(packet)
        for packet in (
            encode_message("/eos/out/active/cue/1/10", 1.0),
            _truncated_float(),
            encode_message("/eos/out/active/cue/1/40", 1.0),
        )
    )
    for packet in LengthPrefixFramer().feed(stream):
        connection._consume(packet)

    assert bus.value("eos.cue.active.number") == "40"


def _deeply_nested_bundle(depth: int = 2000) -> bytes:
    """A bundle nested past the recursion limit: the one packet found that
    still reaches _consume's catch-all clause rather than OscDecodeError."""
    from wer.connections.osc import encode_message

    inner = encode_message("/eos/out/active/cue/text", "1/58")
    for _ in range(depth):
        inner = (
            b"#bundle\x00" + struct.pack(">Q", 1)
            + struct.pack(">i", len(inner)) + inner
        )
    return inner


def test_a_repeating_decoder_bug_does_not_burn_down_the_log(
    bus: DataBus, caplog
) -> None:
    """One traceback, not one per packet.

    The catch-all in _consume is there so a decoder bug cannot take the desk
    off the air mid-act, and it should shout the first time. But whatever
    provokes it is a property of the sender, so it repeats: at 180 KiB of
    traceback per packet, a peer resending it rolls all five 5 MiB log files
    in seconds and takes the session's diagnostics with them -- on the one
    night you would want to read them.
    """
    connection = EosConnection("eos", bus, EosSettings())
    packet = _deeply_nested_bundle()
    with caplog.at_level(logging.ERROR, logger="wer.connections.eos"):
        for _ in range(30):
            connection._consume(packet)

    with_traceback = [r for r in caplog.records if r.exc_info]
    assert len(with_traceback) == 1, "the traceback is worth exactly one telling"
    assert len(caplog.records) <= 2, "the rest are counted, not restated"
    assert connection._decoder_faults == 30
    assert len(connection.monitor_entries()) == 30, (
        "every packet is still there to look at, with its own reason"
    )


# ------------------------------- only the console counts as the console talking


def _udp_eos(bus: DataBus) -> EosConnection:
    from wer.connections.eos import EosTransport

    connection = EosConnection(
        "eos", bus, EosSettings(transport=EosTransport.UDP, port=8000)
    )
    connection._set_state(ConnectionState.WAITING)
    return connection


def test_another_apps_osc_on_the_port_is_not_the_desk_talking(bus: DataBus) -> None:
    """UDP mode binds 0.0.0.0 and accepts datagrams from anyone. A second OSC
    talker on the same port used to hold the connection at Live for the whole
    show while the overlay sat on a cue the desk had left long ago."""
    from wer.connections.osc import encode_message

    connection = _udp_eos(bus)
    connection._consume(encode_message("/1/fader1", 0.5))

    assert connection.status.state is ConnectionState.WAITING
    assert connection.status.seconds_since_packet is None
    assert connection.monitor_entries(), "foreign traffic should still be visible"


def test_junk_on_the_port_is_not_the_desk_talking(bus: DataBus) -> None:
    connection = _udp_eos(bus)
    connection._consume(b"\x00\x01keepalive from the projector\x00")

    assert connection.status.state is ConnectionState.WAITING
    assert connection.status.seconds_since_packet is None


def test_the_console_itself_still_makes_the_connection_live(bus: DataBus) -> None:
    from wer.connections.osc import encode_message

    connection = _udp_eos(bus)
    connection._consume(encode_message("/eos/out/active/cue/1/58", 1.0))

    assert connection.status.state is ConnectionState.LIVE
    assert bus.value("eos.connected") is True


def test_a_silent_desk_goes_stale_even_while_another_app_talks(bus: DataBus) -> None:
    from wer.connections.osc import encode_message

    connection = _udp_eos(bus)
    connection.stale_after = 0.2
    connection._consume(encode_message("/eos/out/active/cue/1/58", 1.0))
    assert bus.value("eos.connected") is True

    time.sleep(0.3)
    for _ in range(5):
        connection._consume(encode_message("/1/fader1", 0.5))
    connection.refresh_staleness()

    assert connection.status.state is ConnectionState.STALE
    assert bus.value("eos.connected") is False
    assert bus.value("eos.cue.active.number") == "58", (
        "a console that merely goes quiet keeps its last cue on screen"
    )


def test_foreign_traffic_is_counted_but_not_as_the_console(bus: DataBus) -> None:
    """"Nothing has arrived" and "plenty has, none of it the desk" are
    different problems and the panel has to be able to tell them apart.

    The liveness fix stopped foreign packets touching status.packets, which is
    right -- that number decides Live and Stale. But it also left a shared port
    reading "0 packets" beside a monitor pane scrolling rx rows, which reads
    like the panel contradicting itself to whoever is debugging the clash.
    """
    from wer.connections.osc import encode_message

    connection = _udp_eos(bus)
    connection._consume(encode_message("/1/fader1", 0.5))
    connection._consume(b"\x00\x01keepalive from the projector\x00")

    assert connection.status.packets == 0, "the console has still sent nothing"
    assert connection.status.noise_packets == 2
    assert connection.status.noise_bytes > 0
    assert "2 not from this source" in connection.status.describe()

    connection._consume(encode_message("/eos/out/active/cue/1/58", 1.0))
    assert connection.status.packets == 1
    assert connection.status.noise_packets == 2, "the desk's own traffic is not noise"


def test_somebody_else_on_the_port_is_said_once_not_per_packet(
    bus: DataBus, caplog
) -> None:
    from wer.connections.osc import encode_message

    connection = _udp_eos(bus)
    with caplog.at_level(logging.INFO, logger="wer.connections.eos"):
        for _ in range(20):
            connection._consume(encode_message("/1/fader1", 0.5))

    said = [r for r in caplog.records if "not Eos OSC" in r.message]
    assert len(said) == 1
    assert len(connection.monitor_entries()) == 20
