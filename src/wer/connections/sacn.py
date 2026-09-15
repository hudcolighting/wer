"""sACN (ANSI E1.31) packets, for sending Wer's recording status.

No Qt and no socket: building and reading packets only, so every byte can be
tested without a network. The sender is wer.connections.sacn_sender.

Written without a capture of our own, so the sources are named here. The
current standard is ANSI E1.31-2025 (2016 and 2018 before it); none of those
PDFs could be read as text on 13 Sep 2026, so every field below was checked
against three implementations instead:

- ETC's sACN library, github.com/ETCLabs/sACN: src/sacn/private/pdu.h (the
  offsets), src/sacn/pdu.c, src/sacn/source_state.c (sequence numbers,
  termination, keep-alives), src/sacn/private/common.h, include/sacn/source.h
  (defaults).
- libe131, github.com/hhromic/libe131: src/e131.h and src/e131.c.
- python sacn, github.com/Hundemeier/sacn: sacn/messages/root_layer.py,
  data_packet.py and universe_discovery.py.

Per-address priority (START code 0xDD) is an ETC extension, described at
etclabs.github.io/sACNDocs/2.0.1/per_address_priority.html.

A data packet with all 512 slots is 638 bytes::

    0     preamble size 0x0010, post-amble size 0x0000
    4     ACN packet identifier "ASC-E1.17" and three zeros
    16    root flags+length, vector 4, CID (16 bytes)
    38    framing flags+length, vector 2, source name (64 bytes), priority,
          sync address, sequence number, options, universe
    115   DMP flags+length, vector 0x02, type 0xA1, first address 0,
          increment 1, value count (1 + slots)
    125   START code (0x00 levels, 0xDD per-address priorities)
    126   slots 1-512

Each flags+length is 0x7000 plus the bytes from that field to the end of the
packet.
"""

from __future__ import annotations

import math
import struct
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "ACN_PACKET_IDENTIFIER",
    "DATA_PACKET_SIZE",
    "DEFAULT_PRIORITY",
    "DISCOVERY_INTERVAL",
    "DISCOVERY_UNIVERSE",
    "KEEPALIVE_INTERVAL",
    "MULTICAST_TTL",
    "OPTION_PREVIEW",
    "OPTION_STREAM_TERMINATED",
    "PRIORITY_KEEPALIVE_INTERVAL",
    "PRIORITY_MAX",
    "PULSE_PERIOD",
    "REPEATS_AFTER_CHANGE",
    "SACN_PORT",
    "SLOT_COUNT",
    "START_CODE_LEVELS",
    "START_CODE_PER_ADDRESS_PRIORITY",
    "TERMINATION_PACKETS",
    "UNIVERSE_MAX",
    "UNIVERSE_MIN",
    "SacnDataPacket",
    "SacnDecodeError",
    "SacnDiscoveryPacket",
    "cid_bytes",
    "decode_packet",
    "encode_data_packet",
    "encode_discovery_packet",
    "encode_source_name",
    "multicast_group",
    "new_cid",
    "pulse_level",
]

#: The UDP port every sACN packet goes to (ETC kSacnPort).
SACN_PORT = 5568

#: How many routers a packet may cross. Windows' default for multicast is 1,
#: which the first router drops: a booth VLAN routed to the lighting network
#: would get nothing while the sender read "sending". ETC's sACN library sets
#: 64 (SACN_SOURCE_MULTICAST_TTL, include/sacn/opts.h); python sacn uses 8.
MULTICAST_TTL = 64

ACN_PACKET_IDENTIFIER = b"ASC-E1.17\x00\x00\x00"
_PREAMBLE_SIZE = 0x0010
_POSTAMBLE_SIZE = 0x0000

_VECTOR_ROOT_DATA = 0x00000004
_VECTOR_ROOT_EXTENDED = 0x00000008
_VECTOR_FRAMING_DATA = 0x00000002
_VECTOR_FRAMING_DISCOVERY = 0x00000002
_VECTOR_DISCOVERY_UNIVERSE_LIST = 0x00000001
_VECTOR_DMP_SET_PROPERTY = 0x02
_DMP_ADDRESS_AND_DATA_TYPE = 0xA1

