"""sACN packets, byte by byte.

The offsets and fixed values are the ones in ETC's sACN library
(src/sacn/private/pdu.h), cross-checked against libe131 and python sacn; see
wer.connections.sacn. No capture of Wer's own packets by independent software
has been made yet, so these pin the layout those three agree on.

No Qt, no sockets.
"""

from __future__ import annotations

import struct

import pytest

from wer.connections.sacn import (
    DATA_PACKET_SIZE,
    DISCOVERY_UNIVERSE,
    OPTION_STREAM_TERMINATED,
    START_CODE_PER_ADDRESS_PRIORITY,
    SacnDataPacket,
    SacnDecodeError,
    SacnDiscoveryPacket,
    cid_bytes,
    decode_packet,
    encode_data_packet,
    encode_discovery_packet,
    encode_source_name,
    multicast_group,
    new_cid,
    pulse_level,
)

CID = bytes(range(16))


def levels(**overrides) -> bytes:
    slots = bytearray(512)
    slots[41] = 200
    fields = dict(
        cid=CID, source_name="Wer on booth", priority=150, sequence=7,
        universe=0x0102, slots=bytes(slots),
    )
    fields.update(overrides)
    return encode_data_packet(**fields)


def u16(packet: bytes, offset: int) -> int:
    return struct.unpack_from(">H", packet, offset)[0]


def u32(packet: bytes, offset: int) -> int:
    return struct.unpack_from(">I", packet, offset)[0]


# ------------------------------------------------------------- data packets


def test_a_full_packet_is_638_bytes_with_every_field_where_etc_puts_it() -> None:
    packet = levels()
    assert len(packet) == DATA_PACKET_SIZE == 638

    assert u16(packet, 0) == 0x0010, "preamble size"
    assert u16(packet, 2) == 0x0000, "post-amble size"
    assert packet[4:16] == b"ASC-E1.17\x00\x00\x00"
    assert u32(packet, 18) == 0x00000004, "root vector"
    assert packet[22:38] == CID

    assert u32(packet, 40) == 0x00000002, "framing vector"
    assert packet[44:108] == b"Wer on booth".ljust(64, b"\x00")
    assert packet[108] == 150, "priority"
    assert u16(packet, 109) == 0, "sync address"
    assert packet[111] == 7, "sequence"
    assert packet[112] == 0, "options"
    assert u16(packet, 113) == 0x0102, "universe"

    assert packet[117] == 0x02, "DMP vector"
    assert packet[118] == 0xA1, "address and data type"
    assert u16(packet, 119) == 0x0000, "first property address"
    assert u16(packet, 121) == 0x0001, "address increment"
    assert u16(packet, 123) == 513, "property value count"
    assert packet[125] == 0x00, "START code"
    assert packet[126 + 41] == 200 and sum(packet[126:]) == 200


def test_each_length_counts_from_its_own_field_to_the_end() -> None:
    packet = levels()
    assert u16(packet, 16) == 0x726E  # 0x7000 | 622
    assert u16(packet, 38) == 0x7258  # 0x7000 | 600
    assert u16(packet, 115) == 0x720B  # 0x7000 | 523


def test_a_short_packet_keeps_its_three_lengths_and_count_in_step() -> None:
    packet = levels(slots=bytes(10))
    assert len(packet) == 136
    assert u16(packet, 16) & 0x0FFF == 136 - 16
    assert u16(packet, 38) & 0x0FFF == 136 - 38
    assert u16(packet, 115) & 0x0FFF == 136 - 115
    assert u16(packet, 123) == 11


def test_per_address_priority_is_the_same_packet_with_start_code_dd() -> None:
    packet = levels(start_code=START_CODE_PER_ADDRESS_PRIORITY)
    assert packet[125] == 0xDD
    assert len(packet) == 638


def test_stream_terminated_is_bit_6_of_the_options() -> None:
    assert levels(options=OPTION_STREAM_TERMINATED)[112] == 0x40


def test_the_sequence_wraps_at_255() -> None:
    assert levels(sequence=256)[111] == 0


@pytest.mark.parametrize("bad", [
    dict(universe=0), dict(universe=64000), dict(priority=201), dict(priority=-1),
    dict(slots=b""), dict(slots=bytes(513)), dict(cid=b"short"),
])
def test_values_a_receiver_cannot_take_are_refused(bad) -> None:
    with pytest.raises(ValueError):
        levels(**bad)


