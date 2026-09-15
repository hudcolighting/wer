"""Believable bus values and a stand-in picture, for previewing a layout.

A layout is built before the console is connected and before the camera is
running -- at a desk in the afternoon, not in the booth at the half. Rendered
against an empty bus every widget says "--" and the cue block is one short
line, so a preview says nothing about how wide the command line really gets or
whether the cue stack will run into the clock. This fills a bus with the kind
of values a show in progress produces, so a preview or thumbnail comes out the
right size and shape.

Every key is one a connection really publishes (wer.connections.eos_parser and
wer.connections.builtin), with a value of the type it publishes. The values
themselves are made up, and are published under their own source id, so one
can always be told from a real value by where it came from.

No Qt: numpy and the DataBus only, so anything that wants a preview can have
one without starting a QApplication.
"""

from __future__ import annotations

import numpy as np

from wer.core.databus import DataBus

__all__ = ["SAMPLE_SOURCE", "sample_bus", "sample_frame"]

#: The source every sample value is published under, so a preview value can
#: always be told from a real one. The time-in-cue widget relies on it: a
#: sample bus never fires a cue, so it shows a stand-in time for these.
SAMPLE_SOURCE = "sample"
_SOURCE = SAMPLE_SOURCE

#: A tech of The Quiet Neighbours, part way through a fade in Act Two.
_VALUES: dict[str, object] = {
    # The console. The cue text is in the desk's own "list/number label time
    # percent" shape.
    "eos.connected": True,
    "eos.show.name": "Quiet Neighbours LX",
    "eos.cue.active.list": "1",
    "eos.cue.active.number": "58",
    "eos.cue.active.label": "Beatrix Xs Center",
    "eos.cue.active.text": "1/58 Beatrix Xs Center 3.0 62%",
    "eos.cue.active.fading": True,
    "eos.cue.active.progress": 0.62,
    "eos.cue.active.duration": 3.0,
    "eos.cue.active.remaining": 1.1,
    "eos.cue.pending.number": "62",
    "eos.cue.pending.label": "Enter Jasper",
    "eos.cue.previous.number": "54",
    "eos.cue.previous.label": "Beatrix Xs SL",
    "eos.cmdline.text": "LIVE: Chan 1 Thru 12 At 75 Enter",
    # No capture holds a selection -- the fixtures record only the empty
    # string sent when nothing is selected -- so this is a stand-in
    # of plausible length, not the desk's own format.
    "eos.chan.active": "1 Thru 12",
    # The clock, formatted as SystemConnection formats it.
    "clock.wall": "21:57:31",
    "clock.wall12": "9:57:31 PM",
    "clock.date": "2026-09-08",
    "clock.date_long": "Tuesday 08 September 2026",
    "clock.elapsed": "0:12:04",
    "clock.elapsed_seconds": 724.0,
    "clock.recording": True,
    "clock.take": 3,
    # The Manual tab.
    "manual.show": "The Quiet Neighbours",
    "manual.act": "Two",
    "manual.scene": "One",
    "manual.note": "Followspot 2 late on the Jasper entrance",
}


def sample_bus() -> DataBus:
    """A new bus holding a show in progress, for previews and thumbnails.

    A fresh bus on every call, never the live one: a preview must have no way
    to put a made-up cue number in front of a recording. Nothing is published
    with an expiry, so a preview left open does not decay into "--" while the
    layout is being worked on.
    """
    bus = DataBus()
    for key, value in _VALUES.items():
        bus.publish(key, value, source_connection_id=_SOURCE)
    return bus


def sample_frame(width: int, height: int) -> np.ndarray:
    """A dark stage with one warm pool of light, as a BGR uint8 frame.

    Text over flat black flatters every style, and legibility over a bright
    stage is the whole game. So the stand-in has both: a dim wash, darkest at
    the top of frame where the cue block and clock usually sit, and a soft warm
    special centre stage that spreads down towards the lower third. A preview
    on it shows whether a style still reads with something bright behind it.

    Deterministic -- no noise -- so two previews of one layout are the same
    picture and a test can compare them.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"a frame needs a positive size, not {width}x{height}")

    y = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    x = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]

    # A barely lit cyc: near black at the top, a little lighter at the floor.
    wash = 10.0 + 34.0 * y

    # The special: a Gaussian pool, round in pixels rather than in fractions of
    # the frame, so it is not squashed into an oval on a wide picture. A round
    # Gaussian is one curve across times one curve down, so the exponential is
    # taken along two edges instead of at every pixel.
    spread = -2.0 * 0.22**2
    across = np.exp(((x - 0.55) * (width / height)) ** 2 / spread)
    down = np.exp((y - 0.66) ** 2 / spread)
    pool = down * across

    frame = np.empty((height, width, 3), dtype=np.float32)
    # Warm: red strongest and blue weakest, clipping to near-white at the centre.
    frame[..., 0] = wash * 1.10 + pool * 150.0
    frame[..., 1] = wash * 0.95 + pool * 205.0
    frame[..., 2] = wash * 0.85 + pool * 245.0
    np.clip(frame, 0.0, 255.0, out=frame)
    return frame.astype(np.uint8)