START_CODE_LEVELS = 0x00
#: ETC's per-address priority: the same packet, with a priority in each slot.
#: 0 means "do not use this source for this address"; 1-200 are priorities.
START_CODE_PER_ADDRESS_PRIORITY = 0xDD

OPTION_PREVIEW = 0x80
#: Receivers go straight to data loss on a packet with this set.
OPTION_STREAM_TERMINATED = 0x40

UNIVERSE_MIN = 1
UNIVERSE_MAX = 63999
#: Where universe discovery packets go (239.255.250.214).
DISCOVERY_UNIVERSE = 64214

PRIORITY_MAX = 200
DEFAULT_PRIORITY = 100

SLOT_COUNT = 512
_SOURCE_NAME_SIZE = 64
_DATA_HEADER_SIZE = 126
DATA_PACKET_SIZE = _DATA_HEADER_SIZE + SLOT_COUNT
_DISCOVERY_HEADER_SIZE = 120
_DISCOVERY_UNIVERSES_PER_PAGE = 512

#: A source whose data has stopped changing sends a keep-alive every
#: 800-1000 ms. After a change it first sends it in quick succession: ETC sends
#: four in all, the change and three more, before it starts suppressing
#: (NUM_PRE_SUPPRESSION_PACKETS).
KEEPALIVE_INTERVAL = 0.85
#: ETC's default keep-alive for per-address priority packets.
PRIORITY_KEEPALIVE_INTERVAL = 1.0
REPEATS_AFTER_CHANGE = 3
#: ETC kSacnUniverseDiscoveryInterval.
DISCOVERY_INTERVAL = 10.0
#: ETC sends three packets with the Stream_Terminated option when a stream ends.
TERMINATION_PACKETS = 3

#: One fade from 0 to 255 and back, while Wer is recording. Hudson's choice,
#: 13 Sep 2026.
PULSE_PERIOD = 2.0

_ROOT = struct.Struct(">HH12sHI16s")                # 38 bytes, offset 0
_FRAMING = struct.Struct(">HI64sBHBBH")             # 77 bytes, offset 38
_DMP = struct.Struct(">HBBHHHB")                    # 11 bytes, offset 115
_DISCOVERY_FRAMING = struct.Struct(">HI64s4s")      # 74 bytes, offset 38
_DISCOVERY_LAYER = struct.Struct(">HIBB")           # 8 bytes, offset 112


class SacnDecodeError(ValueError):
    """Bytes that are not a well-formed sACN packet, with what was wrong."""


@dataclass(frozen=True, slots=True)
class SacnDataPacket:
    cid: bytes
    source_name: str
    priority: int
    sync_address: int
    sequence: int
    options: int
    universe: int
    start_code: int
    slots: bytes

    @property
    def stream_terminated(self) -> bool:
        return bool(self.options & OPTION_STREAM_TERMINATED)

    @property
    def preview(self) -> bool:
        return bool(self.options & OPTION_PREVIEW)

    def level(self, address: int) -> int:
        """The value in a slot, numbered from 1 as a console numbers them."""
        return self.slots[address - 1]


@dataclass(frozen=True, slots=True)
class SacnDiscoveryPacket:
    cid: bytes
    source_name: str
    page: int
    last_page: int
    universes: tuple[int, ...]


# ------------------------------------------------------------------ encoding


def _flags_and_length(length: int) -> int:
    if not 0 < length < 0x1000:
        raise ValueError(f"a PDU of {length} bytes does not fit its length field")
    return 0x7000 | length


def _check_universe(universe: int) -> None:
    if not UNIVERSE_MIN <= universe <= UNIVERSE_MAX:
        raise ValueError(f"universe {universe} is outside {UNIVERSE_MIN}-{UNIVERSE_MAX}")


def _check_cid(cid: bytes) -> None:
    if len(cid) != 16:
        raise ValueError(f"a CID is 16 bytes, not {len(cid)}")


def multicast_group(universe: int) -> str:
    """The multicast address a universe is sent to: 239.255.<high>.<low>."""
    if universe != DISCOVERY_UNIVERSE:
        _check_universe(universe)
    return f"239.255.{(universe >> 8) & 0xFF}.{universe & 0xFF}"