def test_the_source_name_ends_in_a_zero_and_is_cut_before_a_character() -> None:
    assert encode_source_name("x" * 100) == b"x" * 63 + b"\x00"
    # "é" is two bytes: 62 bytes of "a" leave one byte, which cannot hold it.
    name = encode_source_name("a" * 62 + "é")
    assert name == b"a" * 62 + b"\x00\x00"
    assert len(encode_source_name("")) == 64


# ------------------------------------------------------------- multicast


@pytest.mark.parametrize(("universe", "group"), [
    (1, "239.255.0.1"), (42, "239.255.0.42"), (256, "239.255.1.0"),
    (63999, "239.255.249.255"), (DISCOVERY_UNIVERSE, "239.255.250.214"),
])
def test_a_universe_is_sent_to_239_255_high_low(universe: int, group: str) -> None:
    assert multicast_group(universe) == group


@pytest.mark.parametrize("universe", [0, 64000])
def test_there_is_no_group_for_a_universe_that_does_not_exist(universe: int) -> None:
    with pytest.raises(ValueError):
        multicast_group(universe)


# ------------------------------------------------------------ discovery


def test_universe_discovery_is_laid_out_as_etc_sends_it() -> None:
    packet = encode_discovery_packet(cid=CID, source_name="Wer", universes=[7, 3, 7])
    assert len(packet) == 120 + 4
    assert u16(packet, 16) & 0x0FFF == len(packet) - 16
    assert u32(packet, 18) == 0x00000008, "root vector: extended"
    assert u16(packet, 38) & 0x0FFF == len(packet) - 38
    assert u32(packet, 40) == 0x00000002, "framing vector: discovery"
    assert packet[108:112] == b"\x00" * 4, "reserved"
    assert u16(packet, 112) & 0x0FFF == len(packet) - 112
    assert u32(packet, 114) == 0x00000001, "universe list"
    assert packet[118:120] == b"\x00\x00", "page 0 of 0"
    assert struct.unpack_from(">2H", packet, 120) == (3, 7), "sorted, once each"


# ------------------------------------------------------------- decoding


def test_a_data_packet_reads_back_as_it_was_written() -> None:
    decoded = decode_packet(levels(options=OPTION_STREAM_TERMINATED))
    assert isinstance(decoded, SacnDataPacket)
    assert (decoded.cid, decoded.source_name, decoded.priority, decoded.sequence,
            decoded.universe, decoded.start_code) == (CID, "Wer on booth", 150, 7, 0x0102, 0)
    assert decoded.level(42) == 200
    assert decoded.stream_terminated and not decoded.preview


def test_a_discovery_packet_reads_back() -> None:
    decoded = decode_packet(
        encode_discovery_packet(cid=CID, source_name="Wer", universes=[9, 2])
    )
    assert isinstance(decoded, SacnDiscoveryPacket)
    assert decoded.universes == (2, 9)
    assert decoded.source_name == "Wer"


@pytest.mark.parametrize("damage", [
    lambda p: p[:100],
    lambda p: p[:4] + b"ASC-E1.18\x00\x00\x00" + p[16:],
    lambda p: p[:16] + struct.pack(">H", 0x7000 | 621) + p[18:],
    lambda p: p[:40] + struct.pack(">I", 3) + p[44:],
    lambda p: p[:118] + b"\xA2" + p[119:],
    lambda p: p + b"\x00",
])
def test_a_malformed_packet_is_refused_with_a_reason(damage) -> None:
    with pytest.raises(SacnDecodeError):
        decode_packet(damage(levels()))


# ------------------------------------------------------------------ pulse


def test_the_pulse_starts_at_zero_peaks_at_one_second_and_comes_back() -> None:
    assert pulse_level(0.0) == 0
    assert pulse_level(1.0) == 255
    assert pulse_level(2.0) == 0
    assert pulse_level(0.5) in (127, 128)
    assert pulse_level(3.0) == 255, "it repeats every 2 s"


def test_the_pulse_rises_smoothly_then_falls() -> None:
    rising = [pulse_level(t / 100) for t in range(0, 101)]
    falling = [pulse_level(1 + t / 100) for t in range(0, 101)]
    assert rising == sorted(rising)
    assert falling == sorted(falling, reverse=True)
    steps = [b - a for a, b in zip(rising, rising[1:])]
    assert max(steps) <= 5, "no jumps at 10 ms steps"


def test_before_a_take_the_pulse_is_zero() -> None:
    assert pulse_level(-1.0) == 0


# -------------------------------------------------------------------- CID


def test_a_cid_is_kept_as_text_and_sent_as_16_bytes() -> None:
    text = new_cid()
    assert len(cid_bytes(text)) == 16
    assert new_cid() != text
    with pytest.raises(ValueError):
        cid_bytes("not a uuid")
