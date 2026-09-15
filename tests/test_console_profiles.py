"""Saved console profiles, and finding a working one on startup.

The central rule, and the reason this is not just "connect to the first one that
accepts": Eos accepts a TCP connection whether or not its OSC output is enabled,
and then sends nothing. Only a console that actually SENDS counts as found.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from wer import SHOW_SCHEMA_VERSION
from wer.connections.console_finder import ConsoleFinder, probe_console
from wer.core.showfile import ConsoleProfile, ShowFile, load_show, migrate, save_show


# ------------------------------------------------------------------ the model


def test_a_profile_summarises_itself_for_a_list() -> None:
    profile = ConsoleProfile(name="House", host="10.101.90.101", port=3032)
    assert "10.101.90.101:3032" in profile.summary


def test_a_new_profile_has_never_connected() -> None:
    assert ConsoleProfile().has_connected is False


def test_profiles_round_trip(tmp_path: Path) -> None:
    show = ShowFile()
    show.consoles = [
        ConsoleProfile(name="House Ion XE", host="10.101.90.101",
                       user=2, notes="booth, house left", last_connected=1234.5),
        ConsoleProfile(name="Rehearsal Nomad", host="192.168.1.20"),
    ]
    show.active_console = "House Ion XE"

    path = tmp_path / "show.wer"
    save_show(show, path)
    loaded = load_show(path)

    assert [p.name for p in loaded.consoles] == [
        "House Ion XE", "Rehearsal Nomad"
    ]
    assert loaded.consoles[0].user == 2
    assert loaded.consoles[0].notes == "booth, house left"
    assert loaded.consoles[0].last_connected == 1234.5
    assert loaded.active_console == "House Ion XE"


# ------------------------------------------------------------------ migration


def test_a_v1_file_gains_a_profile_from_its_single_console() -> None:
    """Nobody should lose the address they had working."""
    data = {
        "schema_version": 1,
        "eos": {"enabled": True, "host": "10.101.90.101", "port": 3032, "user": 2},
    }
    out = migrate(dict(data))

    assert out["schema_version"] == SHOW_SCHEMA_VERSION
    assert len(out["consoles"]) == 1
    console = out["consoles"][0]
    assert console["host"] == "10.101.90.101"
    assert console["user"] == 2
    assert out["active_console"] == console["name"]


def test_the_migrated_profile_is_named_after_its_host() -> None:
    """"Console 1" tells you nothing; an address you recognise does."""
    out = migrate({"schema_version": 1, "eos": {"host": "10.101.90.101"}})
    assert out["consoles"][0]["name"] == "10.101.90.101"


def test_a_migrated_localhost_console_gets_a_readable_name() -> None:
    out = migrate({"schema_version": 1, "eos": {"host": "127.0.0.1"}})
    assert out["consoles"][0]["name"] == "This computer"


def test_a_v1_console_that_worked_sorts_above_ones_added_later() -> None:
    """It was enabled, so it had connected; it should not sort last."""
    out = migrate({"schema_version": 1, "eos": {"enabled": True, "host": "10.0.0.5"}})
    assert out["consoles"][0]["last_connected"] > 0


def test_a_v1_console_that_never_worked_does_not_claim_it_had() -> None:
    out = migrate({"schema_version": 1, "eos": {"enabled": False, "host": "10.0.0.5"}})
    assert out["consoles"][0]["last_connected"] == 0.0


def test_migrating_a_current_file_changes_nothing() -> None:
    data = {"schema_version": SHOW_SCHEMA_VERSION,
            "consoles": [{"name": "A", "host": "1.2.3.4"}]}
    out = migrate(dict(data))
    assert out["consoles"] == data["consoles"]


def test_a_real_v1_file_loads(tmp_path: Path) -> None:
    path = tmp_path / "old.wer"
    path.write_text(json.dumps({
        "schema_version": 1,
        "show_name": "The Comedy of Errors",
        "eos": {"enabled": True, "host": "10.101.90.101", "port": 3032},
    }), encoding="utf-8")

    show = load_show(path)
    assert show.show_name == "The Comedy of Errors"
    assert len(show.consoles) == 1
    assert show.consoles[0].host == "10.101.90.101"


# -------------------------------------------------------------------- ordering


def test_the_most_recently_used_console_is_tried_first() -> None:
    """No favourite to mark: the one you used last is the one you want."""
    profiles = [
        ConsoleProfile(name="Old", last_connected=1000.0),
        ConsoleProfile(name="Newest", last_connected=3000.0),
        ConsoleProfile(name="Middle", last_connected=2000.0),
    ]
    assert [p.name for p in ConsoleFinder.order(profiles)] == [
        "Newest", "Middle", "Old"
    ]


def test_a_console_that_never_connected_is_tried_last_but_is_tried() -> None:
    """It may be a rig set up in advance for tonight."""
    profiles = [
        ConsoleProfile(name="Never"),
        ConsoleProfile(name="Used", last_connected=1000.0),
    ]
    order = [p.name for p in ConsoleFinder.order(profiles)]
    assert order == ["Used", "Never"]


def test_a_disabled_console_is_skipped() -> None:
    profiles = [
        ConsoleProfile(name="Off", enabled=False, last_connected=9999.0),
        ConsoleProfile(name="On", last_connected=1.0),
    ]
    assert [p.name for p in ConsoleFinder.order(profiles)] == ["On"]


# --------------------------------------------------------------------- probing


@pytest.fixture()
def silent_server():
    """A server that accepts and says nothing -- exactly what Eos does with OSC off."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    accepted: list[socket.socket] = []

    def accept_forever() -> None:
        while True:
            try:
                client, _ = server.accept()
                accepted.append(client)
            except OSError:
                return

    threading.Thread(target=accept_forever, daemon=True).start()
    yield server.getsockname()[1]
    for client in accepted:
        client.close()
    server.close()


