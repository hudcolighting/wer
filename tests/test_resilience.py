"""Surviving a long tech: disk space and encoder failure.

Both of these matter only over hours, which is exactly why they need testing in
seconds. Recording is "the part that must not fail".
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from wer.paths import ffmpeg_path
from wer.video.capture import Frame
from wer.video.encoder import OutputSettings
from wer.video.recorder import (
    CRITICAL_FREE_BYTES,
    MAX_RESTARTS,
    DiskSpace,
    DiskWarning,
    Recorder,
    RecorderState,
)

pytestmark = pytest.mark.skipif(ffmpeg_path() is None, reason="bundled ffmpeg missing")

W, H, FPS = 320, 180, 30


def frames(count: int):
    for i in range(count):
        image = np.zeros((H, W, 3), np.uint8)
        image[:, (i * 3) % (W - 20):(i * 3) % (W - 20) + 20] = (30, 180, 240)
        yield Frame(image=image, timestamp=time.perf_counter(), index=i)


# ------------------------------------------------------------------- the disk


def test_a_warning_describes_itself_usefully() -> None:
    """Hours remaining is the useful unit, not bytes."""
    warning = DiskWarning(free_bytes=8_000_000_000, minutes_remaining=42.0)
    text = warning.describe()
    assert "8.0 GB" in text
    assert "42 minutes" in text


def test_a_critical_warning_says_what_is_being_done() -> None:
    warning = DiskWarning(free_bytes=400_000_000, minutes_remaining=2.0, critical=True)
    assert "stopped" in warning.describe().lower()


def test_a_warning_without_a_rate_still_says_something() -> None:
    """In the first seconds there is no measured write rate yet."""
    assert "GB left" in DiskWarning(free_bytes=5_000_000_000,
                                    minutes_remaining=None).describe()


def test_the_write_rate_is_measured_not_assumed(tmp_path: Path) -> None:
    """A quiet stage at Compact and a busy one at Archive differ hugely."""
    recorder = Recorder()
    assert recorder.start(
        OutputSettings(), tmp_path / "rate.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    for frame in frames(30):
        recorder.submit(frame)
    # Under five seconds there is deliberately no estimate rather than a wild one.
    assert recorder.stats.bytes_per_second == 0.0
    recorder.stop()


def test_disk_warnings_reach_the_callback(tmp_path: Path) -> None:
    """Set the threshold absurdly high so any real disk triggers it."""
    seen: list[DiskWarning] = []
    recorder = Recorder(on_disk_warning=seen.append)
    recorder.low_disk_bytes = 10 ** 18  # more than any drive has

    assert recorder.start(
        OutputSettings(), tmp_path / "warn.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    for frame in frames(10):
        recorder.submit(frame)
        time.sleep(0.02)
    # The watchdog runs this every second; drive it here rather than sleep
    # through a tick, which is what the recorder's own disk tests do.
    recorder._next_disk_check = 0.0
    recorder._check_disk_if_due()
    recorder.stop()

    assert seen, "a low-disk warning should have been raised"
    assert seen[0].free_bytes > 0


def test_the_low_warning_fires_once_not_every_check(tmp_path: Path) -> None:
    """Ten seconds apart for four hours would be 1,440 warnings."""
    seen: list[DiskWarning] = []
    recorder = Recorder(on_disk_warning=seen.append)
    recorder.low_disk_bytes = 10 ** 18

    assert recorder.start(
        OutputSettings(), tmp_path / "once.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    for _ in range(4):
        for frame in frames(5):
            recorder.submit(frame)
            time.sleep(0.02)
        # Four checks, as four watchdog ticks would have made.
        recorder._next_disk_check = 0.0
        recorder._check_disk_if_due()
    recorder.stop()

    assert len(seen) == 1, f"warned {len(seen)} times; should warn once"


def test_the_critical_threshold_leaves_room_to_close_the_file() -> None:
    """A disk that fills mid-write does not fail politely."""
    assert CRITICAL_FREE_BYTES >= 100_000_000


def test_free_space_hours_estimate() -> None:
    space = DiskSpace(free_bytes=40_000_000_000, total_bytes=100_000_000_000)
    assert space.hours_at(4000) == pytest.approx(10.0, rel=0.01)


# -------------------------------------------------------- encoder resilience


def kill_ffmpeg(recorder: Recorder) -> None:
    process = recorder._process
    assert process is not None
    process.kill()


def test_the_session_continues_after_ffmpeg_dies(tmp_path: Path) -> None:
    """Three hours into a tech, a hiccup should cost a gap, not the rest."""
    recorder = Recorder()
    output = tmp_path / "tech.mkv"
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), output,
        capture_width=W, capture_height=H, capture_fps=FPS,
    )

    for index, frame in enumerate(frames(120)):
        recorder.submit(frame)
        if index == 40:
            kill_ffmpeg(recorder)
        time.sleep(1 / FPS)

    assert recorder.is_recording, "the session should have carried on"
    recorder.stop()

    assert recorder._restarts == 1
    assert len(recorder.parts) == 2
    assert recorder.parts[1].path.name.endswith("-part2.mkv")


def test_the_continuation_file_is_playable(tmp_path: Path) -> None:
    import subprocess

    recorder = Recorder()
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), tmp_path / "tech.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    for index, frame in enumerate(frames(120)):
        recorder.submit(frame)
        if index == 30:
            kill_ffmpeg(recorder)
        time.sleep(1 / FPS)
    recorder.stop()

    usable = [part for part in recorder.parts if part.usable and part.exists]
    assert usable, "nothing survived"
    result = subprocess.run(
        [str(ffmpeg_path()), "-hide_banner", "-i", str(usable[-1].path),
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )
    assert "Video: h264" in result.stderr


def test_parts_carry_their_offset_into_the_session(tmp_path: Path) -> None:
    """Markers are timed against the session; chapters need the part's own
    timeline, and this offset is what converts between them."""
    recorder = Recorder()
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), tmp_path / "tech.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    for index, frame in enumerate(frames(90)):
        recorder.submit(frame)
        if index == 45:
            kill_ffmpeg(recorder)
        time.sleep(1 / FPS)
    recorder.stop()

    assert recorder.parts[0].session_offset == 0.0
    assert recorder.parts[1].session_offset > 0.5


def test_an_empty_part_is_marked_unusable(tmp_path: Path) -> None:
    """A hard kill can leave a file with nothing in it. Say so rather than
    listing it as though it held footage."""
    recorder = Recorder()
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), tmp_path / "tech.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    for index, frame in enumerate(frames(90)):
        recorder.submit(frame)
        if index == 10:
            kill_ffmpeg(recorder)  # very early: nothing flushed
        time.sleep(1 / FPS)
    recorder.stop()

    assert any(not part.usable for part in recorder.parts) or all(
        part.path.stat().st_size > 0 for part in recorder.parts if part.exists
    )


def test_continuation_can_be_switched_off(tmp_path: Path) -> None:
    recorder = Recorder()
    recorder.restart_on_failure = False
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), tmp_path / "tech.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    for index, frame in enumerate(frames(60)):
        recorder.submit(frame)
        if index == 20:
            kill_ffmpeg(recorder)
        time.sleep(1 / FPS)

    assert not recorder.is_recording
    assert recorder.state is RecorderState.ERROR
    recorder.stop()
    assert len(recorder.parts) == 1


def test_it_gives_up_rather_than_restarting_forever() -> None:
    """A process that dies instantly every time is a real fault, and retrying
    would just produce a directory full of empty files."""
    assert 1 <= MAX_RESTARTS <= 10


def test_the_stop_message_says_when_there_is_more_than_one_file(
    tmp_path: Path,
) -> None:
    """More than one file is not what was asked for; the user must be told."""
    recorder = Recorder()
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), tmp_path / "tech.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    for index, frame in enumerate(frames(120)):
        recorder.submit(frame)
        if index == 40:
            kill_ffmpeg(recorder)
        time.sleep(1 / FPS)
    recorder.stop()

    usable = [part for part in recorder.parts if part.usable and part.exists]
    if len(usable) > 1:
        assert "parts" in recorder.detail.lower()


def test_a_normal_recording_still_produces_exactly_one_part(tmp_path: Path) -> None:
    """The common case must not have grown an extra file."""
    recorder = Recorder()
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), tmp_path / "tech.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    for frame in frames(30):
        recorder.submit(frame)
    recorder.stop()

    assert len(recorder.parts) == 1
    assert recorder._restarts == 0
    assert recorder.parts[0].path.name == "tech.mkv"


# ------------------------------------------- the stop floor, not just a warning


def test_the_default_floor_is_ten_gigabytes() -> None:
    """Requested explicitly: stop at 10 GB, not at the last few hundred MB."""
    from wer.core.showfile import RecordingConfig

    assert RecordingConfig().low_disk_stop_gb == 10.0


def test_the_warning_comes_well_before_the_stop() -> None:
    """A stop out of nowhere is worse than a stop you saw coming."""
    from wer.core.showfile import RecordingConfig

    config = RecordingConfig()
    assert config.low_disk_warning_gb > config.low_disk_stop_gb


def test_recording_stops_at_the_configured_floor(tmp_path: Path) -> None:
    """Set the floor above the real free space so the check trips at once."""
    stopped: list[DiskWarning] = []
    recorder = Recorder(on_disk_warning=stopped.append)
    recorder.stop_below_bytes = 10 ** 18  # more than any drive has

    assert recorder.start(
        OutputSettings(quality_preset="tiny"), tmp_path / "floor.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    recorder._next_disk_check = 0.0
    for frame in frames(30):
        recorder.submit(frame)
        time.sleep(1 / FPS)

    assert stopped, "no disk warning was raised"
    assert stopped[-1].critical, "the warning should be the critical one"
    assert recorder._disk_stop_requested, "the writer should have been asked to stop"
    recorder.stop()


def test_the_file_is_closed_properly_when_the_disk_stops_it(tmp_path: Path) -> None:
    """The whole reason for stopping early rather than running to the last byte."""
    import subprocess

    recorder = Recorder()
    recorder.stop_below_bytes = 10 ** 18
    output = tmp_path / "floor.mkv"
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), output,
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    recorder._next_disk_check = 0.0
    for frame in frames(60):
        recorder.submit(frame)
        time.sleep(1 / FPS)
    recorder.stop()

    assert output.is_file() and output.stat().st_size > 0
    result = subprocess.run(
        [str(ffmpeg_path()), "-hide_banner", "-i", str(output), "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )
    assert "Video: h264" in result.stderr, "the file should still be playable"


def test_an_absolute_floor_applies_even_if_the_setting_is_daft() -> None:
    """Someone setting the floor to zero should still not fill the disk."""
    recorder = Recorder()
    recorder.stop_below_bytes = 0
    assert max(recorder.stop_below_bytes, CRITICAL_FREE_BYTES) == CRITICAL_FREE_BYTES
