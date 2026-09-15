"""This computer's network adapters, for choosing which one sACN leaves by.

No Qt. Windows only in practice: GetAdaptersAddresses through ctypes, as
wer.core.power and wer.core.instance already call Windows, so there is no new
dependency. Anywhere else the list is empty, never an exception.

Why a choice at all: a laptop with Wi-Fi and a show network both connected
sends multicast out of whichever card Windows prefers, which is often the
Wi-Fi, and nothing says so. "Automatic" is that preference, found without
sending anything, and always shown by name.

Layout of the structures from Microsoft's documentation
(learn.microsoft.com/en-us/windows/win32/api/iptypes/ns-iptypes-ip_adapter_addresses_lh
and ns-iptypes-ip_adapter_unicast_address_lh), and checked on the development
laptop on 13 Sep 2026 with this project's Python 3.12 x64: FriendlyName at
offset 72, OperStatus at 104.

Measured there too: an adapter that is down still lists its IPv4 address,
marked Tentative. Only an adapter that is up, with a Preferred address, can
send.
"""

from __future__ import annotations

import ctypes
import logging
import socket
import sys
from collections.abc import Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

__all__ = [
    "AdapterChoice",
    "NetworkAdapter",
    "choose_adapter",
    "list_adapters",
    "read_adapters",
    "route_source_ip",
]

_IF_TYPE_KIND = {6: "ethernet", 71: "wifi", 24: "loopback", 131: "tunnel"}
_OPER_STATUS_UP = 1
_DAD_STATE_PREFERRED = 4
_AF_INET = 2
_GAA_FLAG_SKIP_ANYCAST = 0x2
_GAA_FLAG_SKIP_MULTICAST = 0x4
_GAA_FLAG_SKIP_DNS_SERVER = 0x8
_ERROR_BUFFER_OVERFLOW = 111
_ERROR_NO_DATA = 232


@dataclass(frozen=True, slots=True)
class NetworkAdapter:
    #: Windows' AdapterName, a GUID. Kept in settings: the friendly name can
    #: be renamed and the interface index changes when an adapter is disabled
    #: and enabled again.
    key: str
    #: What Windows calls it: "Ethernet 2", "Wi-Fi".
    name: str
    #: The usable IPv4 address, or None when it has none that can send.
    ipv4: str | None
    up: bool
    #: "ethernet", "wifi", "loopback", "tunnel" or "other".
    kind: str
    description: str = ""

    @property
    def usable(self) -> bool:
        return self.up and self.ipv4 is not None

    @property
    def label(self) -> str:
        if self.usable:
            return f"{self.name} ({self.ipv4})"
        return f"{self.name} (not connected)"


@dataclass(frozen=True, slots=True)
class AdapterChoice:
    """Where sACN would go, or why it cannot."""

    adapter: NetworkAdapter | None
    ip: str | None
    #: Why nothing can be sent. Empty when it can.
    problem: str = ""
    #: Something worth saying about a choice that works: Automatic landing on
    #: Wi-Fi, or more than one network connected.
    warning: str = ""
    automatic: bool = False

    @property
    def label(self) -> str:
        if self.adapter is not None and self.ip:
            return f"{self.adapter.name} ({self.ip})"
        if self.adapter is not None:
            return self.adapter.name
        return self.ip or "no network"


def choose_adapter(
    adapters: Sequence[NetworkAdapter],
    key: str,
    *,
    route_ip: str | None,
    saved_name: str = "",
) -> AdapterChoice:
    """Resolve a saved choice ("" for Automatic) against the adapters there now.

    ``route_ip`` is the address Windows would send multicast from
    (route_source_ip); only Automatic uses it. ``saved_name`` is what the
    chosen adapter was called, so a missing one can be named.
    """
    if not key:
        loopback = route_ip is not None and (
            route_ip.startswith("127.")
            or any(a.ipv4 == route_ip and a.kind == "loopback" for a in adapters)
        )
        if route_ip is None or loopback:
            # With nothing else connected, Windows routes multicast to its
            # loopback interface (development laptop, 13 Sep 2026), which
            # reaches nothing off this computer. Taking that for a network let
            # a dropped cable read as "sending" in green.
            return AdapterChoice(
                None, None, automatic=True,
                problem="No network is connected, so there is nowhere to send sACN.",
            )
        adapter = next((a for a in adapters if a.ipv4 == route_ip), None)
        connected = [a for a in adapters if a.usable and a.kind != "loopback"]
        picked = adapter.name if adapter is not None else route_ip
        warnings = []
        if adapter is not None and adapter.kind == "wifi":
            warnings.append(
                f"Automatic is sending on Wi-Fi ({adapter.name}). If the lighting "
                "network is on a cable, choose it instead."
            )
        if len(connected) > 1:
            warnings.append(
                f"More than one network is connected "
                f"({', '.join(a.name for a in connected)}), and Automatic picked "
                f"{picked}. Choose the lighting network to be sure."
            )
        return AdapterChoice(
            adapter, route_ip, warning=" ".join(warnings), automatic=True
        )

    adapter = next((a for a in adapters if a.key == key), None)
    if adapter is None:
        return AdapterChoice(
            None, None,
            problem=(
                f"The network chosen for sACN{f' ({saved_name})' if saved_name else ''} "
                "is not on this computer now. Plug it in, or choose another."
            ),
        )
    if not adapter.up:
        return AdapterChoice(adapter, None, problem=f"{adapter.name} is not connected.")
    if adapter.ipv4 is None:
        return AdapterChoice(
            adapter, None, problem=f"{adapter.name} has no IPv4 address yet."
        )
    return AdapterChoice(adapter, adapter.ipv4)


