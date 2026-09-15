"""Wer - Windows Eos Recorder.

A live camera feed with lighting-console data composited on top, recorded to
a video file you can actually use later.

Copyright (C) 2026 Hudco Lighting LLC. Free software under the GNU General
Public License v3 or later: see LICENSE beside this repository's README, and
wer.licensing for the notice the application itself shows.

Package layout, and the import rules that keep it honest:

    wer.core         no Qt. The DataBus and show-file model.
    wer.connections  no Qt. Protocol code: OSC from the console, sACN out.
    wer.video        no Qt. Capture, compositing buffers, the encoder pipe.
    wer.overlay      Qt. Widgets that draw onto frames via QPainter.
    wer.ui           Qt. Application chrome.

`core`, `connections` and `video` must stay importable in a process with no Qt
loaded, so protocol parsers can be tested without a QApplication. Nothing in
those three packages may import from `overlay` or `ui`.

Connections and widgets never import each other. They meet at the DataBus.
"""

#: 1.0.0 is the first release. It has been installed and used to record on
#: Windows 10 and Windows 11, with and without a hardware encoder. The last
#: number goes up by one with every installer, so no two installers share a
#: version.
__version__ = "1.0.0"

APP_NAME = "Wer"
APP_DISPLAY_NAME = "Windows Eos Recorder"
APP_ORG = "Wer"

#: Extension for show files. JSON, human-readable, diffable.
SHOW_FILE_EXT = ".wer"

#: Bumped whenever the on-disk show-file shape changes. The migration hook
#: exists from day one so we are not retrofitting it under pressure.
#:
#: 2 -- consoles became a named list of profiles rather than one set of
#:     settings, so a laptop can move between venues.
#: 3 -- the A/V offset is kept for each camera and audio input
#:     (recording.av_offsets), not one value for everything.
SHOW_SCHEMA_VERSION = 3