def test_a_silent_console_is_not_counted_as_found(silent_server) -> None:
    """THE central rule.

    An auto-connect that stopped at the first socket to accept would attach to a
    console with OSC switched off and sit silent all night.
    """
    result = probe_console("127.0.0.1", silent_server, name="Silent",
                           listen_seconds=1.5)
    assert result.reachable is True
    assert result.talking is False
    assert result.usable is False
    assert "silent" in result.describe().lower()


def test_a_silent_console_says_what_to_check(silent_server) -> None:
    """A server that accepts and sends nothing has no more specific detail,
    so the OSC-output hint is the right thing to show."""
    result = probe_console("127.0.0.1", silent_server, listen_seconds=1.0)
    assert "osc" in result.describe().lower()


def test_an_unreachable_console_is_reported(silent_server) -> None:
    result = probe_console("127.0.0.1", 1, name="Nothing", listen_seconds=1.0)
    assert result.reachable is False
    assert result.usable is False
    assert result.detail


def test_probing_is_quick_when_nothing_is_there() -> None:
    """Walking a list of five venues must not take a minute."""
    started = time.perf_counter()
    probe_console("127.0.0.1", 1, listen_seconds=5.0, connect_timeout=2.0)
    assert time.perf_counter() - started < 4.0


# ---------------------------------------------------------------- the search


def wait_for(event: threading.Event, timeout: float = 40.0) -> bool:
    return event.wait(timeout)


def test_the_search_falls_through_to_a_working_console(silent_server) -> None:
    """Two duds ahead of the good one, and the duds are the more recent."""
    profiles = [
        ConsoleProfile(name="Dead venue", host="127.0.0.1", port=1,
                       last_connected=3000.0),
        ConsoleProfile(name="Silent desk", host="127.0.0.1", port=silent_server,
                       last_connected=2000.0),
        ConsoleProfile(name="Good one", host="127.0.0.1", port=_talking_port(),
                       last_connected=1000.0),
    ]
    done = threading.Event()
    chosen: list = []
    finder = ConsoleFinder(
        sweep=False,
        on_finished=lambda profile, results: (chosen.append(profile), done.set())
    )
    finder.start(profiles, listen_seconds=1.5)
    assert wait_for(done), "the search never finished"
    assert chosen[0] is not None, "nothing was found"
    assert chosen[0].name == "Good one"