def route_source_ip(group: str = "239.255.0.1", port: int = 5568) -> str | None:
    """The address Windows would send multicast from, or None with no route.

    A UDP connect() sends nothing; it only makes the route decision, which
    getsockname() then reports.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect((group, port))
            ip = probe.getsockname()[0]
        except OSError:
            return None
    return None if ip in ("", "0.0.0.0") else ip


# ------------------------------------------------------------------ Windows


class _SocketAddress(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class _UnicastAddress(ctypes.Structure):
    pass


_UnicastAddress._fields_ = [
    ("Length", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_UnicastAddress)),
    ("Address", _SocketAddress),
    ("PrefixOrigin", ctypes.c_int),
    ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
]


class _AdapterAddresses(ctypes.Structure):
    pass


# Only as far as OperStatus: the list is walked through Next and never
# allocated by size, so the fields after it need not be declared.
_AdapterAddresses._fields_ = [
    ("Length", ctypes.c_ulong),
    ("IfIndex", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_AdapterAddresses)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_UnicastAddress)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Mtu", ctypes.c_ulong),
    ("IfType", ctypes.c_ulong),
    ("OperStatus", ctypes.c_int),
]


def list_adapters() -> list[NetworkAdapter]:
    """Every IPv4 adapter Windows knows, in its route-preference order.

    Slow by Microsoft's own account, so never call it on the interface thread.
    Empty, with a log line, if Windows cannot be asked. read_adapters raises
    instead, for a caller that must not take a failed look for no adapters.
    """
    try:
        return read_adapters()
    except Exception:  # noqa: BLE001 - a missing list must not stop anything
        log.exception("Could not list network adapters")
        return []


def read_adapters() -> list[NetworkAdapter]:
    """list_adapters, raising OSError when Windows cannot be asked."""
    if sys.platform != "win32":
        return []
    iphlpapi = ctypes.WinDLL("iphlpapi")
    call = iphlpapi.GetAdaptersAddresses
    call.argtypes = [
        ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.POINTER(_AdapterAddresses), ctypes.POINTER(ctypes.c_ulong),
    ]
    call.restype = ctypes.c_ulong
    flags = _GAA_FLAG_SKIP_ANYCAST | _GAA_FLAG_SKIP_MULTICAST | _GAA_FLAG_SKIP_DNS_SERVER

    size = ctypes.c_ulong(15000)
    for _attempt in range(3):
        buffer = ctypes.create_string_buffer(size.value)
        result = call(
            _AF_INET, flags, None,
            ctypes.cast(buffer, ctypes.POINTER(_AdapterAddresses)), ctypes.byref(size),
        )
        if result != _ERROR_BUFFER_OVERFLOW:
            break
    if result == _ERROR_NO_DATA:
        return []
    if result != 0:
        raise OSError(result, f"GetAdaptersAddresses failed with error {result}")

    adapters: list[NetworkAdapter] = []
    node = ctypes.cast(buffer, ctypes.POINTER(_AdapterAddresses))
    while node:
        entry = node.contents
        adapters.append(NetworkAdapter(
            key=(entry.AdapterName or b"").decode("ascii", "replace"),
            name=entry.FriendlyName or "",
            ipv4=_preferred_ipv4(entry.FirstUnicastAddress),
            up=entry.OperStatus == _OPER_STATUS_UP,
            kind=_IF_TYPE_KIND.get(entry.IfType, "other"),
            description=entry.Description or "",
        ))
        node = entry.Next
    return adapters


def _preferred_ipv4(first) -> str | None:
    unicast = first
    while unicast:
        address = unicast.contents
        sockaddr = address.Address.lpSockaddr
        if sockaddr and address.DadState == _DAD_STATE_PREFERRED:
            family = ctypes.c_ushort.from_address(sockaddr).value
            if family == _AF_INET:
                octets = (ctypes.c_ubyte * 4).from_address(sockaddr + 4)
                return ".".join(str(octet) for octet in octets)
        unicast = address.Next
    return None