def encode_source_name(name: str) -> bytes:
    """UTF-8 in a 64-byte field that must end in a zero: 63 bytes of text at
    most, cut before a character rather than through one."""
    raw = name.encode("utf-8")[: _SOURCE_NAME_SIZE - 1]
    while raw:
        try:
            raw.decode("utf-8")
            break
        except UnicodeDecodeError:
            raw = raw[:-1]
    return raw.ljust(_SOURCE_NAME_SIZE, b"\x00")


def encode_data_packet(
    *,
    cid: bytes,
    source_name: str,
    priority: int,
    sequence: int,
    universe: int,
    slots: bytes,
    start_code: int = START_CODE_LEVELS,
    options: int = 0,
    sync_address: int = 0,
) -> bytes:
    """One E1.31 data packet. ``slots`` holds slot 1 onwards, 1-512 of them."""
    _check_cid(cid)
    _check_universe(universe)
    if not 0 <= priority <= PRIORITY_MAX:
        raise ValueError(f"priority {priority} is outside 0-{PRIORITY_MAX}")
    if not 1 <= len(slots) <= SLOT_COUNT:
        raise ValueError(f"{len(slots)} slots; a packet carries 1-{SLOT_COUNT}")
    total = _DATA_HEADER_SIZE + len(slots)
    return b"".join((
        _ROOT.pack(
            _PREAMBLE_SIZE, _POSTAMBLE_SIZE, ACN_PACKET_IDENTIFIER,
            _flags_and_length(total - 16), _VECTOR_ROOT_DATA, cid,
        ),
        _FRAMING.pack(
            _flags_and_length(total - 38), _VECTOR_FRAMING_DATA,
            encode_source_name(source_name), priority, sync_address,
            sequence & 0xFF, options & 0xFF, universe,
        ),
        _DMP.pack(
            _flags_and_length(total - 115), _VECTOR_DMP_SET_PROPERTY,
            _DMP_ADDRESS_AND_DATA_TYPE, 0x0000, 0x0001, 1 + len(slots), start_code,
        ),
        bytes(slots),
    ))


def encode_discovery_packet(
    *,
    cid: bytes,
    source_name: str,
    universes: Sequence[int],
    page: int = 0,
    last_page: int = 0,
) -> bytes:
    """One page of E1.31 universe discovery: the universes this source sends."""
    _check_cid(cid)
    listed = sorted(set(universes))
    if len(listed) > _DISCOVERY_UNIVERSES_PER_PAGE:
        raise ValueError(f"{len(listed)} universes do not fit one discovery page")
    for universe in listed:
        _check_universe(universe)
    total = _DISCOVERY_HEADER_SIZE + 2 * len(listed)
    return b"".join((
        _ROOT.pack(
            _PREAMBLE_SIZE, _POSTAMBLE_SIZE, ACN_PACKET_IDENTIFIER,
            _flags_and_length(total - 16), _VECTOR_ROOT_EXTENDED, cid,
        ),
        _DISCOVERY_FRAMING.pack(
            _flags_and_length(total - 38), _VECTOR_FRAMING_DISCOVERY,
            encode_source_name(source_name), b"\x00" * 4,
        ),
        _DISCOVERY_LAYER.pack(
            _flags_and_length(total - 112), _VECTOR_DISCOVERY_UNIVERSE_LIST,
            page, last_page,
        ),
        struct.pack(f">{len(listed)}H", *listed),
    ))


# ------------------------------------------------------------------ decoding


def _check_length(flags_and_length: int, expected: int, layer: str) -> None:
    if flags_and_length >> 12 != 0x7:
        raise SacnDecodeError(f"{layer} layer flags are {flags_and_length >> 12:#x}, not 0x7")
    if flags_and_length & 0x0FFF != expected:
        raise SacnDecodeError(
            f"{layer} layer says {flags_and_length & 0x0FFF} bytes, "
            f"but {expected} follow it"
        )