def test_the_search_reports_when_nothing_answers(silent_server) -> None:
    profiles = [
        ConsoleProfile(name="Dead", host="127.0.0.1", port=1),
        ConsoleProfile(name="Silent", host="127.0.0.1", port=silent_server),
    ]
    done = threading.Event()
    outcome: list = []
    finder = ConsoleFinder(
        sweep=False,          # pass one only; pass two would find a real desk
        on_finished=lambda profile, results: (
            outcome.append((profile, results)), done.set()
        )
    )
    finder.start(profiles, listen_seconds=1.0)
    assert wait_for(done)
    profile, results = outcome[0]
    assert profile is None
    assert len(results) == 2
    assert all(not result.usable for result in results)


def test_the_search_reports_progress(silent_server) -> None:
    """During a tech, silence for ten seconds looks like a hang."""
    messages: list[str] = []
    done = threading.Event()
    finder = ConsoleFinder(
        sweep=False,
        on_progress=messages.append,
        on_finished=lambda profile, results: done.set(),
    )
    finder.start(
        [ConsoleProfile(name="Dead", host="127.0.0.1", port=1)], listen_seconds=1.0
    )
    assert wait_for(done)
    assert any("Trying" in message for message in messages)


def test_an_empty_list_finishes_rather_than_hanging() -> None:
    done = threading.Event()
    chosen: list = []
    finder = ConsoleFinder(
        sweep=False,
        on_finished=lambda profile, results: (chosen.append(profile), done.set())
    )
    finder.start([], listen_seconds=1.0)
    assert wait_for(done, timeout=10.0)
    assert chosen[0] is None


def _talking_port() -> int:
    """A server that sends a valid OSC message the moment anything connects.

    Stands in for a console with OSC output enabled, which announces itself
    with a state dump within about a second of a client connecting.
    """
    from wer.connections.osc import LengthPrefixFramer, encode_message

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    port = server.getsockname()[1]

    def talk() -> None:
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return
            try:
                client.sendall(
                    LengthPrefixFramer.frame(
                        encode_message("/eos/out/active/cue/1/58", 1.0)
                    )
                )
            except OSError:
                pass

    threading.Thread(target=talk, daemon=True).start()
    return port


def test_a_talking_console_is_found() -> None:
    result = probe_console("127.0.0.1", _talking_port(), name="Live desk",
                           listen_seconds=3.0)
    assert result.talking is True
    assert result.usable is True
    assert "talking" in result.describe()


# ------------------------------------------------- the UDP sweep and its noise


def _free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _udp_talker(port: int, payloads: list[bytes]) -> threading.Event:
    """Send to a port until told to stop. Stands in for whatever else is on
    the show network."""
    stop = threading.Event()

    def talk() -> None:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            while not stop.wait(0.1):
                for payload in payloads:
                    try:
                        sender.sendto(payload, ("127.0.0.1", port))
                    except OSError:
                        return
        finally:
            sender.close()

    threading.Thread(target=talk, daemon=True).start()
    return stop


def test_something_else_on_the_port_is_not_a_console() -> None:
    """The sweep writes whatever it finds back onto the profile permanently.

    Accepting any datagram at all meant a projector keepalive or another OSC app
    on UDP 8000 or 8123 -- both common -- moved the saved profile onto that port
    and announced "Connected". Every later launch then found the same junk on
    pass one and never probed the real desk again.
    """
    from wer.connections.osc import encode_message

    port = _free_udp_port()
    stop = _udp_talker(port, [
        b"\x00\x01keepalive from the projector\x00",
        encode_message("/1/fader1", 0.5),
    ])
    try:
        result = probe_console("127.0.0.1", port, transport="UDP",
                               name="Booth Ion", listen_seconds=1.5)
    finally:
        stop.set()

    assert result.talking is False
    assert result.usable is False
    assert result.detail, "say what was heard, or the sweep looks broken"


