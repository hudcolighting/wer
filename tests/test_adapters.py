"""Choosing the network sACN leaves by.

The choice is tested against made-up adapter lists. The real list is only
checked for its shape: it reads Windows' adapter table and sends nothing.
"""

from __future__ import annotations

import sys

import pytest

from wer.connections.adapters import NetworkAdapter, choose_adapter, list_adapters

WIRED = NetworkAdapter("{wired}", "Ethernet 2", "10.101.90.150", True, "ethernet")
WIFI = NetworkAdapter("{wifi}", "Wi-Fi", "192.168.1.20", True, "wifi")
UNPLUGGED = NetworkAdapter("{usb}", "USB Ethernet", None, False, "ethernet")
LOOPBACK = NetworkAdapter("{lo}", "Loopback Pseudo-Interface 1", "127.0.0.1", True, "loopback")


def test_automatic_on_the_only_wired_network_says_nothing_more() -> None:
    choice = choose_adapter([WIRED, UNPLUGGED, LOOPBACK], "", route_ip="10.101.90.150")
    assert (choice.adapter, choice.ip, choice.problem, choice.warning) == (
        WIRED, "10.101.90.150", "", ""
    )
    assert choice.automatic
    assert choice.label == "Ethernet 2 (10.101.90.150)"


def test_automatic_landing_on_wifi_says_so_by_name() -> None:
    choice = choose_adapter([WIFI, UNPLUGGED], "", route_ip="192.168.1.20")
    assert not choice.problem
    assert "Wi-Fi" in choice.warning


def test_automatic_with_two_networks_connected_names_both() -> None:
    choice = choose_adapter([WIFI, WIRED, LOOPBACK], "", route_ip="192.168.1.20")
    assert "Ethernet 2" in choice.warning and "More than one network" in choice.warning
    assert "Loopback" not in choice.warning


def test_automatic_with_no_route_cannot_send() -> None:
    choice = choose_adapter([UNPLUGGED], "", route_ip=None)
    assert choice.problem and choice.ip is None


@pytest.mark.parametrize("route", ["127.0.0.1", "127.0.0.5"])
def test_automatic_routed_to_loopback_is_no_network(route: str) -> None:
    """With nothing else connected, Windows routes multicast to loopback. That
    reaches nothing off this computer, and taking it for a network turned a
    dropped cable into a green "sending"."""
    choice = choose_adapter([LOOPBACK, UNPLUGGED], "", route_ip=route)
    assert choice.problem.startswith("No network is connected")
    assert choice.ip is None


def test_a_chosen_network_is_used_whatever_windows_prefers() -> None:
    choice = choose_adapter([WIFI, WIRED], "{wired}", route_ip="192.168.1.20")
    assert (choice.adapter, choice.ip, choice.warning) == (WIRED, "10.101.90.150", "")


def test_a_chosen_network_that_is_gone_is_named_not_swapped_for_another() -> None:
    choice = choose_adapter([WIFI], "{wired}", route_ip="192.168.1.20", saved_name="Ethernet 2")
    assert choice.ip is None
    assert "Ethernet 2" in choice.problem


def test_a_chosen_network_that_is_unplugged_cannot_send() -> None:
    choice = choose_adapter([UNPLUGGED, WIFI], "{usb}", route_ip="192.168.1.20")
    assert choice.ip is None
    assert "USB Ethernet is not connected" in choice.problem


def test_an_adapter_that_is_up_without_an_address_cannot_send() -> None:
    waiting = NetworkAdapter("{w}", "Ethernet 3", None, True, "ethernet")
    assert "no IPv4 address" in choose_adapter([waiting], "{w}", route_ip=None).problem


@pytest.mark.skipif(sys.platform != "win32", reason="reads Windows' adapter table")
def test_windows_lists_its_adapters_by_name() -> None:
    adapters = list_adapters()
    assert adapters, "Windows listed no adapters at all, not even loopback"
    for adapter in adapters:
        assert adapter.key and adapter.name
        assert adapter.kind in {"ethernet", "wifi", "loopback", "tunnel", "other"}
        if adapter.ipv4 is not None:
            assert len(adapter.ipv4.split(".")) == 4
    assert any(a.kind == "loopback" and a.ipv4 == "127.0.0.1" for a in adapters)
