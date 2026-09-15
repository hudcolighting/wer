"""Raw OSC capture tool.

Connects to an Eos console (or listens for UDP) and records everything it sends,
in both raw and decoded form. This exists because no Eos parser gets written
against a guessed spec: capture first, parse against the captures, keep them as
fixtures.

No Qt. Runs standalone from a checkout.

Examples
--------
Capture over TCP with OSC 1.0 length-prefix framing (Eos port 3032)::

    python tools/osc_capture.py --host 10.101.90.101 --tcp 3032 --seconds 60

Same console, SLIP framing (port 3033), to confirm which framing each port uses::

    python tools/osc_capture.py --host 10.101.90.101 --tcp 3033 --framing slip

Listen for UDP output instead (console must be told to transmit to us)::

    python tools/osc_capture.py --udp 8000

Output
------
Three files per run, under ``captures/`` by default:

``<name>.raw``      every byte received, exactly as received, length-delimited
                    per read so the stream can be replayed
``<name>.jsonl``    one decoded message per line with a monotonic timestamp
``<name>.txt``      human-readable log, the thing to skim
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, TextIO

# Allow running straight from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wer.connections.osc import (  # noqa: E402
    LengthPrefixFramer,
    OscBundle,
    OscDecodeError,
    OscMessage,
    SlipFramer,
    decode_packet,
    encode_message,
)


def _json_safe(value: Any) -> Any:
    """Make an OSC argument representable in JSON without losing information."""
    if isinstance(value, bytes):
        return {"__blob__": value.hex()}
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return {"__float__": repr(value)}
    return value


class Recorder:
    """Writes the three output files and keeps a running tally."""

    def __init__(self, base: Path) -> None:
        base.parent.mkdir(parents=True, exist_ok=True)
        self.base = base
        self.raw: BinaryIO = open(base.with_suffix(".raw"), "wb")
        self.jsonl: TextIO = open(base.with_suffix(".jsonl"), "w", encoding="utf-8")
        self.text: TextIO = open(base.with_suffix(".txt"), "w", encoding="utf-8")
        self.started = time.perf_counter()
        self.address_counts: Counter[str] = Counter()
        self.typetag_examples: dict[str, str] = {}
        self.packets = 0
        self.decode_errors = 0

    # Raw stream format: 8-byte monotonic-ms timestamp, 4-byte length, payload.
    # Enough to replay the stream later with original timing.
    def write_raw(self, chunk: bytes) -> None:
        elapsed_ms = int((time.perf_counter() - self.started) * 1000)
        self.raw.write(struct.pack(">Qi", elapsed_ms, len(chunk)) + chunk)
        self.raw.flush()

    def record(self, message: OscMessage) -> None:
        elapsed = time.perf_counter() - self.started
        self.packets += 1
        self.address_counts[message.address] += 1
        # Keep one worked example of each address, with its type tags. This is
        # the single most useful thing for writing the parser afterwards.
        if message.address not in self.typetag_examples:
            self.typetag_examples[message.address] = str(message)

        self.jsonl.write(
            json.dumps(
                {
                    "t": round(elapsed, 4),
                    "address": message.address,
                    "typetags": message.typetags,
                    "args": [_json_safe(a) for a in message.args],
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        self.jsonl.flush()

        line = f"[{elapsed:8.3f}] {message}"
        self.text.write(line + "\n")
        self.text.flush()
        print(line, flush=True)

    def error(self, detail: str, raw: bytes) -> None:
        self.decode_errors += 1
        line = f"[decode error] {detail}\n              bytes: {raw[:64]!r}"
        self.text.write(line + "\n")
        self.text.flush()
        print(line, file=sys.stderr, flush=True)

    def close(self) -> None:
        summary = self.summary()
        self.text.write("\n" + summary + "\n")
        for handle in (self.raw, self.jsonl, self.text):
            handle.close()
        print("\n" + summary)
        print(f"\nWrote:\n  {self.base.with_suffix('.raw')}"
              f"\n  {self.base.with_suffix('.jsonl')}"
              f"\n  {self.base.with_suffix('.txt')}")

    def summary(self) -> str:
        lines = [
            "=" * 72,
            f"{self.packets} messages, {self.decode_errors} decode errors, "
            f"{len(self.address_counts)} distinct addresses",
            "=" * 72,
        ]
        if self.address_counts:
            lines.append("")
            lines.append("Addresses seen (count, then one worked example):")
            width = max(len(a) for a in self.address_counts)
            for address, count in self.address_counts.most_common():
                lines.append(f"  {count:6d}  {address:<{width}}")
            lines.append("")
            lines.append("One example of each:")
            for address in sorted(self.typetag_examples):
                lines.append(f"  {self.typetag_examples[address]}")
        return "\n".join(lines)


def _dispatch(packet: bytes, recorder: Recorder) -> None:
    try:
        decoded = decode_packet(packet)
    except OscDecodeError as exc:
        recorder.error(str(exc), packet)
        return
    if isinstance(decoded, OscBundle):
        for message in decoded.messages():
            recorder.record(message)
    else:
        recorder.record(decoded)


def capture_tcp(
    host: str,
    port: int,
    framing: str,
    seconds: float,
    recorder: Recorder,
    user: int | None,
    subscribe: bool,
) -> None:
    framer = SlipFramer() if framing == "slip" else LengthPrefixFramer()
    print(f"Connecting to {host}:{port} ({framing} framing)...")
    with socket.create_connection((host, port), timeout=10) as sock:
        print(f"Connected from {sock.getsockname()}. Capturing for {seconds:g}s.")
        print("Run some cues on the console now.\n")

        # Two optional handshakes. Both are documented Eos behaviour rather
        # than measured behaviour, so they are opt-in and their effect is
        # visible in the capture itself.
        def send(raw: bytes) -> None:
            sock.sendall(
                SlipFramer.frame(raw) if framing == "slip"
                else LengthPrefixFramer.frame(raw)
            )

        if user is not None:
            print(f"-> /eos/user {user}")
            send(encode_message("/eos/user", user))
        if subscribe:
            print("-> /eos/subscribe 1")
            send(encode_message("/eos/subscribe", 1))

        sock.settimeout(1.0)
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                print("\nConsole closed the connection.", file=sys.stderr)
                break
            recorder.write_raw(chunk)
            try:
                for packet in framer.feed(chunk):
                    _dispatch(packet, recorder)
            except OscDecodeError as exc:
                recorder.error(f"framing: {exc}", chunk)
                break


def capture_udp(port: int, seconds: float, recorder: Recorder, bind: str) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind, port))
        sock.settimeout(1.0)
        print(f"Listening on UDP {bind}:{port} for {seconds:g}s.")
        print("The console must be configured to transmit OSC to this machine.")
        print("Run some cues on the console now.\n")
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            try:
                datagram, _peer = sock.recvfrom(65536)
            except socket.timeout:
                continue
            recorder.write_raw(datagram)
            _dispatch(datagram, recorder)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture raw OSC from an Eos console.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", help="console IP (TCP only)")
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--tcp", type=int, metavar="PORT",
                           help="capture over TCP (Eos: usually 3032; the port does "
                                "not decide the framing, see --framing)")
    transport.add_argument("--udp", type=int, metavar="PORT",
                           help="listen for UDP datagrams (Eos default: 8000)")
    parser.add_argument("--framing", choices=("length", "slip"),
                        help="TCP framing. Defaults by port: 3033 -> slip, else length")
    parser.add_argument("--seconds", type=float, default=60.0,
                        help="how long to capture (default: 60)")
    parser.add_argument("--bind", default="0.0.0.0", help="local address for UDP")
    parser.add_argument("--out", type=Path, default=Path("captures"),
                        help="output directory (default: captures/)")
    parser.add_argument("--name", help="base filename (default: timestamped)")
    parser.add_argument("--user", type=int,
                        help="send /eos/user <n> on connect (TCP). 0 means any user")
    parser.add_argument("--subscribe", action="store_true",
                        help="send /eos/subscribe 1 on connect (TCP)")
    args = parser.parse_args(argv)

    if args.tcp is None and args.udp is None:
        parser.error("choose a transport: --tcp PORT or --udp PORT")

    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    transport_label = f"tcp{args.tcp}" if args.tcp else f"udp{args.udp}"
    base = args.out / (args.name or f"eos-{transport_label}-{stamp}")

    recorder = Recorder(base)
    try:
        if args.tcp is not None:
            framing = args.framing or ("slip" if args.tcp == 3033 else "length")
            capture_tcp(args.host, args.tcp, framing, args.seconds,
                        recorder, args.user, args.subscribe)
        else:
            capture_udp(args.udp, args.seconds, recorder, args.bind)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    except OSError as exc:
        print(f"\nConnection failed: {exc}", file=sys.stderr)
        recorder.close()
        return 1
    recorder.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