def test_a_console_transmitting_udp_is_found() -> None:
    """The real Ion XE that started all this was found sending UDP to 8123."""
    from wer.connections.osc import encode_message

    port = _free_udp_port()
    stop = _udp_talker(port, [encode_message("/eos/out/active/cue/1/58", 1.0)])
    try:
        result = probe_console("127.0.0.1", port, transport="UDP",
                               name="Booth Ion", listen_seconds=3.0)
    finally:
        stop.set()

    assert result.talking is True
    assert result.usable is True


def test_a_console_transmitting_a_bundle_is_found() -> None:
    """Eos sends bundles as well as bare messages; both are the desk talking."""
    from wer.connections.osc import BUNDLE_MARKER, encode_message

    inner = encode_message("/eos/out/active/cue/text", "1/58 Adriana 3.0 100%")
    bundle = (
        BUNDLE_MARKER + struct.pack(">Q", 1) + struct.pack(">i", len(inner)) + inner
    )
    port = _free_udp_port()
    stop = _udp_talker(port, [bundle])
    try:
        result = probe_console("127.0.0.1", port, transport="UDP",
                               name="Booth Ion", listen_seconds=3.0)
    finally:
        stop.set()

    assert result.talking is True


# ------------------------------------------------------------------ the panel


@pytest.fixture()
def panel(qt_app):
    from wer.connections.eos import EosConnection, EosSettings
    from wer.core.databus import DataBus
    from wer.ui.connection_panel import EosConnectionPanel

    widget = EosConnectionPanel(EosConnection("eos", DataBus(), EosSettings()))
    yield widget
    widget.deleteLater()


def test_the_panel_shows_the_settings_of_the_selected_console(panel) -> None:
    profiles = [
        ConsoleProfile(name="House", host="10.101.90.101", port=3032, user=2),
        ConsoleProfile(name="Laptop", host="127.0.0.1", port=3040, user=5),
    ]
    panel.set_profiles(profiles, "Laptop")
    assert panel.active_profile.name == "Laptop"

    panel._profile_box.setCurrentIndex(panel._profile_box.findData("House"))
    assert panel._host.text() == "10.101.90.101"
    assert panel._user.value() == 2


def test_editing_the_form_writes_back_to_the_selected_console(panel) -> None:
    """The form is a view of one profile, not a separate set of settings."""
    profiles = [ConsoleProfile(name="Venue", host="127.0.0.1")]
    panel.set_profiles(profiles, "Venue")

    panel._host.setText("10.101.90.55")
    panel._user.setValue(4)
    panel.apply_settings()

    assert profiles[0].host == "10.101.90.55"
    assert profiles[0].user == 4


def test_a_console_that_never_connected_says_so_in_the_list(panel) -> None:
    """Otherwise a typo sits in the list looking exactly like a working desk."""
    panel.set_profiles([ConsoleProfile(name="Typo")], "Typo")
    assert "never connected" in panel._profile_box.itemText(0)


def test_the_last_console_cannot_be_deleted(panel) -> None:
    """There must always be something to edit."""
    panel.set_profiles([ConsoleProfile(name="Only one")], "Only one")
    assert panel._delete_profile_button.isEnabled() is False

    panel.set_profiles(
        [ConsoleProfile(name="A"), ConsoleProfile(name="B")], "A"
    )
    assert panel._delete_profile_button.isEnabled() is True


def test_a_duplicate_does_not_inherit_the_original_place_in_the_queue(
    panel, monkeypatch
) -> None:
    """A copy has not connected under its own name, so it must not claim to."""
    from PySide6.QtWidgets import QInputDialog

    profiles = [ConsoleProfile(name="House", host="10.101.90.101",
                               last_connected=5000.0)]
    panel.set_profiles(profiles, "House")
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("House B", True)))

    panel._duplicate_profile()

    copy = profiles[1]
    assert copy.name == "House B"
    assert copy.host == "10.101.90.101"
    assert copy.last_connected == 0.0


