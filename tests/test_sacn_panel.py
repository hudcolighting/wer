"""The sACN output tab.

A stub sender and made-up adapter lists: nothing here sends anything or reads
the real adapter table.
"""

from __future__ import annotations

import time

import pytest

from wer.connections.adapters import AdapterChoice, NetworkAdapter
from wer.connections.base import ConnectionStatus
from wer.core.showfile import SacnConfig

WIRED = NetworkAdapter("{wired}", "Ethernet 2", "10.101.90.150", True, "ethernet")
WIFI = NetworkAdapter("{wifi}", "Wi-Fi", "192.168.1.20", True, "wifi")


class StubSender:
    def __init__(self) -> None:
        self.status = ConnectionStatus()
        self.is_running = False
        self.choice: AdapterChoice | None = None

    def refresh_staleness(self) -> None:
        pass


def pump_until(qt_app, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    return predicate()


@pytest.fixture()
def make_panel(qt_app):
    made = []

    def build(config: SacnConfig | None = None, adapters=(WIRED,), route="10.101.90.150"):
        from wer.ui.sacn_panel import SacnPanel

        panel = SacnPanel(
            config or SacnConfig(), StubSender(),
            list_adapters=lambda: list(adapters), route_source_ip=lambda: route,
        )
        panel._timer.stop()
        made.append(panel)
        assert pump_until(qt_app, lambda: not panel._listing), "the adapter list never came back"
        return panel

    yield build
    for panel in made:
        panel.deleteLater()
    qt_app.processEvents()


def test_a_new_tab_is_off_on_universe_101_address_1(make_panel) -> None:
    """Hudson's defaults, 13 Sep 2026."""
    panel = make_panel()
    assert (panel._universe.value(), panel._address.value()) == (101, 1)
    assert panel._priority.value() == 100
    assert panel._per_address.isChecked()
    assert not panel._enabled.isChecked() and panel._enabled.isEnabled()


def test_a_change_on_the_tab_goes_into_the_settings_and_is_announced(make_panel) -> None:
    config = SacnConfig()
    panel = make_panel(config)
    announced: list[bool] = []
    panel.settings_changed.connect(lambda: announced.append(True))
    panel._priority.setValue(90)
    panel._universe.setValue(12)
    panel._enabled.setChecked(True)
    assert (config.priority, config.universe, config.enabled) == (90, 12, True)
    assert len(announced) == 2, "the universe goes with the next change, not apart"


def test_stepping_through_universes_applies_only_where_it_stops(make_panel, qt_app) -> None:
    """Holding an arrow key or turning the wheel emits every step, and each
    universe applied would end one stream and start another."""
    config = SacnConfig()
    panel = make_panel(config)
    announced: list[int] = []
    panel.settings_changed.connect(lambda: announced.append(config.universe))
    for value in range(102, 110):
        panel._universe.setValue(value)
    assert announced == [] and config.universe == 109
    assert pump_until(qt_app, lambda: bool(announced), 3.0)
    assert announced == [109]


def test_loading_the_tab_announces_nothing(make_panel, qt_app) -> None:
    config = SacnConfig(universe=7, address=9)
    panel = make_panel(config)
    announced: list[bool] = []
    panel.settings_changed.connect(lambda: announced.append(True))
    panel.load_settings()
    panel.refresh_adapters()
    pump_until(qt_app, lambda: not panel._listing)
    time.sleep(0.9)
    qt_app.processEvents()
    assert announced == []
    assert (config.universe, config.address) == (7, 9)


def test_turning_per_address_priority_off_warns_about_the_whole_universe(make_panel) -> None:
    panel = make_panel()
    assert panel._warning.isHidden()
    panel._per_address.setChecked(False)
    assert not panel._warning.isHidden()
    assert "all 512 addresses" in panel._warning.text()


def test_automatic_names_the_network_it_picked(make_panel) -> None:
    panel = make_panel()
    assert panel._network.itemText(0) == "Automatic: Ethernet 2 (10.101.90.150)"
    assert panel._warning.isHidden()


def test_automatic_on_wifi_warns(make_panel) -> None:
    panel = make_panel(adapters=(WIFI,), route="192.168.1.20")
    assert "Wi-Fi" in panel._warning.text() and not panel._warning.isHidden()


def test_the_tab_follows_where_a_running_sender_really_is(make_panel) -> None:
    """The tab's own look at the networks is from when it was filled. Once the
    sender has moved to the Wi-Fi, the tab must say so."""
    panel = make_panel()
    assert panel._warning.isHidden()
    panel.sender.is_running = True
    panel.sender.choice = AdapterChoice(
        WIFI, WIFI.ipv4, automatic=True,
        warning="Automatic is sending on Wi-Fi (Wi-Fi).",
    )
    panel._refresh_status()
    assert "Wi-Fi" in panel._warning.text() and not panel._warning.isHidden()
    assert panel._network.itemText(0) == "Automatic: Wi-Fi (192.168.1.20)"


def test_choosing_a_network_keeps_its_key_and_its_name(make_panel) -> None:
    config = SacnConfig()
    panel = make_panel(config, adapters=(WIFI, WIRED), route="192.168.1.20")
    panel._network.setCurrentIndex(panel._network.findData("{wired}"))
    assert (config.adapter, config.adapter_name) == ("{wired}", "Ethernet 2")


def test_a_chosen_network_that_is_missing_is_shown_missing_not_as_automatic(make_panel) -> None:
    config = SacnConfig(adapter="{usb}", adapter_name="USB Ethernet")
    panel = make_panel(config)
    assert panel._network.currentText() == "USB Ethernet (not on this computer now)"
    assert config.adapter == "{usb}", "loading the tab must not change the choice"
    assert "USB Ethernet" in panel._warning.text()
