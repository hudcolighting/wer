"""Markers and sidecar files. No Qt."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from wer.core.markers import (
    Marker,
    MarkerKind,
    MarkerLog,
    format_timecode,
)


@pytest.fixture()
def log() -> MarkerLog:
    log = MarkerLog()
    log.add(Marker(timestamp=0.0, kind=MarkerKind.START))
    log.add_cue(12.0, "1/58", "Adriana Xs Center")
    log.add_cue(48.5, "1/62", "Enter Dromio")
    log.add_manual(70.25, "followspot late")
    log.add_cue(95.0, "1/65", "Antiph Quote")
    log.add(Marker(timestamp=120.0, kind=MarkerKind.STOP))
    return log


# ------------------------------------------------------------------- timecode


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "00:00:00.000"), (61.5, "00:01:01.500"), (3661.25, "01:01:01.250")],
)
def test_timecode_milliseconds(seconds: float, expected: str) -> None:
    assert format_timecode(seconds) == expected


def test_timecode_frames() -> None:
    assert format_timecode(61.5, frames_per_second=30) == "00:01:01:15"


def test_timecode_never_shows_1000_milliseconds() -> None:
    """Rounding can push .9999 over the second boundary."""
    assert not format_timecode(1.9999).endswith("1000")


def test_negative_time_clamps_rather_than_showing_a_minus() -> None:
    assert format_timecode(-5) == "00:00:00.000"


# --------------------------------------------------------------------- titles


def test_cue_marker_title() -> None:
    marker = Marker(0, MarkerKind.CUE, cue="1/58", label="Adriana Xs Center")
    assert marker.title == "Cue 1/58 - Adriana Xs Center"


def test_titles_stay_ascii() -> None:
    """These pass through ffmetadata, Matroska and whatever opens the file."""
    marker = Marker(0, MarkerKind.CUE, cue="1/58", label="Label")
    assert marker.title.isascii()


def test_manual_marker_title_is_its_note() -> None:
    assert Marker(0, MarkerKind.MANUAL, note="spot late").title == "spot late"


# ------------------------------------------------------------------------ csv


def test_csv_has_a_row_per_marker(tmp_path: Path, log: MarkerLog) -> None:
    path = log.write_csv(tmp_path / "m.csv", frames_per_second=30)
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(log)
    assert rows[0]["type"] == "start"
    assert rows[1]["cue"] == "1/58"
    assert rows[1]["label"] == "Adriana Xs Center"
    assert rows[3]["note"] == "followspot late"


def test_csv_has_no_blank_lines_between_rows(tmp_path: Path, log: MarkerLog) -> None:
    """Windows text mode plus the csv module's own line endings double up."""
    path = log.write_csv(tmp_path / "m.csv")
    text = path.read_text(encoding="utf-8")
    assert "\n\n" not in text


def test_csv_survives_a_comma_in_a_note(tmp_path: Path) -> None:
    log = MarkerLog()
    log.add_manual(1.0, "spot late, again")
    path = log.write_csv(tmp_path / "m.csv")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["note"] == "spot late, again"


# ------------------------------------------------------------------- chapters


def test_chapters_are_written_in_ffmetadata_form(tmp_path: Path, log: MarkerLog) -> None:
    path = log.write_chapters(tmp_path / "c.txt", 120.0)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(";FFMETADATA1")
    assert "[CHAPTER]" in text
    assert "TIMEBASE=1/1000" in text


def test_chapters_exclude_start_and_stop_markers(tmp_path: Path, log: MarkerLog) -> None:
    """"Recording start" as a chapter title is not information."""
    text = log.write_chapters(tmp_path / "c.txt", 120.0).read_text(encoding="utf-8")
    assert "Recording start" not in text
    assert "Recording end" not in text


def test_chapters_do_not_overlap(tmp_path: Path, log: MarkerLog) -> None:
    """Overlapping chapters make the whole set be discarded."""
    text = log.write_chapters(tmp_path / "c.txt", 120.0).read_text(encoding="utf-8")
    starts = [int(l.split("=")[1]) for l in text.splitlines() if l.startswith("START=")]
    ends = [int(l.split("=")[1]) for l in text.splitlines() if l.startswith("END=")]
    assert starts == sorted(starts)
    for start, end in zip(starts, ends):
        assert end > start, "a zero-length chapter is silently discarded"
    for end, next_start in zip(ends, starts[1:]):
        assert end <= next_start, "chapters overlap"


def test_two_cues_in_the_same_millisecond_still_produce_valid_chapters(
    tmp_path: Path,
) -> None:
    """Happens for real: a GO during a fade fires two events together."""
    log = MarkerLog()
    log.add_cue(10.0000, "1/58", "One")
    log.add_cue(10.0001, "1/59", "Two")
    text = log.write_chapters(tmp_path / "c.txt", 30.0).read_text(encoding="utf-8")
    starts = [int(l.split("=")[1]) for l in text.splitlines() if l.startswith("START=")]
    ends = [int(l.split("=")[1]) for l in text.splitlines() if l.startswith("END=")]
    for start, end in zip(starts, ends):
        assert end > start


def test_chapter_titles_are_escaped(tmp_path: Path) -> None:
    """=, ;, # and backslash all mean something to the ffmetadata parser."""
    log = MarkerLog()
    log.add_manual(1.0, r"levels = 50; see #3 \ note")
    text = log.write_chapters(tmp_path / "c.txt", 10.0).read_text(encoding="utf-8")
    title = next(l for l in text.splitlines() if l.startswith("title="))
    assert title.count("=") > 1  # the title's own = is escaped, not a new field
    assert r"\=" in title and r"\;" in title and r"\#" in title


def test_a_newline_in_a_note_cannot_break_the_file(tmp_path: Path) -> None:
    log = MarkerLog()
    log.add_manual(1.0, "line one\nline two")
    text = log.write_chapters(tmp_path / "c.txt", 10.0).read_text(encoding="utf-8")
    titles = [l for l in text.splitlines() if l.startswith("title=")]
    assert len(titles) == 1
    assert "line one line two" in titles[0]


def test_markers_beyond_the_recording_are_dropped(tmp_path: Path) -> None:
    """A marker past the end would create a chapter outside the media."""
    log = MarkerLog()
    log.add_cue(5.0, "1/1", "Inside")
    log.add_cue(500.0, "1/2", "Outside")
    text = log.write_chapters(tmp_path / "c.txt", 10.0).read_text(encoding="utf-8")
    assert "Inside" in text
    assert "Outside" not in text


def test_no_markers_still_produces_a_valid_file(tmp_path: Path) -> None:
    path = MarkerLog().write_chapters(tmp_path / "c.txt", 60.0)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(";FFMETADATA1")


# ------------------------------------------------------------------ threading


def test_marker_log_is_thread_safe() -> None:
    """Cue markers arrive on the Eos thread while manual ones arrive on the UI."""
    import threading

    log = MarkerLog()

    def add(offset: int) -> None:
        for i in range(100):
            log.add_cue(offset + i * 0.01, f"1/{i}", "Label")

    threads = [threading.Thread(target=add, args=(t * 10,)) for t in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(log) == 600
    timestamps = [m.timestamp for m in log.markers]
    assert timestamps == sorted(timestamps)