def test_two_consoles_cannot_share_a_name(panel, monkeypatch) -> None:
    """The name is how the show file refers to the active console."""
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    profiles = [ConsoleProfile(name="House")]
    panel.set_profiles(profiles, "House")
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("House", True)))
    warned: list = []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a)))

    panel._new_profile()

    assert len(profiles) == 1
    assert warned


def test_trying_every_console_greys_out_when_auto_connect_is_off(panel) -> None:
    panel.set_startup_options(auto_connect=False, try_all=True)
    assert panel._try_all.isEnabled() is False

    panel.set_startup_options(auto_connect=True, try_all=True)
    assert panel._try_all.isEnabled() is True


def test_the_startup_switches_report_changes(panel) -> None:
    panel.set_startup_options(auto_connect=True, try_all=True)
    seen: list = []
    panel.startup_options_changed.connect(lambda a, t: seen.append((a, t)))

    panel._try_all.setChecked(False)

    assert seen[-1] == (True, False)


# ------------------------------------------------------------- the whole app


def test_a_fresh_install_has_a_console_to_edit(qt_app) -> None:
    """An empty list would leave the panel with nothing to show."""
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        assert window.show_file.consoles
        assert window.active_console is not None
        assert window.show_file.active_console == window.active_console.name
    finally:
        window.close()
        window.deleteLater()


def test_a_fresh_install_does_not_go_dialling_addresses_nobody_chose(qt_app) -> None:
    """Nothing has ever connected, so there is nothing to reconnect to."""
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        assert not any(p.has_connected for p in window.show_file.consoles)
        assert window.eos.is_running is False
        assert window.finder.is_running is False
    finally:
        window.close()
        window.deleteLater()


def test_switching_console_switches_what_the_connection_will_dial(qt_app) -> None:
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.show_file.consoles = [
            ConsoleProfile(name="House", host="10.101.90.101", port=3032),
            ConsoleProfile(name="Laptop", host="127.0.0.1", port=3032),
        ]
        window._profiles_changed()

        window.select_console("House")
        assert window.eos.settings.host == "10.101.90.101"

        window.select_console("Laptop")
        assert window.eos.settings.host == "127.0.0.1"
        assert window.show_file.active_console == "Laptop"
    finally:
        window.close()
        window.deleteLater()


def test_switching_to_a_console_that_is_not_there_does_nothing(qt_app) -> None:
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        before = window.show_file.active_console
        window.select_console("A console nobody saved")
        assert window.show_file.active_console == before
    finally:
        window.close()
        window.deleteLater()


def test_the_dropdown_follows_a_console_chosen_by_the_search(qt_app) -> None:
    """Found live: the search connected to one desk while the dropdown still
    named another, with the fields below describing the wrong one."""
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.show_file.consoles = [
            ConsoleProfile(name="House", host="10.101.90.101",
                           notes="booth, house left"),
            ConsoleProfile(name="Laptop", host="127.0.0.1", notes="the nomad"),
        ]
        window.show_file.active_console = "House"
        window._profiles_changed()

        window.select_console("Laptop")

        panel = window.eos_panel
        assert panel.active_profile.name == "Laptop"
        assert panel._host.text() == "127.0.0.1"
        assert panel._notes.text() == "the nomad"
    finally:
        window.close()
        window.deleteLater()


def test_picking_from_the_dropdown_does_not_re_enter_itself(qt_app) -> None:
    """select_console is wired to the dropdown, and it moves the dropdown."""
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.show_file.consoles = [
            ConsoleProfile(name="One", host="10.0.0.1"),
            ConsoleProfile(name="Two", host="10.0.0.2"),
            ConsoleProfile(name="Three", host="10.0.0.3"),
        ]
        window.show_file.active_console = "One"
        window._profiles_changed()

        panel = window.eos_panel
        panel._profile_box.setCurrentIndex(panel._profile_box.findData("Three"))

        assert window.show_file.active_console == "Three"
        assert window.eos.settings.host == "10.0.0.3"
        assert panel._profile_box.count() == 3
    finally:
        window.close()
        window.deleteLater()


