"""Markers and the sidecar files written alongside a recording.

No Qt.

A recording you have to scrub through is only half useful. The point of this
module is that a four-hour tech opens in an editor with every cue already
labelled, so finding the moment the followspot was late takes seconds rather
than an afternoon.

Three files are written next to the video:

``<name>.markers.csv``    timestamp, type, cue, label, note -- for reading, and
                         for anything that eats CSV.
``<name>.chapters.txt``   ffmetadata chapters, which import into Resolve,
                         Premiere and mpv as navigable chapter marks.
``<name>.bus.jsonl``      every DataBus change with a record-relative
                         timestamp -- see :mod:`wer.core.buslog`.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["MarkerKind", "Marker", "MarkerLog", "format_timecode"]


class MarkerKind(str, Enum):
    #: Dropped automatically when the console fires a cue.
    CUE = "cue"
    #: Dropped by the operator on a hotkey.
    MANUAL = "manual"
    #: The start and end of the recording itself. Useful in the CSV as anchors;
    #: deliberately excluded from chapters, where they would be noise.
    START = "start"
    STOP = "stop"


def format_timecode(seconds: float, *, frames_per_second: float | None = None) -> str:
    """``HH:MM:SS.mmm``, or ``HH:MM:SS:FF`` when a frame rate is given.

    The millisecond form is what a person reads; the frame form is what an
    editor wants typed into a timecode field.
    """
    seconds = max(0.0, seconds)
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if frames_per_second and frames_per_second > 0:
        frames = int((seconds - int(seconds)) * frames_per_second)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"
    milliseconds = int(round((seconds - int(seconds)) * 1000))
    if milliseconds == 1000:  # rounding can push it over
        milliseconds = 999
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


@dataclass(frozen=True, slots=True)
class Marker:
    """One point of interest in a recording."""

    #: Seconds from the start of the recording.
    timestamp: float
    kind: MarkerKind = MarkerKind.MANUAL
    #: e.g. "1/58". Empty for manual markers.
    cue: str = ""
    label: str = ""
    note: str = ""
    #: Wall-clock time, so a marker can be tied back to a stage-management
    #: report or a note someone made on paper.
    wall_clock: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        # MarkerKind subclasses str; markers may be rebuilt from a CSV or a
        # show file, and `is MarkerKind.CUE` would be False for "cue".
        object.__setattr__(self, "kind", MarkerKind(self.kind))

    @property
    def title(self) -> str:
        """One line, for a chapter name or a list row."""
        if self.kind is MarkerKind.CUE:
            parts = [f"Cue {self.cue}" if self.cue else "Cue"]
            if self.label:
                parts.append(self.label)
            if self.note:
                parts.append(f"({self.note})")
            # A plain hyphen, not an em dash. These titles pass through
            # ffmetadata, Matroska and whatever editor opens the file next;
            # ASCII is the one thing all of them agree on.
            return " - ".join(parts)
        if self.note:
            return self.note
        return {
            MarkerKind.START: "Recording start",
            MarkerKind.STOP: "Recording end",
        }.get(self.kind, "Marker")

    @property
    def clock_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.wall_clock))


class MarkerLog:
    """Collects markers during a take and writes the sidecars afterwards.

    Thread-safe: cue markers arrive on the Eos connection thread while manual
    ones arrive on the UI thread.
    """

    def __init__(self) -> None:
        self._markers: list[Marker] = []
        self._lock = threading.RLock()

    def add(self, marker: Marker) -> Marker:
        with self._lock:
            self._markers.append(marker)
        log.info("Marker at %s: %s", format_timecode(marker.timestamp), marker.title)
        return marker

    def add_cue(self, timestamp: float, cue: str, label: str, note: str = "") -> Marker:
        return self.add(
            Marker(timestamp=timestamp, kind=MarkerKind.CUE, cue=cue,
                   label=label, note=note)
        )

    def add_manual(self, timestamp: float, note: str = "") -> Marker:
        return self.add(
            Marker(timestamp=timestamp, kind=MarkerKind.MANUAL, note=note)
        )

    def clear(self) -> None:
        with self._lock:
            self._markers.clear()

    @property
    def markers(self) -> list[Marker]:
        """Every marker, in time order."""
        with self._lock:
            return sorted(self._markers, key=lambda m: m.timestamp)

    def __len__(self) -> int:
        with self._lock:
            return len(self._markers)

    # ------------------------------------------------------------------ files

    def write_csv(self, path: Path, *, frames_per_second: float | None = None) -> Path:
        """Write ``<name>.markers.csv``.

        ``newline=""`` is not optional: without it the csv module's own
        line endings combine with Windows text-mode translation and every row
        is separated by a blank line.
        """
        markers = self.markers
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["timecode", "seconds", "type", "cue", "label", "note", "wall_clock"]
            )
            for marker in markers:
                writer.writerow([
                    format_timecode(marker.timestamp, frames_per_second=frames_per_second),
                    f"{marker.timestamp:.3f}",
                    marker.kind.value,
                    marker.cue,
                    marker.label,
                    marker.note,
                    marker.clock_text,
                ])
        log.info("Wrote %d markers to %s", len(markers), path.name)
        return path

    def write_chapters(self, path: Path, total_duration: float) -> Path:
        """Write ffmetadata chapters, importable by Resolve, Premiere and mpv.

        The format is fussy in ways that are easy to get wrong and silently
        produce a file with no chapters at all:

        - Chapters must not overlap, so each one runs until the next begins.
        - A chapter with START == END is discarded, so zero-length chapters
          (two cues fired in the same millisecond, which happens on a GO
          during a fade) are given a minimum length.
        - Titles are escaped: ``=``, ``;``, ``#``, ``\\`` and newlines all mean
          something to the parser.
        - Start and stop markers are excluded. They would appear as chapters
          called "Recording start", which is not information.
        """
        candidates = [
            m for m in self.markers
            if m.kind in (MarkerKind.CUE, MarkerKind.MANUAL)
            and 0.0 <= m.timestamp <= max(total_duration, 0.0)
        ]

        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [";FFMETADATA1"]

        # A chapter covering the run-up to the first cue, so the file does not
        # begin in the middle of nothing.
        if not candidates or candidates[0].timestamp > 1.0:
            first_end = candidates[0].timestamp if candidates else total_duration
            lines += _chapter_block(0.0, first_end, "Start")

        for index, marker in enumerate(candidates):
            start = marker.timestamp
            end = (
                candidates[index + 1].timestamp
                if index + 1 < len(candidates)
                else total_duration
            )
            lines += _chapter_block(start, end, marker.title)

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        log.info("Wrote %d chapters to %s", len(candidates), path.name)
        return path


#: Minimum chapter length in milliseconds. Two cues fired within the same
#: millisecond would otherwise produce a zero-length chapter, which ffmpeg
#: discards silently.
MIN_CHAPTER_MS = 1


def _chapter_block(start: float, end: float, title: str) -> list[str]:
    start_ms = max(0, int(round(start * 1000)))
    end_ms = max(start_ms + MIN_CHAPTER_MS, int(round(end * 1000)))
    return [
        "",
        "[CHAPTER]",
        "TIMEBASE=1/1000",
        f"START={start_ms}",
        f"END={end_ms}",
        f"title={_escape_metadata(title)}",
    ]


def _escape_metadata(text: str) -> str:
    """Escape the characters ffmetadata treats as syntax."""
    for character in ("\\", "=", ";", "#"):
        text = text.replace(character, "\\" + character)
    return text.replace("\n", " ").replace("\r", " ")
