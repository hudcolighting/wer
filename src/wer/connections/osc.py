"""OSC 1.0 decoding and the two TCP stream framings Eos uses.

Pure stdlib, no Qt, no third-party protocol library. Everything here is
testable in a process with no QApplication, which is the point.

Scope note: this module decodes. It does not interpret. Turning
``/eos/out/active/cue/1/58`` into a cue object is the Eos connection's job, and
that parser is not written until there are real captures to write it against.

References
----------
OSC 1.0 spec: 4-byte-aligned, big-endian, null-terminated strings padded to a
multiple of 4. SLIP framing: RFC 1055.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Iterator

__all__ = [
    "OscMessage",
    "OscBundle",
    "OscDecodeError",
    "decode_packet",
    "encode_message",
    "SlipFramer",
    "LengthPrefixFramer",
    "BUNDLE_MARKER",
]

BUNDLE_MARKER = b"#bundle\x00"


class OscDecodeError(ValueError):
    """Raised when bytes are not valid OSC.

    Carries enough context to be worth logging: a bare "invalid" tells you
    nothing when you are staring at a console that will not talk.
    """


@dataclass(frozen=True, slots=True)
class OscMessage:
    address: str
    args: tuple[Any, ...] = ()
    #: The raw type-tag string without its leading comma, e.g. "sif".
    #: Kept because argument *types* are part of what we need to learn from
    #: real captures, and they are lost once values become Python objects.
    typetags: str = ""

    def __str__(self) -> str:
        if not self.args:
            return self.address
        rendered = ", ".join(_render_arg(a) for a in self.args)
        return f"{self.address} [{self.typetags}] {rendered}"


@dataclass(frozen=True, slots=True)
class OscBundle:
    #: NTP timetag as sent. 1 means "immediately" by convention.
    timetag: int
    elements: tuple[OscMessage | OscBundle, ...] = field(default=())

    def messages(self) -> Iterator[OscMessage]:
        """Flatten nested bundles into a stream of messages."""
        for element in self.elements:
            if isinstance(element, OscBundle):
                yield from element.messages()
            else:
                yield element


def _render_arg(value: Any) -> str:
    if isinstance(value, bytes):
        return f"<blob {len(value)}B>"
    if isinstance(value, str):
        return repr(value)
    return str(value)


# --------------------------------------------------------------------- reading


def _padded_length(length: int) -> int:
    """OSC pads every element to a multiple of four bytes."""
    return (length + 3) & ~0x03


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\x00", offset)
    if end == -1:
        raise OscDecodeError(
            f"unterminated string at offset {offset} (of {len(data)} bytes)"
        )
    text = data[offset:end].decode("utf-8", errors="replace")
    return text, offset + _padded_length(end - offset + 1)


def _read_blob(data: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(data):
        raise OscDecodeError(f"truncated blob size at offset {offset}")
    (size,) = struct.unpack_from(">i", data, offset)
    offset += 4
    if size < 0 or offset + size > len(data):
        raise OscDecodeError(f"blob size {size} overruns packet at offset {offset}")
    return data[offset : offset + size], offset + _padded_length(size)


def _unpack(fmt: str, size: int, data: bytes, offset: int, tag: str) -> Any:
    """Read one fixed-width argument, bounds-checked.

    ``struct.unpack_from`` raises ``struct.error`` when the buffer is short, and
    ``struct.error`` is not a ``ValueError`` -- so it was not an
    ``OscDecodeError`` either, and it escaped the one place that catches those.
    A single packet truncated inside a numeric argument therefore took the whole
    console connection down: verified against a real length-prefixed TCP stream,
    where the three good cue packets sitting behind it in the same recv buffer
    went in the bin with it. Check the length here and raise the error the
    callers already handle.
    """
    if offset + size > len(data):
        raise OscDecodeError(
            f"truncated {tag!r} argument at offset {offset}: "
            f"{size} bytes needed, {len(data) - offset} left"
        )
    return struct.unpack_from(fmt, data, offset)[0]


def _decode_message(data: bytes) -> OscMessage:
    address, offset = _read_string(data, 0)

    # A message with no type-tag string is malformed under OSC 1.0, but some
    # senders omit it for zero-argument messages and accepting it costs nothing.
    if offset >= len(data):
        return OscMessage(address=address)

    typetags, offset = _read_string(data, offset)
    if not typetags.startswith(","):
        raise OscDecodeError(
            f"type tag string for {address!r} does not start with a comma: "
            f"{typetags!r}"
        )
    typetags = typetags[1:]

    args: list[Any] = []
    for tag in typetags:
        if tag == "i":
            value = _unpack(">i", 4, data, offset, tag)
            offset += 4
        elif tag == "f":
            value = _unpack(">f", 4, data, offset, tag)
            offset += 4
        elif tag in ("s", "S"):
            value, offset = _read_string(data, offset)
        elif tag == "b":
            value, offset = _read_blob(data, offset)
        elif tag == "h":
            value = _unpack(">q", 8, data, offset, tag)
            offset += 8
        elif tag == "d":
            value = _unpack(">d", 8, data, offset, tag)
            offset += 8
        elif tag == "t":
            value = _unpack(">Q", 8, data, offset, tag)
            offset += 8
        elif tag == "c":
            raw = _unpack(">I", 4, data, offset, tag)
            # chr() of anything above 0x10FFFF raises on its own account, which
            # is a bare ValueError and escapes the same way struct.error did.
            if raw > 0x10FFFF:
                raise OscDecodeError(
                    f"character argument {raw} is not a code point in {address!r}"
                )
            value = chr(raw)
            offset += 4
        elif tag in ("r", "m"):
            value = data[offset : offset + 4]
            offset += 4
        elif tag == "T":
            value = True
        elif tag == "F":
            value = False
        elif tag == "N":
            value = None
        elif tag == "I":
            value = float("inf")
        elif tag in ("[", "]"):
            # Array markers. Flattened rather than nested: no observed Eos
            # traffic uses them, and inventing a representation before seeing
            # one would be guessing.
            continue
        else:
            raise OscDecodeError(
                f"unsupported type tag {tag!r} in {address!r} (tags={typetags!r})"
            )
        args.append(value)

    return OscMessage(address=address, args=tuple(args), typetags=typetags)


def _decode_bundle(data: bytes) -> OscBundle:
    (timetag,) = struct.unpack_from(">Q", data, 8)
    offset = 16
    elements: list[OscMessage | OscBundle] = []
    while offset < len(data):
        if offset + 4 > len(data):
            raise OscDecodeError(f"truncated bundle element size at {offset}")
        (size,) = struct.unpack_from(">i", data, offset)
        offset += 4
        if size < 0 or offset + size > len(data):
            raise OscDecodeError(f"bundle element size {size} overruns packet")
        elements.append(decode_packet(data[offset : offset + size]))
        offset += size
    return OscBundle(timetag=timetag, elements=tuple(elements))


def decode_packet(data: bytes) -> OscMessage | OscBundle:
    """Decode one complete OSC packet (a message or a bundle)."""
    if not data:
        raise OscDecodeError("empty packet")
    if data.startswith(BUNDLE_MARKER):
        if len(data) < 16:
            raise OscDecodeError(f"bundle too short: {len(data)} bytes")
        return _decode_bundle(data)
    if not data.startswith(b"/"):
        raise OscDecodeError(
            "packet is neither a bundle nor an address pattern; starts with "
            f"{data[:8]!r}"
        )
    return _decode_message(data)


# --------------------------------------------------------------------- writing


def encode_message(address: str, *args: Any) -> bytes:
    """Encode a minimal OSC message.

    Present for handshake messages such as ``/eos/subscribe`` and for
    round-trip tests. Wer never drives the console; this is not a control path.
    """
    if not address.startswith("/"):
        raise ValueError(f"OSC address must start with a slash: {address!r}")

    def pad(raw: bytes) -> bytes:
        return raw + b"\x00" * (_padded_length(len(raw) + 1) - len(raw))

    out = bytearray(pad(address.encode("utf-8")))
    tags = ""
    body = bytearray()
    for value in args:
        if isinstance(value, bool):
            tags += "T" if value else "F"
        elif isinstance(value, int):
            tags += "i"
            body += struct.pack(">i", value)
        elif isinstance(value, float):
            tags += "f"
            body += struct.pack(">f", value)
        elif isinstance(value, str):
            tags += "s"
            body += pad(value.encode("utf-8"))
        elif isinstance(value, (bytes, bytearray)):
            tags += "b"
            body += struct.pack(">i", len(value)) + bytes(value)
            body += b"\x00" * (_padded_length(len(value)) - len(value))
        else:
            raise TypeError(
                f"cannot encode {type(value).__name__} as an OSC argument"
            )
    out += pad(("," + tags).encode("ascii"))
    out += body
    return bytes(out)


# --------------------------------------------------------------------- framing


class LengthPrefixFramer:
    """OSC 1.0 over TCP: each packet preceded by a big-endian int32 length.

    Eos exposes this on port 3032.
    """

    #: Refuse absurd lengths rather than trying to buffer them. Choosing the
    #: wrong framing shows up as nonsense in the first four bytes, and this
    #: turns that into a clear error instead of an apparent hang.
    MAX_PACKET = 1 << 20

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        """Add received bytes; return any complete packets they completed."""
        self._buffer += chunk
        packets: list[bytes] = []
        while len(self._buffer) >= 4:
            (size,) = struct.unpack_from(">i", self._buffer, 0)
            if size < 0 or size > self.MAX_PACKET:
                raise OscDecodeError(
                    f"implausible length prefix {size}; this looks like SLIP "
                    "framing, not length-prefix. Eos exposes SLIP on 3033 by "
                    "default, but it can be set on 3032 too -- verified on a "
                    "real desk, so do not trust the port to tell you"
                )
            if len(self._buffer) < 4 + size:
                break
            packets.append(bytes(self._buffer[4 : 4 + size]))
            del self._buffer[: 4 + size]
        return packets

    @staticmethod
    def frame(packet: bytes) -> bytes:
        return struct.pack(">i", len(packet)) + packet


class SlipFramer:
    """OSC 1.1 over TCP: SLIP framing, RFC 1055.

    Eos exposes this on port 3033.
    """

    END = 0xC0
    ESC = 0xDB
    ESC_END = 0xDC
    ESC_ESC = 0xDD

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer += chunk
        packets: list[bytes] = []
        while True:
            index = self._buffer.find(self.END)
            if index == -1:
                break
            raw = bytes(self._buffer[:index])
            del self._buffer[: index + 1]
            # Empty runs are normal: SLIP senders may emit a leading END, and
            # a double END is a legal way to mark a frame boundary.
            if raw:
                packets.append(self._unescape(raw))
        return packets

    @classmethod
    def _unescape(cls, raw: bytes) -> bytes:
        if cls.ESC not in raw:
            return raw
        out = bytearray()
        index = 0
        while index < len(raw):
            byte = raw[index]
            if byte == cls.ESC and index + 1 < len(raw):
                following = raw[index + 1]
                if following == cls.ESC_END:
                    out.append(cls.END)
                    index += 2
                    continue
                if following == cls.ESC_ESC:
                    out.append(cls.ESC)
                    index += 2
                    continue
            out.append(byte)
            index += 1
        return bytes(out)

    @classmethod
    def frame(cls, packet: bytes) -> bytes:
        out = bytearray([cls.END])
        for byte in packet:
            if byte == cls.END:
                out += bytes([cls.ESC, cls.ESC_END])
            elif byte == cls.ESC:
                out += bytes([cls.ESC, cls.ESC_ESC])
            else:
                out.append(byte)
        out.append(cls.END)
        return bytes(out)