# ------------------------------------------- stale data across a desk change


def test_switching_desks_forgets_the_old_desks_data(qt_app) -> None:
    """A cue label from the console you just left is not "last known good",
    it is wrong. Reported from the field as a show file appearing to keep
    labels from the previous one."""
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.show_file.consoles = [
            ConsoleProfile(name="Desk A", host="10.0.0.1"),
            ConsoleProfile(name="Desk B", host="10.0.0.2"),
        ]
        window.show_file.active_console = "Desk A"
        window._profiles_changed()

        window.eos.publish("eos.cue.active.label", "Start of Duke Scene")
        window.eos.publish("eos.show.name", "The Comedy of Errors")
        assert window.bus.get("eos.cue.active.label") is not None

        window.select_console("Desk B")

        assert window.bus.get("eos.cue.active.label") is None
        assert window.bus.get("eos.show.name") is None
    finally:
        window.close()
        window.deleteLater()


def test_reselecting_the_same_console_keeps_the_picture(qt_app) -> None:
    """Blanking the overlay because someone re-picked the current desk would
    be worse than the bug being fixed."""
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.show_file.consoles = [ConsoleProfile(name="Desk A", host="10.0.0.1")]
        window.show_file.active_console = "Desk A"
        window._profiles_changed()
        window.eos.publish("eos.cue.active.label", "Start of Duke Scene")

        window.select_console("Desk A")

        assert window.bus.get("eos.cue.active.label") is not None
    finally:
        window.close()
        window.deleteLater()


def test_a_dropped_link_still_leaves_the_last_cue_on_screen(qt_app) -> None:
    """The existing, deliberate behaviour: a console reboot mid-show goes
    stale rather than blank. The fix above must not have changed it."""
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.eos.publish("eos.cue.active.label", "Start of Duke Scene")
        window.eos.stop()
        assert window.bus.get("eos.cue.active.label") is not None
    finally:
        window.close()
        window.deleteLater()


def test_the_parser_forgets_the_latched_cue_number() -> None:
    """The latched number is what turns a bare fade message into a cue
    identity, so carrying it would label the new desk's fade with the old
    desk's cue."""
    from wer.connections.eos_parser import EosParser
    from wer.connections.osc import OscMessage

    parser = EosParser()
    parser.handle(OscMessage("/eos/out/active/cue/1/58", [1.0]))
    assert parser._active_number == "58"

    parser.reset()

    assert parser._active_number is None
    assert parser._active_list is None
    assert parser._fade_duration is None


# --------------------------------------------- finding a desk that moved


def test_the_saved_settings_are_tried_before_anything_is_guessed() -> None:
    """A working setup must never be slowed down by the sweep."""
    profile = ConsoleProfile(name="House", transport="TCP", port=3032,
                             framing="OSC 1.1 (SLIP)")
    first = ConsoleFinder.endpoints_for(profile)[0]
    assert (first[1], first[2], first[3]) == ("TCP", 3032, "OSC 1.1 (SLIP)")


def test_the_sweep_covers_the_endpoints_a_real_desk_used() -> None:
    """Not documentation -- evidence. Hudson's Ion XE was found transmitting
    UDP to 8123 while both TCP defaults sat silent, and the same desk later
    answered 3032 in SLIP rather than length-prefix."""
    combos = {(t, p, f) for _, t, p, f in ConsoleFinder.COMMON_ENDPOINTS}
    assert ("UDP", 8123, "OSC 1.0 (length prefix)") in combos
    assert ("TCP", 3032, "OSC 1.1 (SLIP)") in combos
    assert ("TCP", 3032, "OSC 1.0 (length prefix)") in combos


def test_the_saved_endpoint_is_not_tried_twice() -> None:
    profile = ConsoleProfile(transport="TCP", port=3032,
                             framing="OSC 1.0 (length prefix)")
    endpoints = ConsoleFinder.endpoints_for(profile)
    triples = [(t, p, f) for _, t, p, f in endpoints]
    assert len(triples) == len(set(triples))