def _source_name(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")


def decode_packet(packet: bytes) -> SacnDataPacket | SacnDiscoveryPacket:
    """Read a data or universe discovery packet, checking every fixed field."""
    if len(packet) < _ROOT.size:
        raise SacnDecodeError(f"{len(packet)} bytes is too short for an ACN root layer")
    preamble, postamble, identifier, root_length, vector, cid = _ROOT.unpack_from(packet, 0)
    if (preamble, postamble, identifier) != (
        _PREAMBLE_SIZE, _POSTAMBLE_SIZE, ACN_PACKET_IDENTIFIER
    ):
        raise SacnDecodeError("not an ACN packet: the preamble or identifier is wrong")
    _check_length(root_length, len(packet) - 16, "root")
    if vector == _VECTOR_ROOT_DATA:
        return _decode_data(packet, cid)
    if vector == _VECTOR_ROOT_EXTENDED:
        return _decode_discovery(packet, cid)
    raise SacnDecodeError(f"root vector {vector:#x} is neither data nor discovery")


def _decode_data(packet: bytes, cid: bytes) -> SacnDataPacket:
    if len(packet) < _DATA_HEADER_SIZE + 1:
        raise SacnDecodeError(f"{len(packet)} bytes is too short for a data packet")
    (framing_length, vector, name, priority, sync_address, sequence, options,
     universe) = _FRAMING.unpack_from(packet, 38)
    _check_length(framing_length, len(packet) - 38, "framing")
    if vector != _VECTOR_FRAMING_DATA:
        raise SacnDecodeError(f"framing vector {vector:#x} is not a data packet's")
    (dmp_length, dmp_vector, address_type, first, increment, count,
     start_code) = _DMP.unpack_from(packet, 115)
    _check_length(dmp_length, len(packet) - 115, "DMP")
    if (dmp_vector, address_type, first, increment) != (
        _VECTOR_DMP_SET_PROPERTY, _DMP_ADDRESS_AND_DATA_TYPE, 0x0000, 0x0001
    ):
        raise SacnDecodeError("the DMP layer's fixed fields are wrong")
    if count != len(packet) - 125:
        raise SacnDecodeError(
            f"the DMP layer counts {count} values, but {len(packet) - 125} follow"
        )
    return SacnDataPacket(
        cid=cid, source_name=_source_name(name), priority=priority,
        sync_address=sync_address, sequence=sequence, options=options,
        universe=universe, start_code=start_code, slots=bytes(packet[126:]),
    )


def _decode_discovery(packet: bytes, cid: bytes) -> SacnDiscoveryPacket:
    if len(packet) < _DISCOVERY_HEADER_SIZE or (len(packet) - _DISCOVERY_HEADER_SIZE) % 2:
        raise SacnDecodeError(f"{len(packet)} bytes is not a discovery packet's size")
    framing_length, vector, name, _reserved = _DISCOVERY_FRAMING.unpack_from(packet, 38)
    _check_length(framing_length, len(packet) - 38, "framing")
    if vector != _VECTOR_FRAMING_DISCOVERY:
        raise SacnDecodeError(f"framing vector {vector:#x} is not universe discovery")
    layer_length, layer_vector, page, last_page = _DISCOVERY_LAYER.unpack_from(packet, 112)
    _check_length(layer_length, len(packet) - 112, "discovery")
    if layer_vector != _VECTOR_DISCOVERY_UNIVERSE_LIST:
        raise SacnDecodeError(f"discovery vector {layer_vector:#x} is not a universe list")
    count = (len(packet) - _DISCOVERY_HEADER_SIZE) // 2
    universes = struct.unpack_from(f">{count}H", packet, _DISCOVERY_HEADER_SIZE)
    return SacnDiscoveryPacket(
        cid=cid, source_name=_source_name(name), page=page, last_page=last_page,
        universes=tuple(universes),
    )


# ---------------------------------------------------------------- the rest


def pulse_level(seconds: float, period: float = PULSE_PERIOD) -> int:
    """The level ``seconds`` into a recording: a smooth fade, 0 to 255 and back.

    A raised cosine, so it leaves 0 and arrives at 255 gently rather than
    bouncing off either end, and starts from 0 when the take does.
    """
    if seconds <= 0:
        return 0
    phase = (seconds % period) / period
    return int(round(255 * (1 - math.cos(2 * math.pi * phase)) / 2))


def new_cid() -> str:
    """A new source identity, made once and kept. E1.31 asks equipment to keep
    one CID for its life, and receivers tell sources apart by CID alone."""
    return str(uuid.uuid4())


def cid_bytes(text: str) -> bytes:
    """The 16 bytes of a CID stored as text. ValueError if it is not a UUID."""
    return uuid.UUID(text).bytes
