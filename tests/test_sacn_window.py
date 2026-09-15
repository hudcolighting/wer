"""The main window's sACN wiring.

A new window sends nothing; a show saved with sACN on starts it, and closing
stops it. Settings from the tab reach the sender, live where the stream allows
and by restarting it where it does not. The status bar pins a lasting failure
once and a passing one not at all, and pins Automatic moving the stream.

Nothing here transmits. Wherever something is switched on, the real sender is
swapped for a fake or its start and stop are replaced, and the window never
saves: a window closed with sACN on would save it on, and every later window
in the run would start sending.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from wer.connections.base import ConnectionState, ConnectionStatus
from wer.connections.sacn import new_cid
from wer.connections.sacn_sender import SacnSender, SacnSettings


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.is_running = False
        self.settings = SacnSettings()
        self.status = ConnectionStatus()
        self.live_changes = True
        self.choice = None
        self.moves = 0
        self.last_move = ""

    def start(self) -> None:
        self.calls.append("start")
        self.is_running = True

    def stop(self) -> None:
        self.calls.append("stop")
        self.is_running = False

    def update_settings(self, settings: SacnSettings) -> bool:
        self.calls.append("update")
        if self.live_changes:
            self.settings = settings
        return self.live_changes

    def refresh_staleness(self) -> None:
        pass


@pytest.fixture()
def window(qt_app, monkeypatch):
    import wer.ui.main_window as main_window_module

    monkeypatch.setattr(main_window_module, "save_autosave", lambda show: None)
    made = main_window_module.MainWindow()
    yield made
    made._take_in_progress = False
    made.show_file.sacn.enabled = False
    made._pinned_messages.clear()
    made.close()
    made.deleteLater()


def faked(window) -> FakeSender:
    window.sacn.stop()
    fake = window.sacn = FakeSender()
    return fake


def test_a_new_window_sends_nothing(window) -> None:
    assert window.show_file.sacn.enabled is False
    assert not window.sacn.is_running
    assert window.sacn_panel is not None
    assert window.show_file.sacn.cid, "the source identity is made on first use"


def test_a_change_on_the_tab_reaches_the_sender(window) -> None:
    fake = faked(window)
    config = window.show_file.sacn
    config.enabled = True
    window.sacn_panel.settings_changed.emit()
    assert fake.calls == ["start"]


def test_switching_on_starts_it_and_a_live_change_does_not_restart_it(window) -> None:
    fake = faked(window)
    config = window.show_file.sacn
    config.universe, config.address, config.enabled = 5, 7, True

    window._sacn_settings_changed()
    assert fake.calls == ["start"]
    assert (fake.settings.universe, fake.settings.address) == (5, 7)

    config.priority = 90
    window._sacn_settings_changed()
    assert fake.calls == ["start", "update"]
    assert fake.settings.priority == 90

    fake.live_changes = False
    config.universe = 6
    window._sacn_settings_changed()
    assert fake.calls[-3:] == ["update", "stop", "start"]
    assert fake.settings.universe == 6

    config.enabled = False
    window._sacn_settings_changed()
    assert fake.calls[-1] == "stop"


def test_a_show_saved_with_sacn_on_starts_it_and_closing_stops_it(qt_app, monkeypatch) -> None:
    import wer.ui.main_window as main_window_module
    from wer.core.showfile import ShowFile

    calls: list[str] = []
    monkeypatch.setattr(SacnSender, "start", lambda self: calls.append("start"))
    monkeypatch.setattr(SacnSender, "stop", lambda self, timeout=3.0: calls.append("stop"))
    monkeypatch.setattr(main_window_module, "save_autosave", lambda show: None)
    show = ShowFile()
    show.sacn.enabled = True
    monkeypatch.setattr(main_window_module, "load_autosave", lambda: show)

    made = main_window_module.MainWindow()
    try:
        assert calls == ["start"]
    finally:
        made.close()
        made.deleteLater()
    assert calls[-1] == "stop", "closing Wer did not stop the sACN output"


def test_a_lasting_failure_is_pinned_once_and_a_passing_one_not_at_all(window) -> None:
    fake = faked(window)
    fake.settings = SacnSettings(universe=5, address=7, cid=new_cid())
    fake.status.state = ConnectionState.ERROR
    fake.status.detail = "Could not send sACN on Ethernet 2 (10.101.90.150): unreachable"
    window.show_file.sacn.enabled = True
    window._pinned_messages.clear()
    message = f"sACN status output stopped sending: {fake.status.detail}"

    window.SACN_PIN_AFTER = 60.0
    for _ in range(3):
        window._refresh_sacn_status()
    assert message not in window._pinned_messages, "a network still coming up was pinned"

    window.SACN_PIN_AFTER = 0.0
    for _ in range(3):
        window._refresh_sacn_status()
    assert window._pinned_messages.count(message) == 1

    fake.status.state = ConnectionState.LIVE
    window._refresh_sacn_status()
    fake.status.state = ConnectionState.ERROR
    for _ in range(3):
        window._refresh_sacn_status()
    assert window._pinned_messages.count(message) == 1, (
        "a failure that came back was pinned again, pushing older pins off the list"
    )


def test_settings_that_cannot_be_sent_are_not_pinned(window) -> None:
    fake = faked(window)
    fake.status.state = ConnectionState.ERROR
    fake.status.detail = "Choose a universe to send on (1-63999)."
    window.show_file.sacn.enabled = True
    window.SACN_PIN_AFTER = 0.0
    window._pinned_messages.clear()
    for _ in range(3):
        window._refresh_sacn_status()
    assert window._pinned_messages == []


def test_automatic_moving_the_stream_is_pinned_once(window) -> None:
    fake = faked(window)
    fake.status.state = ConnectionState.LIVE
    fake.moves = 1
    fake.last_move = (
        "Automatic moved it from Ethernet 2 (10.101.90.150) to Wi-Fi "
        "(192.168.1.20), so receivers on the first no longer get it."
    )
    window.show_file.sacn.enabled = True
    window._pinned_messages.clear()
    for _ in range(3):
        window._refresh_sacn_status()
    assert window._pinned_messages.count(f"sACN status output: {fake.last_move}") == 1


def test_a_second_copy_of_wer_sends_with_an_identity_of_its_own(window) -> None:
    """Receivers tell sources apart by CID alone. A second copy on the same
    settings would otherwise look like one source jumping between two streams."""
    saved = window.show_file.sacn.cid
    claim = window.settings_claim
    window.settings_claim = SimpleNamespace(may_save=False, release=lambda: None)
    try:
        first = window._sacn_settings_from_show().cid
        assert first and first != saved
        assert window._sacn_settings_from_show().cid == first, "it changed within the session"
    finally:
        window.settings_claim = claim
    assert window._sacn_settings_from_show().cid == saved


def test_the_status_pulses_only_for_a_take_that_is_writing(window, monkeypatch) -> None:
    recorder_type = type(window.recorder)
    monkeypatch.setattr(recorder_type, "is_writing", property(lambda self: True))
    assert window._sacn_sees_recording() is False, "no take in progress"
    window._take_in_progress = True
    try:
        assert window._sacn_sees_recording() is True
        monkeypatch.setattr(recorder_type, "is_writing", property(lambda self: False))
        assert window._sacn_sees_recording() is False
    finally:
        window._take_in_progress = False