def test_a_framing_mismatch_is_not_reported_as_silence(silent_server) -> None:
    """The bug this came from: the probe knew the framing was wrong and said
    so in detail, then describe() overrode it with "OSC is probably switched
    off" -- which sent Hudson to the desk's settings for an hour while the
    desk was transmitting perfectly well."""
    from wer.connections.console_finder import ProbeResult

    result = ProbeResult(
        "desk", reachable=True, talking=False,
        detail="answered, but not in this framing (implausible length prefix -1)",
    )
    described = result.describe()
    assert "not in this framing" in described
    assert "switched off" not in described


def test_a_genuinely_silent_console_still_says_so() -> None:
    """The old message is right when there is nothing more specific to say."""
    from wer.connections.console_finder import ProbeResult

    result = ProbeResult("desk", reachable=True, talking=False, detail="")
    assert "switched off" in result.describe()


def test_the_sweep_can_be_switched_off() -> None:
    finder = ConsoleFinder(sweep=False)
    assert finder.sweep is False
    assert ConsoleFinder().sweep is True


def test_a_found_console_keeps_the_settings_that_worked() -> None:
    """Finding the desk and then dialling the saved port would be pointless."""
    import threading

    port = _talking_port()
    profile = ConsoleProfile(name="Moved", host="127.0.0.1",
                             transport="TCP", port=1,
                             framing="OSC 1.0 (length prefix)")
    # Put the live port in the sweep list for this test only.
    original = ConsoleFinder.COMMON_ENDPOINTS
    ConsoleFinder.COMMON_ENDPOINTS = (
        ("live", "TCP", port, "OSC 1.0 (length prefix)"),
    )
    try:
        done = threading.Event()
        found = []
        finder = ConsoleFinder(
            on_finished=lambda p, r: (found.append(p), done.set())
        )
        finder.start([profile], listen_seconds=2.0)
        assert done.wait(40), "the search never finished"
        assert found[0] is not None
        assert profile.port == port
    finally:
        ConsoleFinder.COMMON_ENDPOINTS = original


def test_pass_two_only_runs_when_pass_one_found_nothing() -> None:
    """Guessing must never redirect a profile that names a specific desk to a
    different one on the same host. It is a last resort, not a shortcut."""
    import threading

    port = _talking_port()
    good = ConsoleProfile(name="Good", host="127.0.0.1", port=port,
                          last_connected=10.0)
    original = ConsoleFinder.COMMON_ENDPOINTS
    ConsoleFinder.COMMON_ENDPOINTS = (("elsewhere", "TCP", 9, "OSC 1.0 (length prefix)"),)
    try:
        done = threading.Event()
        found = []
        finder = ConsoleFinder(on_finished=lambda p, r: (found.append(p), done.set()))
        finder.start([good], listen_seconds=2.0)
        assert done.wait(40)
        assert found[0] is not None
        assert good.port == port, "pass one succeeded; nothing should have moved"
        assert len(finder.results) == 1, "pass two ran when it should not have"
    finally:
        ConsoleFinder.COMMON_ENDPOINTS = original


def test_a_single_saved_console_still_gets_searched_for(qt_app) -> None:
    """Found in the field: with exactly one saved console, startup dialled the
    saved settings directly and never ran the finder, so the recovery sweep
    could not help. That is the setup that most needs it -- one desk, whose
    transport or port has changed since last time."""
    from wer.ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.show_file.consoles = [
            ConsoleProfile(name="The only desk", host="127.0.0.1",
                           port=1, last_connected=1000.0)
        ]
        window.show_file.active_console = "The only desk"
        window.show_file.eos.auto_connect = True
        window.show_file.eos.try_all_on_startup = True
        window._profiles_changed()

        searched = []
        window.find_console = lambda: searched.append(True)
        window._maybe_auto_connect()

        assert searched, "one console should still be searched for, not just dialled"
    finally:
        window.close()
        window.deleteLater()
