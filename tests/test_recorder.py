"""Recorder behaviour, exercised against the real bundled ffmpeg.

These actually encode. That is the point: recording is "the part that must not
fail", and a mock would only prove that the mock works.

No Qt.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from wer.paths import ffmpeg_path
from wer.video.capture import Frame
from wer.video.encoder import (
    SOUND_LEVEL_KEY,
    Container,
    OutputSettings,
    sound_level_args,
    sound_stats_args,
)
from wer.video.recorder import (
    SOUND_SETTLE_SECONDS,
    SOUND_SHORT_BELOW,
    SOUND_SILENT_BELOW_DBFS,
    SOUND_SILENT_SECONDS,
    Recorder,
    RecorderState,
    _SoundDelivery,
    check_disk,
)

pytestmark = pytest.mark.skipif(
    ffmpeg_path() is None, reason="bundled ffmpeg missing; run tools/fetch-ffmpeg.ps1"
)

W, H, FPS = 640, 360, 30


def frames(count: int):
    for i in range(count):
        image = np.zeros((H, W, 3), np.uint8)
        # Moving content, so the encoder has real work and the file is not a
        # single repeated keyframe.
        x = (i * 7) % (W - 40)
        image[H // 3 : 2 * H // 3, x : x + 40] = (30, 180, 240)
        yield Frame(image=image, timestamp=time.perf_counter(), index=i)


def record(tmp_path: Path, settings: OutputSettings, count: int = 30) -> Path:
    recorder = Recorder()
    output = tmp_path / f"out.{settings.container.value}"
    assert recorder.start(
        settings, output, capture_width=W, capture_height=H, capture_fps=FPS
    ), recorder.detail
    for frame in frames(count):
        recorder.submit(frame)
    recorder.stop()
    return output


def probe(path: Path) -> str:
    result = subprocess.run(
        [str(ffmpeg_path()), "-hide_banner", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )
    return result.stderr


# --------------------------------------------------------------------- happy path


def test_records_a_playable_file(tmp_path: Path) -> None:
    output = record(tmp_path, OutputSettings(quality_preset="compact"))
    assert output.is_file() and output.stat().st_size > 1000

    info = probe(output)
    assert "Video: h264" in info
    assert f"{W}x{H}" in info


def test_frame_count_survives_the_pipe(tmp_path: Path) -> None:
    """Frames in must equal frames out. A pipe that silently eats frames would
    be the worst possible bug in this application."""
    recorder = Recorder()
    output = tmp_path / "count.mkv"
    assert recorder.start(
        OutputSettings(), output, capture_width=W, capture_height=H, capture_fps=FPS
    )
    # Everything the queue can hold at once, so nothing is refused however
    # fast this loop runs. A literal here used to assume a 90-deep queue and
    # broke when the depth was sized to what a soak showed it actually uses.
    count = recorder._queue.maxsize
    for frame in frames(count):
        assert recorder.submit(frame)
    recorder.stop()

    assert recorder.stats.frames_written == count
    assert recorder.stats.frames_dropped == 0


def test_mkv_is_readable_even_though_it_is_the_crash_safe_choice(tmp_path: Path) -> None:
    output = record(tmp_path, OutputSettings(container=Container.MKV))
    assert "matroska" in probe(output).lower() or "Video: h264" in probe(output)


def test_mp4_is_written_fragmented(tmp_path: Path) -> None:
    output = record(tmp_path, OutputSettings(container=Container.MP4))
    assert output.is_file()
    assert "Video: h264" in probe(output)


def test_recording_smaller_than_capture(tmp_path: Path) -> None:
    """Capture 640x360, record 320x180 - the 4K lever, in miniature."""
    output = record(tmp_path, OutputSettings(width=320, height=180))
    assert "320x180" in probe(output)


# ------------------------------------------------------------------ state model


def test_state_moves_idle_to_recording_to_idle(tmp_path: Path) -> None:
    seen: list[RecorderState] = []
    recorder = Recorder(on_state_change=lambda s, d: seen.append(s))
    recorder.start(
        OutputSettings(), tmp_path / "s.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    assert recorder.is_recording
    for frame in frames(10):
        recorder.submit(frame)
    recorder.stop()

    assert not recorder.is_recording
    assert RecorderState.RECORDING in seen
    assert seen[-1] is RecorderState.IDLE


def test_frames_submitted_when_idle_are_refused(tmp_path: Path) -> None:
    recorder = Recorder()
    frame = next(iter(frames(1)))
    assert recorder.submit(frame) is False


def test_starting_twice_is_refused(tmp_path: Path) -> None:
    recorder = Recorder()
    assert recorder.start(
        OutputSettings(), tmp_path / "a.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    assert recorder.start(
        OutputSettings(), tmp_path / "b.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ) is False
    recorder.stop()


def test_stop_when_idle_is_harmless() -> None:
    assert Recorder().stop() is None


# ---------------------------------------------------------------- failure paths


def test_a_bad_audio_device_fails_fast_and_explains(tmp_path: Path) -> None:
    """Finding out now beats finding out at the end of a four-hour tech."""
    recorder = Recorder()
    started = recorder.start(
        OutputSettings(audio_device="No Such Microphone 9000"),
        tmp_path / "bad.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    assert started is False
    assert recorder.state is RecorderState.ERROR
    assert recorder.detail, "must say why"
    assert "audio" in recorder.detail.lower()


def test_an_unwritable_destination_is_reported(tmp_path: Path) -> None:
    recorder = Recorder()
    # A path whose parent is a file, not a directory.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    started = recorder.start(
        OutputSettings(), blocker / "nope.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    assert started is False
    assert recorder.state is RecorderState.ERROR
    assert recorder.detail


# ----------------------------------------------------------------------- disk


def test_disk_check_reports_free_space(tmp_path: Path) -> None:
    space = check_disk(tmp_path)
    assert space is not None
    assert space.free_bytes > 0
    assert space.free_gb > 0


def test_disk_check_works_for_a_path_that_does_not_exist_yet(tmp_path: Path) -> None:
    """Arming checks the destination before creating it."""
    space = check_disk(tmp_path / "not" / "created" / "yet")
    assert space is not None and space.free_bytes > 0


def test_hours_remaining_estimate() -> None:
    space = check_disk(Path.cwd())
    assert space is not None
    assert space.hours_at(4000) > 0
    assert space.hours_at(0) == float("inf")


#: The real audio input the two tests below record from, by its DirectShow
#: name. They are skipped unless it is set. They used to take whichever input
#: was listed first, which on the development laptop is the USB Color Camera's
#: microphone -- a known-bad test article that stops working until replugged --
#: so a full run opened it, or a Blackmagic's Line In, by listing order.
LIVE_AUDIO_INPUT_ENV = "WER_LIVE_AUDIO_INPUT"


def _live_audio_input() -> str:
    import os

    from wer.video.devices import enumerate_audio_devices

    name = os.environ.get(LIVE_AUDIO_INPUT_ENV, "")
    if not name:
        pytest.skip(f"set {LIVE_AUDIO_INPUT_ENV} to the audio input to record from")
    listed = [device.name for device in enumerate_audio_devices()]
    if name not in listed:
        pytest.skip(f"{name!r} is not listed on this machine: {listed}")
    return name


def test_stop_returns_promptly_with_a_live_audio_input(tmp_path: Path) -> None:
    """The bug behind a 30-second frozen window.

    ffmpeg with a live microphone never reaches EOF on all inputs, so without
    -shortest it ignores the closed video pipe and Stop blocked until the
    shutdown timeout expired.

    Frames are offered from before start() is called, as the window's encoder
    pump offers them: offered only once it returned, the launch waited out the
    whole START_READY_WAIT for one.
    """
    device = _live_audio_input()
    recorder = Recorder()
    output = tmp_path / "audio.mkv"
    with Feeder(recorder):
        assert recorder.start(
            OutputSettings(audio_device=device), output,
            capture_width=W, capture_height=H, capture_fps=FPS,
        ), recorder.detail
        time.sleep(1.0)

    started = time.perf_counter()
    recorder.stop()
    elapsed = time.perf_counter() - started

    assert elapsed < 15.0, (
        f"stop took {elapsed:.1f}s; ffmpeg is not exiting when the pipe closes"
    )
    assert output.is_file()
    assert "Audio: aac" in probe(output)


def test_a_missing_drive_is_not_an_error_worth_a_traceback(caplog) -> None:
    """It runs on a timer. An unplugged drive is an expected state the
    Recording tab already explains, so it must not fill the log."""
    import logging

    from wer.video.recorder import check_disk

    with caplog.at_level(logging.WARNING):
        assert check_disk(Path("Q:/Techs/Tonight")) is None
    assert not caplog.records, f"logged: {[r.message for r in caplog.records]}"


# ------------------------------------------ the camera stops delivering frames


def paced(recorder: Recorder, count: int, fps: float = FPS) -> None:
    """Feed frames at a real rate.

    Not decoration: ffmpeg times this pipe by when frames arrive, so a burst
    submitted in three milliseconds produces a three-millisecond file and any
    duration measured against it would be measuring the harness.
    """
    started = time.perf_counter()
    for index, frame in enumerate(frames(count)):
        recorder.submit(frame)
        remaining = started + (index + 1) / fps - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)


def file_duration(path: Path) -> float:
    import re

    match = re.search(r"Duration: (\d+):(\d+):([\d.]+)", probe(path))
    assert match, f"no duration in:\n{probe(path)[-800:]}"
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def test_a_camera_that_stops_delivering_is_reported(tmp_path: Path, monkeypatch) -> None:
    """Nothing else tells the recorder the picture has gone.

    CameraCapture notices after 30 failed reads and tells the preview, which
    paints a message on the picture and stops there. Down here the only symptom
    is an empty queue, so a take whose camera was unplugged at 02:40 stayed in
    RECORDING for the rest of the afternoon: elapsed climbing, write_fps frozen
    at a healthy 29.4, and a chapter list written against all of it.
    """
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "STALL_TIMEOUT", 1.0)
    seen: list[tuple[RecorderState, str]] = []
    recorder = Recorder(on_state_change=lambda s, d: seen.append((s, d)))
    output = tmp_path / "stall.mkv"
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), output,
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        paced(recorder, 30)
        healthy = recorder.stats.write_fps
        time.sleep(2.5)                     # the camera has gone, and stays gone

        assert healthy > 1.0, "the control half of this test did not record"
        assert "no frames" in recorder.detail.lower(), (
            f"the recorder said nothing about the stall: {recorder.detail!r}"
        )
        assert any("no frames" in detail.lower() for _, detail in seen), (
            "nothing was reported to the state listener, so no UI could show it"
        )
        assert recorder.stats.write_fps == 0.0, (
            f"still claiming {recorder.stats.write_fps:.1f} fps on a dead camera"
        )
    finally:
        recorder.stop()


def test_a_stalled_take_is_not_described_as_longer_than_it_is(tmp_path: Path) -> None:
    """The part duration is what write_chapters is handed as the total, and
    what the marker CSV is written against. Measured before the fix: a camera
    that stopped four seconds into a fifteen-second take gave a 14.6s duration
    over a 4.0s file, and cues at 8s and 12s pointed past the end of it."""
    recorder = Recorder()
    output = tmp_path / "short.mkv"
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), output,
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        paced(recorder, 60)                 # two seconds of camera
        time.sleep(3.0)                     # then none at all
    finally:
        recorder.stop()

    assert recorder.parts, "no part was recorded"
    real = file_duration(output)
    claimed = recorder.parts[-1].duration
    assert abs(claimed - real) < 0.35, (
        f"the recording claims {claimed:.2f}s of footage; the file holds "
        f"{real:.2f}s. Every chapter after {real:.2f}s falls past the end."
    )


def test_marker_times_start_where_the_video_starts(tmp_path: Path) -> None:
    """stats.elapsed is the clock every marker, chapter and bus-log line is
    stamped from, so it has to be the video's clock.

    It was started before ffmpeg was, and _launch then spent 0.6s letting
    ffmpeg fail loudly before any frame was allowed through. Measured
    with a white flash burned into the frames: markers landed +0.581s from
    their flash at 3s, 7s and 12s alike -- a constant skew on every marker of
    every take, in the direction that puts a cue after the moment it fired.
    """
    recorder = Recorder()
    output = tmp_path / "timebase.mkv"
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), output,
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        paced(recorder, 90)                 # three seconds
        marked = recorder.stats.elapsed     # what a marker taken now would say
    finally:
        recorder.stop()

    real = file_duration(output)
    assert abs(marked - real) < 0.25, (
        f"a marker at the last frame would be written at {marked:.2f}s in a "
        f"file that is {real:.2f}s long"
    )


def test_the_measured_write_rate_stops_being_reported_when_frames_stop() -> None:
    """The rate is averaged over the last 90 writes and the deque only advances
    on a write, so it froze at the last healthy value and stayed there. The one
    readout that would reveal a dead camera reported 30.0 fps on one."""
    from wer.video.recorder import FRAME_RATE_STALE_AFTER, RecordingStats

    stats = RecordingStats(started_at=time.perf_counter())
    for _ in range(10):
        stats.note_frame()
        time.sleep(0.01)
    assert stats.write_fps > 1.0

    time.sleep(FRAME_RATE_STALE_AFTER + 0.2)
    assert stats.write_fps == 0.0, "a rate from stale samples is a lie"


# --------------------------------------------- ffmpeg wedged but still alive


def test_stop_does_not_hang_when_ffmpeg_stops_draining_the_pipe(
    tmp_path: Path, monkeypatch
) -> None:
    """The freeze with no way out but Task Manager.

    If ffmpeg stops reading stdin but stays alive -- a wedged hardware encoder,
    a stalled USB or network volume, an SMR disk pausing -- the writer thread
    blocks forever inside stdin.write(), which has no timeout. stop() then
    burned its sentinel and join timeouts and called process.stdin.close(),
    which blocks against that same in-flight write, so it never reached
    process.wait() and never reached the terminate/kill fallback written for
    exactly this case. Measured: stop() still running after 60 seconds, freed
    only by killing ffmpeg from outside.

    The stand-in reads a little and then stops reading, which is the failure
    exactly; using the real ffmpeg would mean finding a way to wedge a healthy
    encoder.
    """
    import sys
    import threading

    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "SENTINEL_TIMEOUT", 1.0)
    monkeypatch.setattr(recorder_module, "WRITER_JOIN_TIMEOUT", 2.0)
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: [
            sys.executable, "-c",
            "import sys, time; sys.stdin.buffer.read(1000); time.sleep(600)",
        ],
    )

    recorder = Recorder()
    assert recorder.start(
        OutputSettings(), tmp_path / "wedged.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    child = recorder._process
    assert child is not None

    finished = threading.Event()
    stopper = threading.Thread(target=lambda: (recorder.stop(), finished.set()))
    try:
        for frame in frames(120):           # fills the queue, wedges the writer
            recorder.submit(frame)
        stopper.start()
        assert finished.wait(25.0), (
            "stop() has not returned; the terminate/kill fallback is unreachable "
            "again and the window would be frozen with no way out"
        )
        assert child.poll() is not None, "ffmpeg was left running"
    finally:
        if child.poll() is None:
            child.kill()
        stopper.join(timeout=10.0)


# ------------------------------------------- what ffmpeg says, while it says it


def test_ffmpeg_output_arrives_while_it_is_still_running() -> None:
    """-stats separates its progress lines with carriage returns and nothing
    else, so readline() returned nothing at all until ffmpeg exited and then
    handed over the whole session as one string. Measured: zero lines in eight
    seconds of recording, then a single 1,884-character blob at exit -- and the
    failure reason shown to the operator was the first 200 characters of it,
    which is the frame counter from the first second."""
    import os
    import threading

    read_fd, write_fd = os.pipe()

    class WedgedProcess:
        stderr = os.fdopen(read_fd, "rb", buffering=0)

    recorder = Recorder()
    recorder._process = WedgedProcess()
    reader = threading.Thread(target=recorder._drain_stderr, daemon=True)
    reader.start()
    try:
        os.write(write_fd, b"frame=   30 fps= 30 q=26.0 size=1KiB time=00:00:01.00\r")
        os.write(write_fd, b"Error writing trailer: No space left on device\r")
        deadline = time.perf_counter() + 3.0
        while time.perf_counter() < deadline and not recorder.ffmpeg_output:
            time.sleep(0.05)
        assert recorder.ffmpeg_output == [
            "Error writing trailer: No space left on device"
        ], (
            "ffmpeg's own words are what the UI shows when a take fails; "
            f"got {recorder.ffmpeg_output}"
        )
    finally:
        os.close(write_fd)
        reader.join(timeout=2.0)


def test_progress_counters_are_read_but_not_kept(tmp_path: Path) -> None:
    """Reading stderr is not optional -- an unread pipe fills and deadlocks the
    process -- but forty copies of the frame counter push out the one line that
    explains a failure, which is what the tail exists to hold."""
    recorder = Recorder()
    recorder.start(
        OutputSettings(), tmp_path / "e.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    count = recorder._queue.maxsize
    for frame in frames(count):
        assert recorder.submit(frame), "ffmpeg blocked: its stderr is not being read"
    recorder.stop()

    assert recorder.stats.frames_written == count
    assert not [line for line in recorder.ffmpeg_output if line.startswith("frame=")]


# ------------------------------------------------------------ the disk warning


def test_the_low_disk_estimate_counts_down_to_the_stop_level(
    tmp_path: Path, monkeypatch
) -> None:
    """The recorder stops itself at the floor, so the space below it is not
    recording time. Counting it doubled the only warning the operator ever
    gets: 19.9 GB at 4 GB/hour was announced as "about 299 minutes" and the
    take ended 149 minutes later."""
    from wer.video import recorder as recorder_module
    from wer.video.recorder import DiskSpace, DiskWarning

    warnings: list[DiskWarning] = []
    recorder = Recorder(on_disk_warning=warnings.append)
    recorder.low_disk_bytes = 20_000_000_000
    recorder.stop_below_bytes = 10_000_000_000

    # 5,555,555 bytes over five seconds is 1,111,111 B/s -- 4.0 GB/hour, the
    # rate the app's own dialog quotes for a 1080p tech.
    output = tmp_path / "take.mkv"
    output.write_bytes(b"\0" * 5_555_555)
    recorder.stats.output_path = output
    recorder.stats.started_at = time.perf_counter() - 5.0

    monkeypatch.setattr(
        recorder_module, "check_disk",
        lambda path: DiskSpace(free_bytes=19_900_000_000, total_bytes=500_000_000_000),
    )
    recorder._next_disk_check = 0.0
    recorder._check_disk_if_due()

    assert warnings, "no low-disk warning was raised at 19.9 GB"
    minutes = warnings[0].minutes_remaining
    assert minutes is not None
    assert 140 < minutes < 160, (
        f"told the operator {minutes:.0f} minutes; there are 149 before the "
        f"recording stops itself"
    )


def test_the_idle_hours_estimate_can_be_asked_for_the_honest_figure() -> None:
    """The mid-take warning counts down to the stop floor. The Recording tab's
    idle "about N hours" line, which reads the same free space off the same
    disk, counts down to zero -- so at 20 GB one says five hours and the other
    says 150 minutes. hours_at cannot know a per-show floor by itself, so it
    takes one; this pins the arithmetic for the panel that passes it."""
    from wer.video.recorder import DiskSpace

    space = DiskSpace(free_bytes=19_900_000_000, total_bytes=500_000_000_000)
    honest = space.hours_at(4000, reserve_bytes=10_000_000_000)
    assert 140 < honest * 60 < 160, (
        f"{honest * 60:.0f} minutes, against the 149 the recorder warns about"
    )
    assert space.hours_at(4000) > honest, "the reserve has to come off the top"


# ------------------------------------------- a file that holds no picture


def kill_ffmpeg(recorder: Recorder) -> None:
    process = recorder._process
    assert process is not None
    process.kill()


def test_a_take_with_no_picture_in_it_is_not_reported_as_saved(tmp_path: Path) -> None:
    """_verify_parts is the only thing between a dead file and "Saved".

    Its test used to be `st_size == 0`, which stopped meaning anything when
    `-flush_packets 1` went into the command: ffmpeg now lands the container
    header within milliseconds of launching, before it has encoded a frame.
    Measured with ffmpeg frozen mid-take: a 593-byte MKV that ffmpeg itself
    refuses to open ("Error opening input: End of file"), reported as
    "Saved suspend.mkv" with the status bar none the wiser.

    Asserted against ffmpeg's own answer rather than against a byte count,
    because a byte count is the thing that failed here.
    """
    recorder = Recorder()
    recorder.restart_on_failure = False     # one part, so the verdict is about it
    output = tmp_path / "nopicture.mkv"
    assert recorder.start(
        OutputSettings(quality_preset="compact"), output,
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail

    paced(recorder, 10)                     # a third of a second, then gone
    kill_ffmpeg(recorder)
    time.sleep(1.0)
    recorder.stop()

    part = recorder.parts[-1]
    opens = part.exists and "Video:" in probe(part.path)
    size = part.path.stat().st_size if part.exists else 0
    assert part.usable == opens, (
        f"{part.path.name} is {size} bytes, ffmpeg "
        f"{'opens' if opens else 'refuses'} it, and the recorder calls it "
        f"{'usable' if part.usable else 'unusable'}"
    )
    if not opens:
        assert recorder.state is RecorderState.ERROR, (
            f"state {recorder.state.value} / {recorder.detail!r} over a file "
            f"with no video in it"
        )
        assert "saved" not in recorder.detail.lower()


def test_a_real_short_take_is_still_delivered(tmp_path: Path) -> None:
    """The control for the test above, and the reason the check is not a byte
    count. A cleanly closed one-second take is 1.5-3 KB -- smaller than any
    round number anyone would pick as "too small to be a recording" -- and it
    is a perfectly good file."""
    recorder = Recorder()
    output = tmp_path / "brief.mkv"
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), output,
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    paced(recorder, 30)
    recorder.stop()

    assert output.stat().st_size < 65_536, "no longer the small-file path"
    assert recorder.parts[-1].usable, (
        f"a real {output.stat().st_size}-byte recording was thrown away"
    )
    assert recorder.state is RecorderState.IDLE
    assert "Video:" in probe(output)


# --------------------------------- markers after ffmpeg died and was restarted


def test_markers_land_in_the_right_place_in_a_restarted_part(tmp_path: Path) -> None:
    """A continuation part has its own timeline, and session_offset is what
    main_window subtracts to get onto it (_write_part_chapters). It therefore
    has to be measured from the same origin the marker times are -- the
    session's first frame -- and, crucially, from the part's first FRAME rather
    than from when its ffmpeg was launched.

    The wallclock formula it replaced looks equivalent and is not. It agrees to
    within 4 ms while frames keep flowing across the restart, because both
    parts then paid the same 0.6s launch check and the two cancelled. It comes
    apart the moment the camera is not delivering when the new part opens --
    which is the same hiccup that tends to have killed ffmpeg in the first
    place. Measured with a 2.5s gap: offset 2.62 against a true 4.00, putting a
    marker at the last frame 4.38s into a 2.97s file.
    """
    recorder = Recorder()
    output = tmp_path / "restart.mkv"
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), output,
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        paced(recorder, 45)                 # a second and a half
        kill_ffmpeg(recorder)
        time.sleep(2.5)                     # the camera is not ready either
        paced(recorder, 90)                 # three seconds in the new part
        marked = recorder.stats.elapsed     # a marker taken at the last frame
    finally:
        recorder.stop()

    assert len(recorder.parts) >= 2, "ffmpeg did not restart; nothing to check"
    part = recorder.parts[-1]
    assert part.usable and part.exists

    real = file_duration(part.path)
    placed = marked - part.session_offset   # exactly what write_chapters does
    assert abs(placed - real) < 0.4, (
        f"a marker at the last frame of {part.path.name} would be written at "
        f"{placed:.2f}s into a file that is {real:.2f}s long"
    )
    assert part.session_offset <= marked, "the part starts after the take ends"


# ------------------------------------------ what happens during the shutdown


def test_ffmpeg_dying_during_the_drain_still_says_why(tmp_path: Path, caplog) -> None:
    """stop() kills ffmpeg itself to unblock a wedged write, and that death
    must not be reported or restarted. A death we did NOT cause is a different
    thing: "Error writing trailer: No space left on device" arrives on exactly
    this path, and this log line is the only place ffmpeg's reason is ever
    recorded -- main_window has let go of the take before stop() returns.

    Called directly: making a real ffmpeg fail inside the drain window is a
    race, and a test that has to win a race is one that fails on somebody
    else's laptop.
    """
    import logging

    from wer.video.recorder import RecordingPart

    recorder = Recorder()
    recorder._base_path = tmp_path / "drain.mkv"
    recorder.parts = [RecordingPart(recorder._base_path, session_offset=0.0)]
    recorder._stderr_tail.append("Error writing trailer: No space left on device")
    recorder.state = RecorderState.STOPPING

    with caplog.at_level(logging.ERROR):
        recorder._handle_ffmpeg_death()
    assert any("No space left" in record.getMessage() for record in caplog.records), (
        f"ffmpeg's last words were swallowed: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    assert len(recorder.parts) == 1, "restarted into a new part mid-shutdown"

    # The half the deadlock fix needs: a death we caused ourselves stays quiet.
    caplog.clear()
    recorder._abort.set()
    with caplog.at_level(logging.ERROR):
        recorder._handle_ffmpeg_death()
    assert not caplog.records, (
        f"reported the kill stop() performs itself: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_frames_draining_during_stop_do_not_reopen_the_recording() -> None:
    """_note_frames_resumed runs from the writer thread on any successful
    write, including the frames drained by stop(). Announcing RECORDING there
    flips the recorder out of STOPPING for the length of the drain: submit()
    starts accepting frames again and the panel repaints as Recording while
    the file is being closed."""
    recorder = Recorder()
    recorder.state = RecorderState.STOPPING
    recorder._stalled = True

    recorder._note_frames_resumed()

    assert recorder.state is RecorderState.STOPPING, (
        f"the recorder says {recorder.state.value} while it is shutting down"
    )
    assert not recorder._stalled, "the stall flag has to clear either way"


# ------------------------------------------------ stand-ins for ffmpeg itself
#
# The failures below are ffmpeg misbehaving: wedging, dying on cue, saying one
# particular thing on its way out. A real encoder cannot be made to do any of
# that on demand, so small Python processes stand in for it. The recorder
# launches them exactly as it launches ffmpeg and writes the same pipe.


def stand_in(script: str) -> list[str]:
    import sys

    return [sys.executable, "-c", script]


#: Reads its pipe to the end, like an encoder that is keeping up.
DRAINS_ITS_PIPE = (
    "import sys, collections; "
    "collections.deque(iter(lambda: sys.stdin.buffer.read(1 << 20), b''), maxlen=0)"
)
#: Takes a little, then stops reading and stays alive.
STOPS_READING = "import sys, time; sys.stdin.buffer.read(1000); time.sleep(600)"
#: Stops reading for a second and a half, then carries on.
PAUSES_THEN_RECOVERS = (
    "import sys, time, collections; sys.stdin.buffer.read(1000); time.sleep(1.5); "
    "collections.deque(iter(lambda: sys.stdin.buffer.read(1 << 20), b''), maxlen=0)"
)
#: Dies the moment frames reach it, saying nothing.
DIES_SAYING_NOTHING = "import sys; sys.stdin.buffer.read(1); sys.exit(1)"
#: Exits before it is sent anything, saying nothing. ffmpeg does that now when
#: an input will not open, because it opens the audio input before it reads the
#: pipe: with a stand-in input that could not open, it was gone 26 ms after
#: launch. (Not over an unknown encoder, as this once said: measured on 8.1.2,
#: ffmpeg finds that out only after it has read a frame.)
FAILS_TO_START = "import sys; sys.exit(1)"
#: Records half a second, then reports its DirectShow input failing and exits.
#: The wording is illustrative; what matters is that it names the dshow input,
#: which is ffmpeg's first input now.
AUDIO_INPUT_ENDS = (
    f"import sys; sys.stdin.buffer.read({W * H * 3 * 15}); "
    "sys.stderr.write('[in#0/dshow @ 000001c3a5f0e2c0] Error during demuxing: "
    "I/O error\\n'); sys.exit(1)"
)
#: Exits at launch without reading a frame, because its audio input will not
#: open: how that looks now ffmpeg opens the audio input before the pipe.
#: DirectShow's line names it; ffmpeg's own line after it names no format.
AUDIO_INPUT_WILL_NOT_OPEN = (
    "import sys; "
    "sys.stderr.write('[dshow @ 000001c3a5f0e2c0] Could not find audio only device "
    "with name [Line In] among source devices of type audio.\\n"
    "[in#0 @ 000001c3a5f0e1a0] Error opening input: I/O error\\n'); sys.exit(1)"
)
#: Stops reading, but goes on printing a rising frame count twice a second.
STOPS_READING_BUT_COUNTS = (
    "import sys, time\n"
    "sys.stdin.buffer.read(1000)\n"
    "for n in range(1, 1200):\n"
    "    sys.stderr.write(f'frame={n:5d} fps= 30 q=26.0 size=  1KiB "
    "time=00:00:01.00 dup=0 drop=0\\r')\n"
    "    sys.stderr.flush()\n"
    "    time.sleep(0.5)\n"
)
#: Takes two frames, stops reading for half a second, then exits saying what
#: the overnight soak's h264_mf ffmpeg said at every start: it could not write
#: a header, so nothing was written.
CANNOT_WRITE_A_HEADER = (
    f"import sys, time; sys.stdin.buffer.read({W * H * 3 * 2}); time.sleep(0.5); "
    "sys.stderr.write('[out#0/matroska @ 0000024e4da87300] Could not write header "
    "(incorrect codec parameters ?): Invalid data found when processing input\\n"
    "[out#0/matroska @ 0000024e4da87300] Nothing was written into output file, "
    "because at least one of its streams received no packets.\\n'); sys.exit(1)"
)
#: Encodes ten frames a second and says so the way ffmpeg does, until two and a
#: half seconds after its first frame, then as fast as frames come: an encoder
#: that falls behind, and catches up. Timed from its first frame and not from
#: launch, because start() can spend its START_READY_WAIT before a test offers
#: one.
ENCODES_TOO_SLOWLY_FOR_A_WHILE = (
    "import sys, time\n"
    "started, n = None, 0\n"
    f"while sys.stdin.buffer.read({W * H * 3}):\n"
    "    started = time.monotonic() if started is None else started\n"
    "    n += 1\n"
    "    sys.stderr.write(f'frame={n:5d} fps= 10 q=26.0 size=  1KiB "
    "time=00:00:01.00 dup=0 drop=0\\r')\n"
    "    sys.stderr.flush()\n"
    "    if time.monotonic() - started < 2.5:\n"
    "        time.sleep(0.1)\n"
)


def fails_saying(*lines: str) -> str:
    """Takes two frames, stops reading for half a second, then exits having
    printed ``lines``, as CANNOT_WRITE_A_HEADER does."""
    said = "".join(f"{line}\n" for line in lines)
    return (
        f"import sys, time; sys.stdin.buffer.read({W * H * 3 * 2}); "
        f"time.sleep(0.5); sys.stderr.write({said!r}); sys.exit(1)"
    )


#: ffmpeg's last line after any failure before the header is written.
NOTHING_WAS_WRITTEN = (
    "[out#0/matroska @ 0000016957649bc0] Nothing was written into output file, "
    "because at least one of its streams received no packets."
)
#: What the bundled 8.1.2 build printed when its audio chain could not be set
#: up (a lavfi sine asked for nine channels) while the video was fine.
AUDIO_CHAIN_FAILS = fails_saying(
    "[af#0:1 @ 000001cc477b6d00] Error reinitializing filters!",
    "[aost#0:1/aac @ 000001cc477b4440] [enc:aac @ 000001cc47780c00] Could not "
    "open encoder before EOF",
    NOTHING_WAS_WRITTEN,
)
#: What the same build printed when libx264 refused its options, with no audio
#: stream at all.
VIDEO_ENCODER_FAILS = fails_saying(
    "[libx264 @ 0000016957690f80] Error setting profile baseline.",
    "[vost#0:0/libx264 @ 0000016957690d40] [enc:libx264 @ 0000016957661880] Error "
    "while opening encoder - maybe incorrect parameters such as bit_rate, rate, "
    "width or height.",
    NOTHING_WAS_WRITTEN,
)


def writes_down_first_frame(path: Path) -> str:
    """Takes one frame, keeps its first eight bytes in ``path``, then drains."""
    return (
        f"import sys, collections; first = sys.stdin.buffer.read({W * H * 3}); "
        f"open({str(path)!r}, 'wb').write(first[:8]); "
        "collections.deque(iter(lambda: sys.stdin.buffer.read(1 << 20), b''), maxlen=0)"
    )


def drains_into(path: Path) -> str:
    """Drains its pipe, having first left a file at ``path`` big enough for the
    recorder to take on trust as holding pictures, so a take can end "Saved"
    with no encoder in it."""
    from wer.video.recorder import PROBE_BELOW_BYTES

    return (
        f"import sys, collections; "
        f"open({str(path)!r}, 'wb').write(bytes({PROBE_BELOW_BYTES + 1})); "
        "collections.deque(iter(lambda: sys.stdin.buffer.read(1 << 20), b''), maxlen=0)"
    )


# --------------------------------- ffmpeg alive, but no longer taking frames


def test_an_ffmpeg_that_stops_taking_frames_mid_take_is_restarted_in_a_new_part(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """ffmpeg stays alive and stops reading its pipe, mid-take.

    The writer then sits inside stdin.write(), which has no timeout, and none
    of its own checks run there: not the one for a dead ffmpeg, not the stall
    check, not the disk check. The take stayed in RECORDING dropping every
    frame, nobody was told, and Stop reported it "Saved" with the file ending
    where the stall began -- while the continuation into a new part, built for
    exactly this, only ever triggered on ffmpeg exiting.

    Nor is the new part then announced as the old ffmpeg recovering. The flag
    saying the stall had been reported outlived the process it was about, so
    the replacement's first frame logged "ffmpeg is taking frames again" --
    the stuck one never did; it was killed -- and overwrote the only notice
    saying which file the take had moved to.
    """
    import logging

    from wer.video import recorder as recorder_module

    caplog.set_level(logging.WARNING, logger="wer.video.recorder")
    monkeypatch.setattr(recorder_module, "WRITE_STALL_WARN_AFTER", 0.5, raising=False)
    monkeypatch.setattr(recorder_module, "WRITE_STALL_RESTART_AFTER", 1.5, raising=False)
    monkeypatch.setattr(recorder_module, "WATCHDOG_INTERVAL", 0.1, raising=False)
    # Only so that, without the watchdog, the stop() in finally does not spend
    # the best part of a minute getting past the wedge.
    monkeypatch.setattr(recorder_module, "SENTINEL_TIMEOUT", 1.0)
    monkeypatch.setattr(recorder_module, "WRITER_JOIN_TIMEOUT", 2.0)
    scripts = iter([STOPS_READING])
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(next(scripts, DRAINS_ITS_PIPE)),
    )

    said: list[str] = []
    recorder = Recorder(on_state_change=lambda state, detail: said.append(detail))
    assert recorder.start(
        OutputSettings(), tmp_path / "wedged.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    wedged = recorder._process
    assert wedged is not None
    try:
        paced(recorder, 120)                # four seconds of camera

        assert any("not taken a frame" in detail for detail in said), (
            f"nobody was told ffmpeg had stopped taking frames: {said}"
        )
        assert wedged.poll() is not None, "the stuck ffmpeg was left to it"
        assert len(recorder.parts) >= 2, "the take did not continue into a new part"
        assert recorder.state is RecorderState.RECORDING, (
            f"{recorder.state.value}: {recorder.detail}"
        )

        paced(recorder, 30)                 # the new part, recording
        assert recorder.parts[-1].path.name in recorder.detail, (
            f"the notice naming the new part was overwritten: {recorder.detail!r}"
        )
        assert not any("taking frames again" in detail for detail in said), said
        assert not any(
            "taking frames again" in record.getMessage() for record in caplog.records
        ), "the log says an ffmpeg that was killed recovered"
    finally:
        if wedged.poll() is None:
            wedged.kill()
        recorder.stop()


def test_a_pause_that_recovers_is_reported_but_not_cut(
    tmp_path: Path, monkeypatch
) -> None:
    """Why the restart waits so much longer than the warning. A disk that
    pauses and comes back lets the write finish, and killing ffmpeg during it
    would turn a short gap into a cut and lose the frames it had buffered. So
    the stall is said out loud straight away, its end is said too, and ffmpeg
    is left alone."""
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "WRITE_STALL_WARN_AFTER", 0.4, raising=False)
    monkeypatch.setattr(recorder_module, "WRITE_STALL_RESTART_AFTER", 30.0, raising=False)
    monkeypatch.setattr(recorder_module, "WATCHDOG_INTERVAL", 0.1, raising=False)
    scripts = iter([PAUSES_THEN_RECOVERS])
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(next(scripts, DRAINS_ITS_PIPE)),
    )

    said: list[str] = []
    recorder = Recorder(on_state_change=lambda state, detail: said.append(detail))
    assert recorder.start(
        OutputSettings(), tmp_path / "pause.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    paused = recorder._process
    assert paused is not None
    try:
        paced(recorder, 105)                # through the pause and out of it

        assert any("not taken a frame" in detail for detail in said), (
            f"the stall was not reported: {said}"
        )
        assert any("taking frames again" in detail for detail in said), (
            f"the recovery was not reported: {said}"
        )
        assert paused.poll() is None, "ffmpeg was killed over a pause it came back from"
        assert len(recorder.parts) == 1
        assert recorder.state is RecorderState.RECORDING
    finally:
        recorder.stop()


def test_a_stuck_write_is_not_cut_while_ffmpeg_is_still_counting_frames(
    tmp_path: Path, monkeypatch
) -> None:
    """The kill takes a second opinion as well as the clock: if ffmpeg's own
    frame count has moved since the stuck write began, ffmpeg is still doing
    something and is left to it. Nothing checked that it was: every stand-in
    printed no progress at all, so the count never moved and time alone
    decided. The stall is still said out loud."""
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "WRITE_STALL_WARN_AFTER", 0.3)
    monkeypatch.setattr(recorder_module, "WRITE_STALL_RESTART_AFTER", 1.0)
    monkeypatch.setattr(recorder_module, "WATCHDOG_INTERVAL", 0.1)
    # The wedge is left in place on purpose; this keeps the stop() at the end
    # from spending the best part of a minute getting past it.
    monkeypatch.setattr(recorder_module, "SENTINEL_TIMEOUT", 1.0)
    monkeypatch.setattr(recorder_module, "WRITER_JOIN_TIMEOUT", 2.0)
    scripts = iter([STOPS_READING_BUT_COUNTS])
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(next(scripts, DRAINS_ITS_PIPE)),
    )

    said: list[str] = []
    recorder = Recorder(on_state_change=lambda state, detail: said.append(detail))
    assert recorder.start(
        OutputSettings(), tmp_path / "counting.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    counting = recorder._process
    assert counting is not None
    try:
        paced(recorder, 75)                 # past the kill time twice over

        assert any("not taken a frame" in detail for detail in said), (
            f"the stall was not reported: {said}"
        )
        assert counting.poll() is None, (
            "ffmpeg was killed while its own frame count was still rising"
        )
        assert len(recorder.parts) == 1
    finally:
        if counting.poll() is None:
            counting.kill()
        recorder.stop()


# ----------------------------------------------- the audio input goes away


def test_a_lost_audio_input_does_not_take_the_rest_of_the_picture_with_it(
    tmp_path: Path, monkeypatch
) -> None:
    """The audio input goes away mid-take while the camera keeps delivering.

    Every restart named the same device again, died as soon as frames reached
    it, and used the restarts up within seconds -- so the rest of the tech was
    not recorded, over a camera that was fine.

    The first failure alone must not cost the sound: a restart with sound is
    launched only once the camera has a frame to give it, so where sound and
    picture come from one unit it waits until both are back. It is the second
    failure -- an ffmpeg gone at launch, as one whose audio input will not open
    now is, with DirectShow naming the input -- that does. And the take, once
    saved,
    says from when it has no sound. No device is enumerated for real: both
    device checks are stubbed.
    """
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(
        recorder_module, "_audio_device_listed", lambda name: True, raising=False
    )
    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0,), raising=False)
    built_with_audio: list[bool] = []

    def build(settings, **kwargs):
        built_with_audio.append(bool(settings.audio_device))
        if not settings.audio_device:
            return stand_in(drains_into(kwargs["output_path"]))
        if len(built_with_audio) == 1:
            return stand_in(AUDIO_INPUT_ENDS)
        return stand_in(AUDIO_INPUT_WILL_NOT_OPEN)

    monkeypatch.setattr(recorder_module, "build_ffmpeg_command", build)

    said: list[str] = []
    recorder = Recorder(on_state_change=lambda state, detail: said.append(detail))
    assert recorder.start(
        OutputSettings(audio_device="Line In (Test Interface)"), tmp_path / "sound.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        paced(recorder, 180)                # six seconds of camera

        assert recorder.state is RecorderState.RECORDING, (
            f"the take ended over its audio input: {recorder.detail}"
        )
        # Exactly this: kept at the first failure, let go at the second. The
        # last resort before giving up ends in a build without sound as well,
        # but only after MAX_RESTARTS more attempts with it, so a looser check
        # passed whether or not ffmpeg's words were listened to.
        assert built_with_audio == [True, True, False], (
            f"the sound was kept or let go at the wrong failure: {built_with_audio}"
        )
        assert recorder.audio_dropped_at is not None
        assert any(
            "WITHOUT SOUND: ffmpeg reported a problem with the audio input" in detail
            for detail in said
        ), f"nobody was told the sound had gone, or why: {said}"
    finally:
        recorder.stop()

    assert "no sound from" in recorder.detail, (
        f"the take was saved without saying it has no sound: {recorder.detail!r}"
    )


def test_an_audio_input_no_longer_listed_is_let_go_at_the_second_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """The other evidence against the sound: ffmpeg dies saying nothing
    useful, and DirectShow no longer lists the device. Unchecked, a quiet
    failure of that kind spent every restart but the last on an input that had
    gone. No device is enumerated for real: the listing is stubbed."""
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(recorder_module, "_audio_device_listed", lambda name: False)
    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0,))
    monkeypatch.setattr(recorder_module, "MAX_RESTARTS", 5)
    built_with_audio: list[bool] = []

    def build(settings, **kwargs):
        built_with_audio.append(bool(settings.audio_device))
        return stand_in(DIES_SAYING_NOTHING if settings.audio_device else DRAINS_ITS_PIPE)

    monkeypatch.setattr(recorder_module, "build_ffmpeg_command", build)

    said: list[str] = []
    recorder = Recorder(on_state_change=lambda state, detail: said.append(detail))
    assert recorder.start(
        OutputSettings(audio_device="Line In (Test Interface)"),
        tmp_path / "unplugged.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        # Long enough for the STALL_TIMEOUT the camera is watched before the
        # sound goes (_camera_still_there), and the part after.
        paced(recorder, 180)

        assert recorder.state is RecorderState.RECORDING, recorder.detail
        assert built_with_audio == [True, True, False], built_with_audio
        assert any(
            '"Line In (Test Interface)" is no longer listed' in detail for detail in said
        ), f"the reason the sound went was not given: {said}"
    finally:
        recorder.stop()


def test_a_device_list_that_is_empty_or_fails_is_not_taken_for_an_unplugged_input(
    monkeypatch,
) -> None:
    """An empty list is also what a DirectShow listing that failed or timed
    out hands back, and one that raised says nothing about the device at all.
    Read as "no longer listed", either would give up the sound for the rest of
    the take over a slow probe."""
    from wer.video import devices
    from wer.video.devices import AudioDevice
    from wer.video.recorder import _audio_device_listed

    def fails() -> list[AudioDevice]:
        raise OSError("the listing did not answer")

    monkeypatch.setattr(devices, "enumerate_audio_devices", lambda: [])
    assert _audio_device_listed("Line In") is None
    monkeypatch.setattr(devices, "enumerate_audio_devices", fails)
    assert _audio_device_listed("Line In") is None

    monkeypatch.setattr(
        devices, "enumerate_audio_devices", lambda: [AudioDevice("Microphone Array")]
    )
    assert _audio_device_listed("Line In") is False
    monkeypatch.setattr(devices, "enumerate_audio_devices", lambda: [AudioDevice("Line In")])
    assert _audio_device_listed("Line In") is True


def test_with_no_clue_why_the_picture_is_still_kept_before_giving_up(
    tmp_path: Path, monkeypatch
) -> None:
    """When ffmpeg says nothing useful and the device is still listed, the
    sound is not blamed early -- but the take is not ended over it either.
    Before giving up, the recorder tries once more without the audio input: a
    take that carries on without sound beats one that stops."""
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(
        recorder_module, "_audio_device_listed", lambda name: True, raising=False
    )
    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0,), raising=False)
    monkeypatch.setattr(recorder_module, "MAX_RESTARTS", 2)
    built_with_audio: list[bool] = []

    def build(settings, **kwargs):
        built_with_audio.append(bool(settings.audio_device))
        return stand_in(DIES_SAYING_NOTHING if settings.audio_device else DRAINS_ITS_PIPE)

    monkeypatch.setattr(recorder_module, "build_ffmpeg_command", build)

    recorder = Recorder()
    assert recorder.start(
        OutputSettings(audio_device="Line In (Test Interface)"), tmp_path / "mute.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        paced(recorder, 180)

        assert recorder.state is RecorderState.RECORDING, (
            f"gave up with a working camera: {recorder.detail}"
        )
        assert built_with_audio == [True, True, True, False], built_with_audio
        assert recorder.audio_dropped_at is not None
    finally:
        recorder.stop()


def tried_without_sound_until_given_up(
    tmp_path: Path, monkeypatch, *, with_sound: str, without_sound: str,
    listed: bool | None = True,
) -> tuple[list[bool], list[str], str]:
    """Record with an audio input nothing points at, until the take is given up.

    Returns which attempts had sound, every detail the recorder gave, and the
    one it ended the take with. No device is enumerated for real: both device
    checks are stubbed, and the device listing answers ``listed``.
    """
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(recorder_module, "_audio_device_listed", lambda name: listed)
    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0,))
    monkeypatch.setattr(recorder_module, "MAX_RESTARTS", 2)
    built_with_audio: list[bool] = []

    def build(settings, **kwargs):
        built_with_audio.append(bool(settings.audio_device))
        return stand_in(with_sound if settings.audio_device else without_sound)

    monkeypatch.setattr(recorder_module, "build_ffmpeg_command", build)

    said: list[str] = []
    recorder = Recorder(on_state_change=lambda state, detail: said.append(detail))
    assert recorder.start(
        OutputSettings(audio_device="Microphone Array (Test Interface)"),
        tmp_path / "trial.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        deadline = time.perf_counter() + 30.0
        while (
            recorder.state is RecorderState.RECORDING
            and time.perf_counter() < deadline
        ):
            paced(recorder, 6)
        ending = recorder.detail
    finally:
        recorder.stop()
    return built_with_audio, said, ending


def test_a_last_try_without_sound_blames_nothing_and_failing_alike_clears_the_sound(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """Before giving up, the recorder always tries once without sound. On the
    soak nothing ffmpeg said named the microphone array and it was still
    listed, yet the try was announced as "ffmpeg failed 6 times in a row with
    it". It then failed saying just what it had said with sound -- the same
    failure with no audio input at all -- and the take ended without saying
    the sound was not the cause."""
    import logging

    caplog.set_level(logging.INFO, logger="wer.video.recorder")
    built_with_audio, said, ending = tried_without_sound_until_given_up(
        tmp_path, monkeypatch,
        with_sound=CANNOT_WRITE_A_HEADER, without_sound=CANNOT_WRITE_A_HEADER,
    )

    assert built_with_audio == [True, True, True, False], built_with_audio
    everything = [record.getMessage() for record in caplog.records] + said
    announced = [line for line in everything if "WITHOUT SOUND" in line]
    assert announced, f"the try without sound was not announced: {said}"
    assert all("in case" in line for line in announced), announced
    assert not any(
        "in a row with it" in line
        or "problem with the audio input" in line
        or "no longer listed" in line
        for line in everything
    ), announced
    assert any("DirectShow still lists it" in line for line in announced), announced
    assert "giving up" in ending, ending
    assert "the sound was not the cause" in ending, ending


def test_a_last_try_without_sound_that_fails_another_way_does_not_clear_the_sound(
    tmp_path: Path, monkeypatch
) -> None:
    """Failing without sound some other way shows only that something fails
    without the input too. Saying the sound was not the cause there would be
    saying more than the take showed."""
    built_with_audio, said, ending = tried_without_sound_until_given_up(
        tmp_path, monkeypatch,
        with_sound=CANNOT_WRITE_A_HEADER, without_sound=DIES_SAYING_NOTHING,
    )

    assert built_with_audio == [True, True, True, False], built_with_audio
    assert "giving up" in ending, ending
    assert "was not the cause" not in ending, ending
    assert "does not show whether the sound was the cause" in ending, ending


def test_failing_without_sound_on_the_same_last_line_but_other_errors_does_not_clear_the_sound(
    tmp_path: Path, monkeypatch
) -> None:
    """ffmpeg ends any failure before a header is written on "Nothing was
    written into output file". Compared on that line alone, an audio chain
    failing with sound and an encoder failing without it were one failure, and
    the take ended saying the sound was not the cause. The lines are the
    bundled build's own, from each of those failures."""
    built_with_audio, said, ending = tried_without_sound_until_given_up(
        tmp_path, monkeypatch,
        with_sound=AUDIO_CHAIN_FAILS, without_sound=VIDEO_ENCODER_FAILS,
    )

    assert built_with_audio == [True, True, True, False], built_with_audio
    assert "giving up" in ending, ending
    assert "was not the cause" not in ending, ending
    assert "does not show whether the sound was the cause" in ending, ending


def test_failing_with_no_errors_with_sound_or_without_does_not_clear_the_sound(
    tmp_path: Path, monkeypatch
) -> None:
    """An ffmpeg that dies saying nothing, with sound and without, gives
    nothing to compare. Its last words were "no output" both times, and that
    counted as failing the same way."""
    built_with_audio, said, ending = tried_without_sound_until_given_up(
        tmp_path, monkeypatch,
        with_sound=DIES_SAYING_NOTHING, without_sound=DIES_SAYING_NOTHING,
    )

    assert built_with_audio == [True, True, True, False], built_with_audio
    assert "giving up" in ending, ending
    assert "was not the cause" not in ending, ending
    assert "does not show whether the sound was the cause" in ending, ending


def test_a_last_try_without_sound_says_so_when_the_device_list_could_not_be_read(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """The try was announced with "the device list did not show it gone"
    whatever the listing gave, including a listing that raised, timed out or
    came back empty, when no list had been read at all."""
    import logging

    caplog.set_level(logging.INFO, logger="wer.video.recorder")
    built_with_audio, said, ending = tried_without_sound_until_given_up(
        tmp_path, monkeypatch,
        with_sound=CANNOT_WRITE_A_HEADER, without_sound=CANNOT_WRITE_A_HEADER,
        listed=None,
    )

    assert built_with_audio == [True, True, True, False], built_with_audio
    trying = [
        record.getMessage() for record in caplog.records
        if "Trying the take WITHOUT SOUND" in record.getMessage()
    ]
    assert len(trying) == 1, trying
    assert "the device list could not be read" in trying[0], trying[0]
    assert "still lists" not in trying[0], trying[0]
    assert "did not show it gone" not in trying[0], trying[0]


def test_a_last_try_without_sound_that_records_says_the_sound_may_have_been_the_cause(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """A try without sound that goes on to record is the one outcome that does
    point at the audio input. It passed unsaid, and the log's last word on the
    sound stayed "in case the audio input is why"."""
    import logging

    from wer.video import recorder as recorder_module

    caplog.set_level(logging.INFO, logger="wer.video.recorder")
    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(recorder_module, "_audio_device_listed", lambda name: True)
    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0,))
    monkeypatch.setattr(recorder_module, "MAX_RESTARTS", 2)
    monkeypatch.setattr(recorder_module, "HEALTHY_PART_SECONDS", 0.5)
    built_with_audio: list[bool] = []

    def build(settings, **kwargs):
        built_with_audio.append(bool(settings.audio_device))
        return stand_in(DIES_SAYING_NOTHING if settings.audio_device else DRAINS_ITS_PIPE)

    monkeypatch.setattr(recorder_module, "build_ffmpeg_command", build)

    def may_have_been():
        return [
            record for record in caplog.records
            if "may have been the cause" in record.getMessage()
        ]

    recorder = Recorder()
    assert recorder.start(
        OutputSettings(audio_device="Microphone Array (Test Interface)"),
        tmp_path / "recovers.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        deadline = time.perf_counter() + 20.0
        while not may_have_been() and time.perf_counter() < deadline:
            paced(recorder, 15)
        paced(recorder, 45)                 # and on it goes, recording
        assert recorder.state is RecorderState.RECORDING, recorder.detail
    finally:
        recorder.stop()

    assert built_with_audio == [True, True, True, False], built_with_audio
    said = may_have_been()
    assert len(said) == 1, [record.getMessage() for record in said]
    assert said[0].levelno == logging.WARNING, said[0].levelname
    assert "3 times in a row" in said[0].getMessage(), said[0].getMessage()


# ------------------------------------------------------- the restart budget


def test_isolated_ffmpeg_deaths_far_apart_do_not_end_the_take(
    tmp_path: Path, monkeypatch
) -> None:
    """The restart budget covered the whole take and never refilled, so the
    sixth unrelated failure in a long tech ended it -- though every part before
    had recorded for most of an hour. Here the budget is two, "most of an hour"
    is half a second, and ffmpeg dies four times, each after recording
    properly."""
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "MAX_RESTARTS", 2)
    monkeypatch.setattr(recorder_module, "HEALTHY_PART_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(DRAINS_ITS_PIPE),
    )

    recorder = Recorder()
    assert recorder.start(
        OutputSettings(), tmp_path / "long.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        for death in range(1, 5):
            paced(recorder, 36)             # records properly for a while
            process = recorder._process
            assert process is not None
            process.kill()
            deadline = time.perf_counter() + 5.0
            while len(recorder.parts) <= death and time.perf_counter() < deadline:
                paced(recorder, 3)
            assert recorder.state is RecorderState.RECORDING, (
                f"gave up at failure {death}: {recorder.detail}"
            )
        paced(recorder, 36)
        assert recorder._restarts == 4
    finally:
        recorder.stop()


def test_a_run_of_failures_waits_longer_before_each_attempt(
    tmp_path: Path, monkeypatch
) -> None:
    """With nothing between attempts, a fault lasting a few seconds -- a GPU
    driver resetting, an audio interface re-enumerating -- used up every
    restart inside those seconds. The first restart still goes straight away;
    a restart after one that did not last waits first. And a fault that
    persists still ends in giving up, not in a directory of empty files."""
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0, 1.5), raising=False)
    monkeypatch.setattr(recorder_module, "MAX_RESTARTS", 2)
    launched_at: list[float] = []

    def build(settings, **kwargs):
        launched_at.append(time.perf_counter())
        return stand_in(DIES_SAYING_NOTHING)

    monkeypatch.setattr(recorder_module, "build_ffmpeg_command", build)

    recorder = Recorder()
    assert recorder.start(
        OutputSettings(), tmp_path / "flaky.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    # The first gap is timed from when start() returned, not from its launch.
    # start() waits for ffmpeg to show it is recording -- here its whole
    # START_READY_WAIT, since no frame is offered until it returns -- and a
    # restart does not wait at all. Timed from the launch, that wait was
    # counted against the restart that goes straight away. (Both gaps used to
    # hold the same 0.6 s launch check, which cancelled.)
    returned_at = time.perf_counter()
    try:
        paced(recorder, 180)
    finally:
        recorder.stop()

    assert len(launched_at) >= 3, launched_at
    straight_away = launched_at[1] - returned_at
    after_waiting = launched_at[2] - launched_at[1]
    assert after_waiting - straight_away >= 1.0, (
        f"restarts {straight_away:.2f}s and {after_waiting:.2f}s apart; the "
        f"second should have waited"
    )
    assert len(launched_at) == 3, "it did not give up on a fault that persists"


def test_a_stop_during_the_pause_before_a_restart_is_prompt(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """The pause before a restart can be half a minute, and Stop can be
    pressed in the middle of it. The writer was waiting rather than reading,
    the queue filled behind it, and stop() then took five seconds and logged
    that it could not queue its sentinel: an ERROR about the wrong thing, in
    the log someone searches the next morning."""
    import logging

    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0, 30.0), raising=False)
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(DIES_SAYING_NOTHING),
    )

    recorder = Recorder()
    assert recorder.start(
        OutputSettings(), tmp_path / "paused.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        deadline = time.perf_counter() + 10.0
        while "trying again" not in recorder.detail and time.perf_counter() < deadline:
            paced(recorder, 3)
        assert "trying again" in recorder.detail, recorder.detail
        paced(recorder, 120)                # more than the queue holds

        started = time.perf_counter()
        with caplog.at_level(logging.ERROR, logger="wer.video.recorder"):
            recorder.stop()
        took = time.perf_counter() - started
    finally:
        recorder.stop()

    assert took < 2.0, f"Stop took {took:.1f}s during the pause"
    assert not any("sentinel" in record.getMessage() for record in caplog.records), (
        [record.getMessage() for record in caplog.records]
    )


def test_the_part_after_a_restart_pause_starts_on_live_picture(
    tmp_path: Path, monkeypatch
) -> None:
    """The camera goes on delivering while the recorder pauses before a
    restart, and those frames stayed queued. So the part after the pause began
    with frames from before and during it, handed to the new ffmpeg in a burst
    as though they were live -- and ffmpeg times the file by when frames reach
    it. Each frame here carries the moment it was offered in its first eight
    bytes, and the stand-in for the part after the pause writes down the first
    one it is given."""
    import struct

    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0, 1.0), raising=False)
    first_frame = tmp_path / "first-frame.bin"
    scripts = iter([DIES_SAYING_NOTHING, DIES_SAYING_NOTHING])
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(
            next(scripts, None) or writes_down_first_frame(first_frame)
        ),
    )
    pause_began: list[float] = []

    def listen(state: RecorderState, detail: str) -> None:
        if "trying again" in detail:
            pause_began.append(time.perf_counter())

    recorder = Recorder(on_state_change=listen)
    assert recorder.start(
        OutputSettings(), tmp_path / "live.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        deadline = time.perf_counter() + 10.0
        index = 0
        while not first_frame.is_file() and time.perf_counter() < deadline:
            image = np.zeros((H, W, 3), np.uint8)
            offered_at = time.perf_counter()
            image.reshape(-1)[:8] = np.frombuffer(struct.pack("<d", offered_at), np.uint8)
            recorder.submit(Frame(image=image, timestamp=offered_at, index=index))
            index += 1
            time.sleep(1 / FPS)
    finally:
        recorder.stop()                     # the stand-in has closed the file by now

    assert pause_began, "the recorder never paused before a restart"
    assert first_frame.is_file(), "the part after the pause was never given a frame"
    (first_offered_at,) = struct.unpack("<d", first_frame.read_bytes()[:8])
    assert first_offered_at >= pause_began[0], (
        f"the part after the pause began on a frame offered "
        f"{pause_began[0] - first_offered_at:.2f}s before the pause did"
    )
    assert recorder.stats.frames_dropped > 0, (
        "the frames offered with no ffmpeg to take them were not counted as dropped"
    )


# ------------------------------------------------- drops, in the take's record


def test_frames_turned_away_while_ffmpeg_keeps_failing_are_not_blamed_on_the_encoder(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """The overnight soak on h264_mf: ffmpeg took two frames at every start,
    could not write a header and died. The log said "The encoder is not keeping
    up with capture" after a restarted ffmpeg had taken its two frames and
    before its death was logged, and the finish message put all 2,638 lost
    frames down to the encoder, which never produced a picture.

    Frames are offered at 240 a second so that the queue fills in the half
    second a failing ffmpeg sits on its third frame, and while the next one
    starts: where the soak's were turned away."""
    import logging

    from wer.video import recorder as recorder_module

    caplog.set_level(logging.INFO, logger="wer.video.recorder")
    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0,))
    monkeypatch.setattr(recorder_module, "MAX_RESTARTS", 2)
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(CANNOT_WRITE_A_HEADER),
    )

    recorder = Recorder()
    assert recorder.start(
        OutputSettings(), tmp_path / "header.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        deadline = time.perf_counter() + 30.0
        while (
            recorder.state is RecorderState.RECORDING
            and time.perf_counter() < deadline
        ):
            paced(recorder, 24, fps=240)
        assert recorder.state is RecorderState.ERROR, recorder.detail
    finally:
        recorder.stop()

    stats = recorder.stats
    assert stats.frames_dropped > 0, "nothing was turned away, so nothing was tested"
    assert stats.frames_dropped_ffmpeg_failing == stats.frames_dropped, (
        f"{stats.frames_dropped - stats.frames_dropped_ffmpeg_failing} of "
        f"{stats.frames_dropped} frames were put down to an encoder falling behind"
    )
    said = [record.getMessage() for record in caplog.records]
    assert not any(
        "keeping up" in line or "Encoder queue full" in line or "fell behind" in line
        for line in said
    ), [line for line in said if "keep" in line or "behind" in line]
    assert any(
        "ffmpeg failed and nothing is taking frames" in line for line in said
    ), said
    assert any("all because ffmpeg failed" in line for line in said), said


def test_an_encoder_that_really_falls_behind_is_still_said_to(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """The other side of the test above. Frames turned away while ffmpeg is
    running and its own frame count is climbing are the encoder not keeping
    up, and the log says so while the take is still recording, not only once
    it has been stopped."""
    import logging

    from wer.video import recorder as recorder_module

    caplog.set_level(logging.INFO, logger="wer.video.recorder")
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(ENCODES_TOO_SLOWLY_FOR_A_WHILE),
    )

    recorder = Recorder()
    assert recorder.start(
        OutputSettings(), tmp_path / "slow.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        paced(recorder, 240, fps=120)       # twelve times what it is taking
        time.sleep(0.5)                     # for its frame count to be read
        while_recording = [record.getMessage() for record in caplog.records]
    finally:
        recorder.stop()

    stats = recorder.stats
    assert stats.frames_dropped > 0, "nothing was turned away, so nothing was tested"
    assert stats.frames_dropped_ffmpeg_failing == 0, (
        f"{stats.frames_dropped_ffmpeg_failing} of {stats.frames_dropped} frames "
        f"were put down to ffmpeg failing, with ffmpeg encoding throughout"
    )
    assert any(
        "not keeping up with capture" in line for line in while_recording
    ), while_recording
    said = [record.getMessage() for record in caplog.records]
    assert not any("ffmpeg failed" in line for line in said), said


def test_drops_keep_being_counted_in_the_log_after_the_thousandth(
    caplog, monkeypatch
) -> None:
    """Drops were logged at 1, 10, 100 and 1,000 and never again, so the next
    morning nobody could tell 1,001 dropped frames from 300,000. ffmpeg here
    is running and says it has encoded another frame after each one it could
    not take, which is what makes these an encoder falling behind."""
    import logging

    from wer.video import recorder as recorder_module

    class _Running:
        def poll(self) -> None:
            return None

    monkeypatch.setattr(recorder_module, "DROP_REPORT_INTERVAL", 0.2, raising=False)
    recorder = Recorder()
    recorder.state = RecorderState.RECORDING    # and nothing draining the queue
    recorder._process = _Running()
    frame = next(iter(frames(1)))
    while recorder.submit(frame):
        pass
    encoded = 0

    def turned_away_while_still_encoding() -> None:
        nonlocal encoded
        recorder.submit(frame)
        encoded += 1
        recorder._note_stderr(
            f"frame={encoded:5d} fps= 10 q=26.0 size=  1KiB "
            f"time=00:00:01.00 dup=0 drop=0",
            recorder._process,
        )

    with caplog.at_level(logging.ERROR, logger="wer.video.recorder"):
        for _ in range(1_499):
            turned_away_while_still_encoding()
        time.sleep(0.3)
        turned_away_while_still_encoding()

    assert recorder.stats.frames_dropped == 1_501
    said = [record.getMessage() for record in caplog.records]
    assert any("1501" in line for line in said), (
        f"the last the log heard was: {said[-1]!r}"
    )


class _Polled:
    """Just enough of a Popen for the recorder to ask whether it is running."""

    def __init__(self) -> None:
        self.exit_code: int | None = None

    def poll(self) -> int | None:
        return self.exit_code


#: A progress line in the shape the bundled build prints, from an ffmpeg that
#: took a frame or two before it failed.
LATE_PROGRESS = (
    "frame=    2 fps=0.0 q=0.0 size=       0KiB time=00:00:00.06 "
    "bitrate=N/A dup=0 drop=0 speed=N/A"
)


def found_dead_with_a_full_queue() -> tuple[Recorder, _Polled, Frame]:
    """A recorder whose writer has just found its ffmpeg dead, with its queue
    full and nothing draining it. Returns the recorder, the dead ffmpeg and a
    frame to offer."""
    recorder = Recorder()
    recorder.state = RecorderState.RECORDING
    dead = _Polled()
    recorder._process = dead
    frame = next(iter(frames(1)))
    while recorder.submit(frame):
        pass
    recorder._judge_pending_drops(ffmpeg_failed=True)
    return recorder, dead, frame


def test_a_late_frame_count_from_an_ffmpeg_found_dead_does_not_blame_the_encoder() -> None:
    """ffmpeg's reader can hand over a line printed on the way out after the
    writer has found that ffmpeg dead, before poll() says it has exited. The
    line showed the count moving, and the frames turned away after it were
    put down to an encoder falling behind."""
    recorder, dead, frame = found_dead_with_a_full_queue()

    recorder._note_stderr(LATE_PROGRESS, dead)
    recorder.submit(frame)

    stats = recorder.stats
    assert stats.frames_dropped_ffmpeg_failing == stats.frames_dropped, (
        f"{stats.frames_dropped - stats.frames_dropped_ffmpeg_failing} of "
        f"{stats.frames_dropped} frames were put down to an encoder falling behind"
    )


def test_a_late_frame_count_from_a_replaced_ffmpeg_does_not_blame_the_encoder() -> None:
    """The same line, read once a replacement has been launched and before it
    has counted a frame. It was judged by whether the replacement was running,
    so the frames turned away while it started were put down to an encoder
    falling behind."""
    recorder, dead, frame = found_dead_with_a_full_queue()
    dead.exit_code = 1
    recorder._process = _Polled()

    recorder._note_stderr(LATE_PROGRESS, dead)
    recorder.submit(frame)

    stats = recorder.stats
    assert stats.frames_dropped_ffmpeg_failing == stats.frames_dropped, (
        f"{stats.frames_dropped - stats.frames_dropped_ffmpeg_failing} of "
        f"{stats.frames_dropped} frames were put down to an encoder falling behind"
    )


def test_the_end_of_take_summary_says_how_many_frames_were_dropped(
    tmp_path: Path, caplog
) -> None:
    """The stop summary gave frames, megabytes and seconds for each part and no
    drop figure at all, so a take that lost thousands of frames finished with
    the same log lines as a perfect one."""
    import logging

    recorder = Recorder()
    assert recorder.start(
        OutputSettings(quality_preset="tiny"), tmp_path / "drops.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    paced(recorder, 30)
    recorder.stats.frames_dropped = 9_600       # what an encoder that fell behind leaves

    with caplog.at_level(logging.INFO, logger="wer.video.recorder"):
        recorder.stop()

    assert any(
        record.levelno >= logging.WARNING and "9600" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
    assert recorder.stats.frames_dropped == 9_600, (
        "the figure has to still be there for whoever reports the take"
    )
    assert "9,600 frames dropped" in recorder.detail, (
        f"the take was reported saved without its drops: {recorder.detail!r}"
    )


# --------------------------------------- the disk estimate, over a long take


def test_the_low_disk_estimate_counts_every_part_after_a_continuation(
    tmp_path: Path, monkeypatch
) -> None:
    """After ffmpeg died and the take continued into part 2, the rate was part
    2's bytes over the whole take's seconds: at 4 GB/hour, ffmpeg dying at 3:00
    and the warning firing at 3:10 said "about 2822 minutes" to a recording
    that stopped itself 148 minutes later. The Recording tab's size showed
    part 2 alone, too."""
    from wer.video import recorder as recorder_module
    from wer.video.recorder import DiskSpace, DiskWarning, RecordingPart

    warnings: list[DiskWarning] = []
    recorder = Recorder(on_disk_warning=warnings.append)
    recorder.low_disk_bytes = 20_000_000_000
    recorder.stop_below_bytes = 10_000_000_000

    # 1,111,111 B/s for 11,400 s is 12,666,665,400 bytes: all but a megabyte in
    # the finished part, as last measured, and a real megabyte being written.
    finished = RecordingPart(tmp_path / "take.mkv", session_offset=0.0)
    finished.size_bytes = 12_665_665_400
    current = tmp_path / "take-part2.mkv"
    current.write_bytes(b"\0" * 1_000_000)
    recorder.parts = [finished, RecordingPart(current, session_offset=10_800.0)]
    recorder.stats.output_path = current
    recorder.stats.started_at = time.perf_counter() - 11_400.0
    recorder.stats.first_frame_at = recorder.stats.started_at

    monkeypatch.setattr(
        recorder_module, "check_disk",
        lambda path: DiskSpace(free_bytes=19_900_000_000, total_bytes=500_000_000_000),
    )
    recorder._next_disk_check = 0.0
    recorder._check_disk_if_due()

    assert warnings, "no low-disk warning was raised at 19.9 GB"
    minutes = warnings[0].minutes_remaining
    assert minutes is not None and 140 < minutes < 160, (
        f"told the operator {minutes} minutes; there are 149 before it stops"
    )
    assert recorder.stats.file_size_mb > 12_000, (
        f"{recorder.stats.file_size_mb:.0f} MB for a 12.7 GB take"
    )


def test_the_low_disk_estimate_follows_a_change_of_pace(
    tmp_path: Path, monkeypatch
) -> None:
    """Averaged over the whole take, two hours with the house dark and then a
    lit act read as a third of the real rate: 4 GB in 188 minutes promised
    "about 470 minutes" to a take that stopped itself 174 minutes later.

    The sizes are given at made-up times a minute apart. They used to be
    measured either side of a real half-second sleep, and the rate is bytes
    over that gap, so the range allowed about a tenth of a second of lateness
    -- which a busy machine can add to a sleep without trying."""
    from wer.video import recorder as recorder_module
    from wer.video.recorder import DiskSpace, DiskWarning

    warnings: list[DiskWarning] = []
    recorder = Recorder(on_disk_warning=warnings.append)
    recorder.low_disk_bytes = 20_000_000_000
    recorder.stop_below_bytes = 10_000_000_000
    recorder.stats.output_path = tmp_path / "take.mkv"
    now = time.perf_counter()
    recorder.stats.started_at = now - 7_200.0
    recorder.stats.first_frame_at = recorder.stats.started_at
    # Only the sizes given below: the disk check must not add a real one.
    monkeypatch.setattr(recorder, "_sample_size_if_due", lambda **kwargs: None)
    free = {"bytes": 25_000_000_000}
    monkeypatch.setattr(
        recorder_module, "check_disk",
        lambda path: DiskSpace(free_bytes=free["bytes"], total_bytes=500_000_000_000),
    )

    recorder.stats.note_size(now - 60.0, 1_000_000)     # two dark hours: next to nothing
    recorder._next_disk_check = 0.0
    recorder._check_disk_if_due()               # plenty of room yet
    assert not warnings

    recorder.stats.note_size(now, 1_000_000 + 60 * 1_111_111)  # a lit minute: 4 GB/hour
    free["bytes"] = 19_900_000_000
    recorder._next_disk_check = 0.0
    recorder._check_disk_if_due()

    assert warnings, "no low-disk warning was raised at 19.9 GB"
    minutes = warnings[0].minutes_remaining
    assert minutes is not None and 140 < minutes < 160, (
        f"told the operator {minutes} minutes; at the rate the stage is "
        f"writing now there are about 149"
    )


def test_the_low_disk_warning_is_given_again_as_time_runs_out(
    tmp_path: Path, monkeypatch
) -> None:
    """It fired once and latched. Once is right for "running low" -- every ten
    seconds for four hours would be 1,440 warnings -- but it made the first
    estimate the only one anyone ever got, however wrong it had become."""
    from wer.video import recorder as recorder_module
    from wer.video.recorder import DiskSpace, DiskWarning

    warnings: list[DiskWarning] = []
    recorder = Recorder(on_disk_warning=warnings.append)
    recorder.low_disk_bytes = 20_000_000_000
    recorder.stop_below_bytes = 10_000_000_000
    output = tmp_path / "take.mkv"
    output.write_bytes(b"\0" * 5_555_555)       # 4 GB/hour over five seconds
    recorder.stats.output_path = output
    recorder.stats.started_at = time.perf_counter() - 5.0

    for free in (19_900_000_000, 19_000_000_000, 13_600_000_000,
                 13_500_000_000, 11_500_000_000, 10_600_000_000):
        monkeypatch.setattr(
            recorder_module, "check_disk",
            lambda path, free=free: DiskSpace(free, 500_000_000_000),
        )
        recorder._next_disk_check = 0.0
        recorder._check_disk_if_due()

    told = [round(warning.minutes_remaining or 0) for warning in warnings]
    assert len(told) == 4, f"warned at {told} minutes"
    assert told == sorted(told, reverse=True), told


# --------------------------------------------- what ffmpeg says, in the log


def test_ffmpeg_warnings_reach_the_log_without_flooding_it(caplog, monkeypatch) -> None:
    """ffmpeg runs at -loglevel warning, so anything it prints is a warning or
    worse by its own reckoning -- yet every line without "error", "failed" or
    "invalid" in it went to DEBUG, below what the app logs, and never reached
    wer.log. The opposite mistake is as bad: a warning printed once a frame,
    logged every time, buries the log in minutes. The wording is illustrative;
    what matters is one line repeating with a number that changes."""
    import logging

    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "FFMPEG_REPEAT_INTERVAL", 0.3, raising=False)
    recorder = Recorder()
    with caplog.at_level(logging.DEBUG, logger="wer.video.recorder"):
        recorder._note_stderr(
            "frame=   30 fps= 30 q=26.0 size=1KiB time=00:00:01.00 dup=0 drop=0",
            None,
        )
        for index in range(500):
            recorder._note_stderr(
                f"[out#0/matroska @ 000001c3a5f0e2c0] Past duration "
                f"0.{999990 + index % 7} too large",
                None,
            )
        time.sleep(0.4)
        recorder._note_stderr(
            "[out#0/matroska @ 000001d4b6a1f3d0] Past duration 0.999996 too large",
            None,
        )

    from_ffmpeg = [r for r in caplog.records if r.getMessage().startswith("ffmpeg:")]
    assert from_ffmpeg, "ffmpeg's warning never reached the log"
    assert all(r.levelno >= logging.WARNING for r in from_ffmpeg), (
        "logged below the level the app writes"
    )
    assert len(from_ffmpeg) == 2, f"{len(from_ffmpeg)} lines for one repeating warning"
    assert "500" in from_ffmpeg[-1].getMessage(), from_ffmpeg[-1].getMessage()
    assert not any("frame=" in r.getMessage() for r in caplog.records), (
        "a progress line is not news"
    )


def test_a_line_ffmpeg_prints_on_every_healthy_take_is_logged_but_not_as_a_warning(
    caplog,
) -> None:
    """A take on the laptop's microphone array opens with ffmpeg guessing the
    channel layout -- printed in an hour-long overnight soak whose sound was
    correct. As a WARNING it made every healthy take on that input look
    faulty. It still reaches the log, and a real warning still warns."""
    import logging

    recorder = Recorder()
    with caplog.at_level(logging.INFO, logger="wer.video.recorder"):
        recorder._note_stderr(
            "[aist#1:0/pcm_s16le @ 000001c61657b8c0] Guessed Channel Layout: stereo",
            None,
        )
        recorder._note_stderr(
            "[out#0/matroska @ 000001c3a5f0e2c0] Past duration 0.999996 too large",
            None,
        )

    levels = {
        record.getMessage(): record.levelno
        for record in caplog.records
        if record.getMessage().startswith("ffmpeg:")
    }
    guessed = next(message for message in levels if "Guessed Channel Layout" in message)
    past = next(message for message in levels if "Past duration" in message)
    assert levels[guessed] == logging.INFO, "a routine line was logged as a warning"
    assert levels[past] == logging.WARNING, "a real warning stopped warning"


# ---------------------------------------------------- the disk-floor stop


def test_the_disk_floor_stop_still_writes_the_frames_already_queued(
    tmp_path: Path,
) -> None:
    """The comment on the disk stop has always said the queued frames are still
    written. They were not: the writer broke out at the top of its next pass,
    before reading the queue, abandoning every one of them -- the mistake the
    sentinel comment in _write_frames records costing the end of every take.
    And frames offered after the floor are refused, rather than queued behind
    a writer that has stopped reading."""
    import threading

    class _Pipe:
        def __init__(self) -> None:
            self.frames = 0

        def write(self, data) -> int:
            self.frames += 1
            # BYTES written, as a real raw FileIO returns. len() would be the
            # row count now that frames go to the pipe as ndarrays rather than
            # through .tobytes(), and the recorder checks this figure to catch
            # a short write shearing the rest of the take.
            return memoryview(data).nbytes

        def close(self) -> None:
            pass

    class _Ffmpeg:
        def __init__(self) -> None:
            self.stdin = _Pipe()

        def poll(self) -> None:
            return None

    recorder = Recorder()
    recorder.stop_below_bytes = 10 ** 18        # every real disk is under this floor
    recorder.state = RecorderState.RECORDING
    recorder.stats.output_path = tmp_path / "floor.mkv"
    ffmpeg = _Ffmpeg()
    recorder._process = ffmpeg
    recorder._writer = threading.current_thread()   # the writer loop runs right here
    queued = recorder._queue.maxsize
    for frame in frames(queued):
        assert recorder.submit(frame)

    # The watchdog notices the floor, not the writer -- see _check_disk_if_due.
    # This test is about what the WRITER does once the stop has been requested,
    # so it makes the request the same way the watchdog would and then runs the
    # writer loop.
    recorder._next_disk_check = 0.0
    recorder._check_disk_if_due()
    assert recorder._disk_stop_requested, "the floor was not noticed"

    recorder._write_frames()

    assert ffmpeg.stdin.frames == queued, (
        f"{queued - ffmpeg.stdin.frames} queued frame(s) were abandoned at the floor"
    )
    assert recorder.submit(next(iter(frames(1)))) is False, (
        "still accepting frames that nothing will write"
    )
    assert recorder.stats.frames_dropped == 0, (
        "refusing frames after the stop is not the encoder dropping them"
    )


def test_stop_does_not_wait_on_a_writer_that_has_already_finished() -> None:
    """Once the disk floor or a failure has ended the writer, nothing reads the
    queue. stop() still pushed its sentinel in with a blocking put, so a queue
    that had filled meanwhile cost the whole SENTINEL_TIMEOUT and a misleading
    "could not queue the stop sentinel" -- and the frames stayed in memory
    until the next take."""
    import threading

    recorder = Recorder()
    recorder.state = RecorderState.RECORDING
    finished = threading.Thread(target=lambda: None)
    finished.start()
    finished.join()
    recorder._writer = finished
    frame = next(iter(frames(1)))
    while recorder.submit(frame):
        pass

    started = time.perf_counter()
    recorder.stop()
    took = time.perf_counter() - started

    assert took < 2.0, f"stop() waited {took:.1f}s on a writer that had already gone"
    assert recorder.queue_depth == 0, "frames nothing will ever write are still held"


def test_stop_stops_asking_for_room_once_the_writer_has_left(caplog) -> None:
    """The other way a writer is gone: it leaves WHILE stop() is waiting to
    queue the sentinel. One whose ffmpeg dies during the drain returns without
    reading on, and so did one pausing before a restart. A single blocking put
    cannot see that happen, so a full queue still cost the whole
    SENTINEL_TIMEOUT and the ERROR about the sentinel."""
    import logging
    import threading

    recorder = Recorder()
    recorder.state = RecorderState.RECORDING
    frame = next(iter(frames(1)))
    while recorder.submit(frame):
        pass
    leaving = threading.Thread(target=time.sleep, args=(0.3,))
    recorder._writer = leaving
    leaving.start()

    started = time.perf_counter()
    with caplog.at_level(logging.ERROR, logger="wer.video.recorder"):
        recorder.stop()
    took = time.perf_counter() - started

    assert took < 2.0, f"stop() waited {took:.1f}s for a writer that left after 0.3s"
    assert not any("sentinel" in record.getMessage() for record in caplog.records), (
        [record.getMessage() for record in caplog.records]
    )


# ------------------------------------------------ one writer, and only one


def test_a_failed_start_does_not_leave_a_second_writer_behind(
    tmp_path: Path, monkeypatch
) -> None:
    """A start whose ffmpeg fails to start -- an audio input or an encoder this
    machine cannot open is the everyday case -- left its writer thread polling the queue
    for good. The next start that worked then had two writers taking frames
    off one queue and writing them into one pipe, in whatever order the two
    threads happened to run."""
    import threading

    from wer.video import recorder as recorder_module

    commands = iter([stand_in(FAILS_TO_START), stand_in(DRAINS_ITS_PIPE)])
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command", lambda *args, **kwargs: next(commands)
    )
    already_running = set(threading.enumerate())

    recorder = Recorder()
    assert not recorder.start(
        OutputSettings(), tmp_path / "refused.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    )
    assert recorder.start(
        OutputSettings(), tmp_path / "works.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        time.sleep(1.0)                     # longer than a writer's empty poll
        writers = [
            thread for thread in threading.enumerate()
            if thread.name == "ffmpeg-writer" and thread not in already_running
        ]
        assert len(writers) == 1, f"{len(writers)} writer threads feed one ffmpeg"
    finally:
        recorder.stop()


# ---------------------------------------- sound and picture starting together
#
# ffmpeg opens its audio input first and then reads the frame waiting in its
# pipe (see build_ffmpeg_command), so a take with sound launches it on a frame
# already queued, frames flow from then on, and start() reads readiness from
# the pipe. The stand-ins are launched as
# ffmpeg is, with frames offered from another thread the way the window's
# encoder pump offers them: from before start() is called.


class Feeder:
    """Offers frames at a real rate from its own thread, as the encoder pump
    does, each carrying the moment it was offered in its first eight bytes.

    Keeps, for every offer, the state before it, whether the frame was taken,
    and the state after it."""

    def __init__(self, recorder: Recorder, fps: float = FPS) -> None:
        import threading

        self.recorder = recorder
        self.fps = fps
        self.offers: list[tuple[RecorderState, bool, RecorderState]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._offer, name="test-feeder", daemon=True
        )

    def __enter__(self) -> Feeder:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _offer(self) -> None:
        import struct

        started = time.perf_counter()
        index = 0
        while not self._stop.is_set():
            image = np.zeros((H, W, 3), np.uint8)
            offered_at = time.perf_counter()
            image.reshape(-1)[:8] = np.frombuffer(struct.pack("<d", offered_at), np.uint8)
            before = self.recorder.state
            taken = self.recorder.submit(
                Frame(image=image, timestamp=offered_at, index=index)
            )
            self.offers.append((before, taken, self.recorder.state))
            index += 1
            remaining = started + index / self.fps - time.perf_counter()
            if remaining > 0:
                self._stop.wait(remaining)

    def taken_while_starting(self) -> int:
        return sum(
            1 for before, taken, after in self.offers
            if taken and before is RecorderState.STARTING
            and after is RecorderState.STARTING
        )


#: Reads one frame, takes nothing for a second, then drains: an ffmpeg that has
#: opened its inputs and takes a second more to open its output.
READS_ONE_FRAME_THEN_PAUSES = (
    f"import sys, time, collections; sys.stdin.buffer.read({W * H * 3}); "
    "time.sleep(1.0); "
    "collections.deque(iter(lambda: sys.stdin.buffer.read(1 << 20), b''), maxlen=0)"
)
#: Reads nothing for two and a half seconds, then drains: an audio input slow to
#: open, which ffmpeg opens before it reads a frame.
OPENS_ITS_AUDIO_INPUT_SLOWLY = (
    "import sys, time, collections; time.sleep(2.5); "
    "collections.deque(iter(lambda: sys.stdin.buffer.read(1 << 20), b''), maxlen=0)"
)
#: Reads nothing and says nothing, for good: an audio input whose open hangs.
#: Measured with the bundled 8.1.2 build and an input that never delivered, the
#: first write stayed blocked until ffmpeg was killed, with nothing on stderr.
AUDIO_INPUT_HANGS_OPENING = "import time; time.sleep(600)"
#: Reads nothing for a third of a second, then drains: an audio input opening
#: about as fast as the fastest measured.
OPENS_ITS_AUDIO_INPUT_QUICKLY = (
    "import sys, time, collections; time.sleep(0.3); "
    "collections.deque(iter(lambda: sys.stdin.buffer.read(1 << 20), b''), maxlen=0)"
)
#: Records half a second, stops reading, and half a second later reports its
#: DirectShow input failing and exits: the frames a camera delivers while a
#: dying ffmpeg winds down queue up behind the write that finds it dead.
AUDIO_INPUT_ENDS_AND_WINDS_DOWN = (
    f"import sys, time; sys.stdin.buffer.read({W * H * 3 * 15}); time.sleep(0.5); "
    "sys.stderr.write('[in#0/dshow @ 000001c3a5f0e2c0] Error during demuxing: "
    "I/O error\\n'); sys.exit(1)"
)


def opens_slowly_then_notes_two_frames(path: Path) -> str:
    """Waits 0.6 s, as an audio input opening does, then reads two frames and
    keeps the first eight bytes of each in ``path``, then drains."""
    return (
        "import sys, time, collections; time.sleep(0.6); "
        f"first = sys.stdin.buffer.read({W * H * 3}); "
        f"second = sys.stdin.buffer.read({W * H * 3}); "
        f"open({str(path)!r}, 'wb').write(first[:8] + second[:8]); "
        "collections.deque(iter(lambda: sys.stdin.buffer.read(1 << 20), b''), maxlen=0)"
    )


def test_frames_are_taken_while_starting_once_the_audio_input_is_checked_and_not_before(
    tmp_path: Path, monkeypatch
) -> None:
    """ffmpeg opens its audio input and then reads the frame waiting in its
    pipe, and the take's sync rests on one being there. Frames were refused
    until start() returned, so the first reached ffmpeg after its input had
    opened, and every take's sound came out 280-790 ms early.

    Not before start() has asked DirectShow for its devices, though, which can
    take fifteen seconds, and frames queued then would only be thrown away.
    Refused then, they are not counted as dropped.

    And start() returns once the SECOND frame has landed -- ffmpeg reads the
    first as it finishes opening its inputs, and the second only once it has
    opened its output -- not at the first, and not at its cap. This stand-in
    reads one frame, then nothing for a second."""
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "START_READY_WAIT", 10.0)
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(READS_ONE_FRAME_THEN_PAUSES),
    )
    recorder = Recorder()
    before_launch: list[bool] = []
    frame = next(iter(frames(1)))

    def ask_directshow(name):
        before_launch.append(recorder.submit(frame))
        return None

    monkeypatch.setattr(recorder_module, "_validate_audio_device", ask_directshow)
    try:
        with Feeder(recorder) as feeder:
            started = time.perf_counter()
            assert recorder.start(
                OutputSettings(audio_device="Line In (Test Interface)"),
                tmp_path / "flowing.mkv",
                capture_width=W, capture_height=H, capture_fps=FPS,
            ), recorder.detail
            took = time.perf_counter() - started
            state_at_return = recorder.state
            landed_at_return = recorder.stats.frames_written
    finally:
        recorder.stop()

    assert before_launch == [False], "a frame was taken before there was an ffmpeg"
    assert feeder.taken_while_starting() > 0, "no frame reached ffmpeg while it started"
    assert state_at_return is RecorderState.RECORDING, state_at_return
    assert landed_at_return >= 2, f"announced with {landed_at_return} frame(s) landed"
    assert 0.9 <= took < 5.0, (
        f"start() took {took:.2f}s. Under the stand-in's one-second pause, it did "
        f"not wait for the second frame; near its ten-second cap, it did not read "
        f"readiness from the pipe."
    )
    assert recorder.stats.frames_dropped == 0


def test_an_audio_input_that_will_not_open_refuses_the_take_with_ffmpegs_words(
    tmp_path: Path, monkeypatch
) -> None:
    """ffmpeg opens its audio input before it reads a frame, and exits at once
    when it cannot. The fixed 0.6 s check that start() used to make caught none
    of that, with frames only let through once it was over; the take was
    announced and failed a frame later. It is refused now, as ffmpeg exits,
    with ffmpeg's reason, and nothing queued for it is left behind."""
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(AUDIO_INPUT_WILL_NOT_OPEN),
    )
    seen: list[RecorderState] = []
    recorder = Recorder(on_state_change=lambda state, detail: seen.append(state))
    with Feeder(recorder):
        started = time.perf_counter()
        started_ok = recorder.start(
            OutputSettings(audio_device="Line In (Test Interface)"),
            tmp_path / "no-sound.mkv",
            capture_width=W, capture_height=H, capture_fps=FPS,
        )
        took = time.perf_counter() - started
        time.sleep(0.2)                     # frames still being offered
        taken_after = recorder.submit(next(iter(frames(1))))

    assert started_ok is False
    assert RecorderState.RECORDING not in seen, f"announced as recording first: {seen}"
    assert recorder.state is RecorderState.ERROR
    assert "could not open the audio device" in recorder.detail, recorder.detail
    assert took < 1.5, f"refused {took:.2f}s after start(), not as ffmpeg exited"
    assert taken_after is False, "still taking frames for a take that did not start"
    assert recorder.queue_depth == 0, "frames queued for the failed take are still held"
    assert recorder.stats.frames_dropped == 0


def test_a_slow_audio_input_is_announced_at_the_cap_and_said_to_be_slow(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """start() runs on the interface thread, so it waits for readiness only so
    long. At its cap with the first frame still in flight, the audio input is
    still opening: the take is announced, and if the input goes on not opening
    the operator is told so in those words, not that ffmpeg has stopped taking
    frames. Once it opens, that is said too, because the file begins then. And
    the frames that piled up meanwhile are not counted as dropped: none of them
    is newer than the file's first picture."""
    import logging

    from wer.video import recorder as recorder_module

    caplog.set_level(logging.INFO, logger="wer.video.recorder")
    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(recorder_module, "START_READY_WAIT", 0.5)
    monkeypatch.setattr(recorder_module, "FIRST_WRITE_WARN_AFTER", 1.0)
    monkeypatch.setattr(recorder_module, "FIRST_WRITE_RESTART_AFTER", 30.0)
    monkeypatch.setattr(recorder_module, "WATCHDOG_INTERVAL", 0.1)
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(OPENS_ITS_AUDIO_INPUT_SLOWLY),
    )
    said: list[str] = []
    recorder = Recorder(on_state_change=lambda state, detail: said.append(detail))
    try:
        with Feeder(recorder):
            started = time.perf_counter()
            assert recorder.start(
                OutputSettings(audio_device="Line In (Test Interface)"),
                tmp_path / "slow.mkv",
                capture_width=W, capture_height=H, capture_fps=FPS,
            ), recorder.detail
            took = time.perf_counter() - started
            landed_at_return = recorder.stats.frames_written
            deadline = time.perf_counter() + 6.0
            while recorder.stats.frames_written < 5 and time.perf_counter() < deadline:
                time.sleep(0.05)
            assert recorder.stats.frames_written >= 5, "the input never opened"
    finally:
        recorder.stop()

    assert took < 1.5, f"start() held the interface thread {took:.2f}s past its cap"
    assert landed_at_return == 0, "the first frame had landed; nothing was tested"
    assert any("has not opened after" in detail for detail in said), said
    assert not any("not taken a frame" in detail for detail in said), said
    assert any("took" in detail and "to open" in detail for detail in said), said
    assert recorder.stats.frames_dropped == 0, (
        f"{recorder.stats.frames_dropped} frames queued while the input opened "
        f"were counted as dropped"
    )
    assert any(
        "given up, oldest first" in record.getMessage() for record in caplog.records
    ), "the frames the full queue gave up were not logged"


def test_a_take_with_sound_launches_ffmpeg_on_a_frame_already_waiting(
    tmp_path: Path, monkeypatch
) -> None:
    """The take's sync rests on a frame waiting in the pipe when ffmpeg's audio
    input opens. Frames were taken from launch, which a camera at 30 fps meets
    long before the fastest open measured -- but the Blackmagic with nothing
    reaching it sends a placeholder at 1.0 fps, and a take started over one
    launched ffmpeg with no frame in hand: the input opened, the first frame
    came up to a second later, and the sound was that late for the whole take,
    with nothing said. ffmpeg is launched only once a frame is queued now, and
    that frame waits in the pipe for the input, which here takes a third of a
    second."""
    import threading

    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(recorder_module, "START_READY_WAIT", 3.0)
    launched_at: list[float] = []

    def build(settings, **kwargs):
        launched_at.append(time.perf_counter())
        return stand_in(OPENS_ITS_AUDIO_INPUT_QUICKLY)

    monkeypatch.setattr(recorder_module, "build_ffmpeg_command", build)
    said: list[str] = []
    recorder = Recorder(on_state_change=lambda state, detail: said.append(detail))
    offered_at: list[float] = []
    stop = threading.Event()

    def placeholder() -> None:
        stop.wait(0.6)
        index = 0
        while not stop.is_set():
            offered_at.append(time.perf_counter())
            recorder.submit(
                Frame(image=np.zeros((H, W, 3), np.uint8), timestamp=offered_at[-1], index=index)
            )
            index += 1
            stop.wait(1.0)

    feeder = threading.Thread(target=placeholder, name="test-placeholder", daemon=True)
    feeder.start()
    try:
        assert recorder.start(
            OutputSettings(audio_device="Line In (Blackmagic Test Unit)"),
            tmp_path / "no-signal.mkv",
            capture_width=W, capture_height=H, capture_fps=FPS,
        ), recorder.detail
    finally:
        stop.set()
        feeder.join(timeout=5.0)
        recorder.stop()

    assert launched_at and offered_at, (launched_at, offered_at)
    assert launched_at[0] >= offered_at[0], (
        f"ffmpeg was launched {offered_at[0] - launched_at[0]:.2f}s before the "
        f"first frame was offered"
    )
    assert recorder.stats.first_frame_at - offered_at[0] >= 0.2, (
        "the first frame did not wait in the pipe for the audio input"
    )
    assert not any("late against the picture" in detail for detail in said), said


def test_a_first_frame_after_the_audio_input_opened_is_said_to_leave_the_sound_late(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """With no frame by START_READY_WAIT, ffmpeg is launched without one -- a
    camera stalled at Record may come back, and the picture is the part that
    cannot be had again -- and when its audio input opens before the first
    frame arrives, the file's sound is late by the difference for as long as it
    runs. That went unsaid: the first frame landed at once, and nothing looked
    at a first write that had not waited. It is said now, on screen, with what
    to do about it."""
    import logging
    import threading

    from wer.video import recorder as recorder_module

    caplog.set_level(logging.INFO, logger="wer.video.recorder")
    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(recorder_module, "START_READY_WAIT", 0.3)
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(DRAINS_ITS_PIPE),
    )
    said: list[str] = []
    recorder = Recorder(on_state_change=lambda state, detail: said.append(detail))
    stop = threading.Event()

    def late_camera() -> None:
        stop.wait(0.9)
        index = 0
        while not stop.is_set():
            recorder.submit(
                Frame(image=np.zeros((H, W, 3), np.uint8), timestamp=time.perf_counter(), index=index)
            )
            index += 1
            stop.wait(1.0 / FPS)

    feeder = threading.Thread(target=late_camera, name="test-late-camera", daemon=True)
    feeder.start()
    try:
        assert recorder.start(
            OutputSettings(audio_device="Line In (Test Interface)"),
            tmp_path / "late.mkv",
            capture_width=W, capture_height=H, capture_fps=FPS,
        ), recorder.detail
        deadline = time.perf_counter() + 5.0
        while recorder.stats.frames_written < 3 and time.perf_counter() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        feeder.join(timeout=5.0)
        recorder.stop()

    assert recorder.stats.frames_written >= 3, "no frame reached the stand-in"
    assert any("late against the picture" in detail for detail in said), said
    assert any(
        record.levelno == logging.WARNING
        and "late against the picture" in record.getMessage()
        for record in caplog.records
    ), "the late sound was not logged as a warning"
    # The launch waited out START_READY_WAIT for a frame and said so, and the
    # take was announced a moment later. That announcement said ffmpeg had had
    # no frame on its way in for START_READY_WAIT after it was launched.
    messages = [record.getMessage() for record in caplog.records]
    assert not any("No frame was on its way into ffmpeg" in m for m in messages), messages


def test_an_audio_input_that_never_opens_is_restarted_in_seconds_not_a_minute(
    tmp_path: Path, monkeypatch
) -> None:
    """A DirectShow open that hangs says nothing: measured, the first write
    blocked until ffmpeg was killed, with nothing on stderr. It is killed after
    FIRST_WRITE_RESTART_AFTER rather than the minute a stalled mid-take write is
    given, and goes down the restart path like any other death -- keeping the
    sound at the first failure."""
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(recorder_module, "_audio_device_listed", lambda name: True)
    monkeypatch.setattr(recorder_module, "START_READY_WAIT", 0.3)
    monkeypatch.setattr(recorder_module, "FIRST_WRITE_WARN_AFTER", 0.5)
    monkeypatch.setattr(recorder_module, "FIRST_WRITE_RESTART_AFTER", 1.5)
    monkeypatch.setattr(recorder_module, "WATCHDOG_INTERVAL", 0.1)
    built_with_audio: list[bool] = []

    def build(settings, **kwargs):
        built_with_audio.append(bool(settings.audio_device))
        if len(built_with_audio) == 1:
            return stand_in(AUDIO_INPUT_HANGS_OPENING)
        return stand_in(DRAINS_ITS_PIPE)

    monkeypatch.setattr(recorder_module, "build_ffmpeg_command", build)
    said: list[str] = []
    recorder = Recorder(on_state_change=lambda state, detail: said.append(detail))
    hung = None
    try:
        with Feeder(recorder):
            assert recorder.start(
                OutputSettings(audio_device="Line In (Test Interface)"),
                tmp_path / "hangs.mkv",
                capture_width=W, capture_height=H, capture_fps=FPS,
            ), recorder.detail
            hung = recorder._process
            deadline = time.perf_counter() + 8.0
            while len(recorder.parts) < 2 and time.perf_counter() < deadline:
                time.sleep(0.05)
            time.sleep(0.5)                 # the new part, recording
            assert recorder.state is RecorderState.RECORDING, recorder.detail
    finally:
        if hung is not None and hung.poll() is None:
            hung.kill()
        recorder.stop()

    assert hung is not None
    assert len(recorder.parts) == 2, "the take did not continue into a new part"
    assert built_with_audio == [True, True], built_with_audio
    assert recorder.audio_dropped_at is None, "the sound was given up at the first failure"
    assert any("has not opened after" in detail for detail in said), said
    assert any("had not opened after" in detail for detail in said), said
    assert recorder.parts[-1].frames > 0


def test_an_audio_input_that_does_not_open_twice_running_is_let_go_at_the_second(
    tmp_path: Path, monkeypatch
) -> None:
    """An open that hangs prints nothing and leaves the device listed, so each
    kill for it was a failure with nothing pointing at the input: six of them,
    and every pause between, before the take was even tried without sound --
    about 164 s of picture at the shipped values, at the top of an act. Two
    kills running are the evidence now, and the second lets the input go, said
    as a finding and not as a trial. The first still keeps it."""
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(recorder_module, "_audio_device_listed", lambda name: True)
    monkeypatch.setattr(recorder_module, "START_READY_WAIT", 0.3)
    monkeypatch.setattr(recorder_module, "FIRST_WRITE_WARN_AFTER", 0.3)
    monkeypatch.setattr(recorder_module, "FIRST_WRITE_RESTART_AFTER", 1.0)
    monkeypatch.setattr(recorder_module, "WATCHDOG_INTERVAL", 0.1)
    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0,))
    monkeypatch.setattr(recorder_module, "MAX_RESTARTS", 5)
    built_with_audio: list[bool] = []

    def build(settings, **kwargs):
        built_with_audio.append(bool(settings.audio_device))
        return stand_in(
            AUDIO_INPUT_HANGS_OPENING if settings.audio_device else DRAINS_ITS_PIPE
        )

    monkeypatch.setattr(recorder_module, "build_ffmpeg_command", build)
    said: list[str] = []
    recorder = Recorder(on_state_change=lambda state, detail: said.append(detail))
    launched: list[subprocess.Popen] = []
    try:
        with Feeder(recorder):
            assert recorder.start(
                OutputSettings(audio_device="Line In (Test Interface)"),
                tmp_path / "wedged.mkv",
                capture_width=W, capture_height=H, capture_fps=FPS,
            ), recorder.detail
            deadline = time.perf_counter() + 12.0
            while len(built_with_audio) < 3 and time.perf_counter() < deadline:
                process = recorder._process
                if process is not None and process not in launched:
                    launched.append(process)
                time.sleep(0.02)
            time.sleep(0.5)                 # the part after, recording
            assert recorder.state is RecorderState.RECORDING, recorder.detail
    finally:
        for process in launched:
            if process.poll() is None:
                process.kill()
        recorder.stop()

    assert built_with_audio == [True, True, False], built_with_audio
    assert recorder.audio_dropped_at is not None
    assert any(
        "WITHOUT SOUND" in detail and "did not open in 1s, 2 times in a row" in detail
        for detail in said
    ), said
    assert not any("in case the audio input" in detail for detail in said), said
    assert recorder.parts[-1].frames > 0


def test_a_restart_that_dies_at_launch_is_one_more_failure_not_the_end_of_the_take(
    tmp_path: Path, monkeypatch
) -> None:
    """An audio input that has gone now makes ffmpeg exit before it reads a
    frame, within tens of milliseconds. The check that followed every launch
    caught exactly that and set ERROR, and the take ended "ffmpeg stopped and
    could not be restarted" at the first restart -- past MAX_RESTARTS, the
    pauses between attempts and carrying on without sound, losing the rest of
    the picture over the sound. Each such death counts as one more failure in a
    row instead."""
    from wer.video import recorder as recorder_module

    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(recorder_module, "_audio_device_listed", lambda name: True)
    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0,))
    monkeypatch.setattr(recorder_module, "MAX_RESTARTS", 2)
    built_with_audio: list[bool] = []

    def build(settings, **kwargs):
        built_with_audio.append(bool(settings.audio_device))
        if not settings.audio_device:
            return stand_in(DRAINS_ITS_PIPE)
        if len(built_with_audio) == 1:
            return stand_in(AUDIO_INPUT_ENDS)
        return stand_in(FAILS_TO_START)

    monkeypatch.setattr(recorder_module, "build_ffmpeg_command", build)
    recorder = Recorder()
    assert recorder.start(
        OutputSettings(audio_device="Microphone Array (Test Interface)"),
        tmp_path / "gone.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        paced(recorder, 180)
        assert recorder.state is RecorderState.RECORDING, (
            f"a death at launch ended the take: {recorder.detail}"
        )
    finally:
        recorder.stop()

    assert built_with_audio == [True, True, True, False], built_with_audio
    assert recorder.stats.most_ffmpeg_failures_in_a_row == 3


def test_a_restart_with_sound_waits_for_the_camera_so_an_unplugged_unit_keeps_its_sound(
    tmp_path: Path, monkeypatch
) -> None:
    """A Blackmagic carries the picture and its Line In together, and both go
    when it is unplugged. ffmpeg opens the audio input before reading a frame,
    so a restart launched at once would die at once with DirectShow blaming the
    input, the second failure a moment later would drop the sound, and it would
    stay dropped for the rest of the take though the unit came back.

    With a frame in hand first, nothing is launched until the camera delivers
    again -- which for that unit is when its sound is back too. And while it
    waits, the take says so."""
    import threading

    from wer.video import recorder as recorder_module

    unplugged = threading.Event()
    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(
        recorder_module, "_audio_device_listed", lambda name: not unplugged.is_set()
    )
    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0,))
    monkeypatch.setattr(recorder_module, "STALL_TIMEOUT", 0.5)
    launches: list[tuple[bool, float]] = []

    def build(settings, **kwargs):
        launches.append((bool(settings.audio_device), time.perf_counter()))
        if len(launches) == 1:
            return stand_in(AUDIO_INPUT_ENDS)
        if unplugged.is_set():
            return stand_in(AUDIO_INPUT_WILL_NOT_OPEN)
        return stand_in(DRAINS_ITS_PIPE)

    monkeypatch.setattr(recorder_module, "build_ffmpeg_command", build)
    said: list[str] = []
    recorder = Recorder(on_state_change=lambda state, detail: said.append(detail))
    assert recorder.start(
        OutputSettings(audio_device="Line In (Blackmagic Test Unit)"),
        tmp_path / "unplugged.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        unplugged.set()
        paced(recorder, 16)                 # the part takes fifteen, then it is pulled
        time.sleep(1.5)                     # no picture and no sound
        unplugged.clear()
        back_at = time.perf_counter()
        paced(recorder, 45)                 # plugged back in
        assert recorder.state is RecorderState.RECORDING, recorder.detail
    finally:
        recorder.stop()

    assert [with_audio for with_audio, _ in launches] == [True, True], launches
    assert launches[1][1] >= back_at, (
        f"restarted {back_at - launches[1][1]:.2f}s before the camera came back"
    )
    assert recorder.audio_dropped_at is None, "the sound was given up for the take"
    assert any("no frames have come from the camera" in detail for detail in said), said


def test_the_first_restart_with_sound_is_not_launched_on_a_frame_from_before_the_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """The first failure has no pause before its restart, so nothing emptied
    the queue, and a restart with sound took the frame at its head: one the
    camera delivered before the unit was pulled, queued behind the write that
    found ffmpeg dead. Launched on it, the part died at once with DirectShow
    naming the input, and that second failure dropped the sound for the rest of
    the take, though the unit came back seconds later. Only a frame captured
    after the death was dealt with starts the part now, and the ones before it
    are counted as lost to ffmpeg failing."""
    import threading

    from wer.video import recorder as recorder_module

    unplugged = threading.Event()
    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(
        recorder_module, "_audio_device_listed", lambda name: not unplugged.is_set()
    )
    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0, 1.0, 3.0))
    monkeypatch.setattr(recorder_module, "STALL_TIMEOUT", 0.5)
    monkeypatch.setattr(recorder_module, "START_READY_WAIT", 0.3)
    launches: list[tuple[bool, float]] = []

    def build(settings, **kwargs):
        launches.append((bool(settings.audio_device), time.perf_counter()))
        if len(launches) == 1:
            return stand_in(AUDIO_INPUT_ENDS_AND_WINDS_DOWN)
        if unplugged.is_set():
            return stand_in(AUDIO_INPUT_WILL_NOT_OPEN)
        return stand_in(DRAINS_ITS_PIPE)

    monkeypatch.setattr(recorder_module, "build_ffmpeg_command", build)
    recorder = Recorder()
    assert recorder.start(
        OutputSettings(audio_device="Line In (Blackmagic Test Unit)"),
        tmp_path / "pulled.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        unplugged.set()
        # Fifteen taken; the sixteenth waits on a pipe nobody reads, and six
        # more queue behind it while ffmpeg winds down, before the unit is
        # pulled a quarter of a second ahead of ffmpeg exiting.
        paced(recorder, 22)
        time.sleep(1.5)                     # no picture and no sound
        unplugged.clear()
        back_at = time.perf_counter()
        paced(recorder, 45)                 # plugged back in
        assert recorder.state is RecorderState.RECORDING, recorder.detail
    finally:
        recorder.stop()

    assert recorder.audio_dropped_at is None, "the sound was given up for the take"
    assert [with_audio for with_audio, _ in launches] == [True, True], launches
    assert launches[1][1] >= back_at, (
        f"restarted {back_at - launches[1][1]:.2f}s before the camera came back"
    )
    # How many frames queue behind the blocked write while ffmpeg winds down
    # is the machine's pace, not the recorder's doing: six here on a quiet
    # machine, five under a full suite. What the test is for is that whatever
    # queued before the death is counted as lost rather than launched on --
    # launched on, the restart would have consumed them and counted none.
    assert recorder.stats.frames_dropped_ffmpeg_failing >= 1, (
        f"{recorder.stats.frames_dropped_ffmpeg_failing} frame(s) counted as lost "
        f"to ffmpeg failing; the ones queued before the restart were not"
    )


def test_a_camera_that_outlasts_its_audio_input_by_a_few_frames_does_not_cost_the_sound(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """Pulled out, a Blackmagic loses its Line In and its picture together, but
    the recorder need not see them go together. Here ffmpeg finds the input
    gone at once, the camera hands over a few more frames, and a restart
    launched on one of them dies at launch with DirectShow naming the input.
    That second failure dropped the sound for the rest of the take, though the
    unit was back a second and a half later. A probe of that case still
    failed after frames from before the failure were set aside. Before
    sound is given up on evidence, the camera is watched now: it went
    STALL_TIMEOUT without a frame, so the evidence is not held against the
    input, and the take restarts with sound once the camera is back."""
    import logging
    import threading

    from wer.video import recorder as recorder_module

    caplog.set_level(logging.INFO, logger="wer.video.recorder")
    unplugged = threading.Event()
    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(
        recorder_module, "_audio_device_listed", lambda name: not unplugged.is_set()
    )
    monkeypatch.setattr(recorder_module, "RESTART_DELAYS", (0.0,))
    monkeypatch.setattr(recorder_module, "STALL_TIMEOUT", 0.5)
    monkeypatch.setattr(recorder_module, "START_READY_WAIT", 0.3)
    launches: list[tuple[bool, float]] = []

    def build(settings, **kwargs):
        launches.append((bool(settings.audio_device), time.perf_counter()))
        if len(launches) == 1:
            return stand_in(AUDIO_INPUT_ENDS)
        if unplugged.is_set():
            return stand_in(AUDIO_INPUT_WILL_NOT_OPEN)
        return stand_in(DRAINS_ITS_PIPE)

    monkeypatch.setattr(recorder_module, "build_ffmpeg_command", build)
    recorder = Recorder()
    assert recorder.start(
        OutputSettings(audio_device="Line In (Blackmagic Test Unit)"),
        tmp_path / "outlasted.mkv",
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        unplugged.set()
        # Fifteen taken, ffmpeg gone at the sixteenth, and the camera delivers
        # a fifth of a second more before the picture goes too.
        paced(recorder, 21)
        time.sleep(1.5)                     # no picture and no sound
        unplugged.clear()
        back_at = time.perf_counter()
        paced(recorder, 45)                 # plugged back in
        assert recorder.state is RecorderState.RECORDING, recorder.detail
    finally:
        recorder.stop()

    assert recorder.audio_dropped_at is None, (
        f"the sound was given up for the take; launches relative to the replug: "
        f"{[(audio, round(at - back_at, 2)) for audio, at in launches]}"
    )
    assert all(with_audio for with_audio, _ in launches), launches
    assert launches[-1][1] >= back_at, (
        f"the part that recorded was launched {back_at - launches[-1][1]:.2f}s "
        f"before the camera came back"
    )


def test_frames_queued_while_the_audio_input_opened_do_not_open_the_file(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """ffmpeg reads the first frame only once its audio input has opened, and
    every frame queued meanwhile was read straight after it in a burst, older
    than the file's first picture: measured, three frames of 0.1-0.3 s old
    picture opened every take. Once the first frame lands those are dropped,
    and the second frame ffmpeg reads is live. Not counted as dropped: nothing
    is missing from the file, which begins at its first frame."""
    import logging
    import struct

    from wer.video import recorder as recorder_module

    caplog.set_level(logging.INFO, logger="wer.video.recorder")
    notes = tmp_path / "first-two.bin"
    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(opens_slowly_then_notes_two_frames(notes)),
    )
    recorder = Recorder()
    try:
        with Feeder(recorder):
            assert recorder.start(
                OutputSettings(audio_device="Line In (Test Interface)"),
                tmp_path / "stale.mkv",
                capture_width=W, capture_height=H, capture_fps=FPS,
            ), recorder.detail
            deadline = time.perf_counter() + 5.0
            while time.perf_counter() < deadline and not (
                notes.is_file() and notes.stat().st_size >= 16
            ):
                time.sleep(0.05)
    finally:
        recorder.stop()

    assert notes.is_file() and notes.stat().st_size >= 16, "the stand-in read no two frames"
    first_offered, second_offered = struct.unpack("<dd", notes.read_bytes()[:16])
    landed = recorder.stats.first_frame_at
    assert landed - first_offered > 0.4, "the first frame did not wait for the input"
    assert landed - second_offered < 1.0 / FPS, (
        f"the second frame ffmpeg read was offered {landed - second_offered:.2f}s "
        f"before the first landed: stale backlog opened the file"
    )
    assert any(
        "captured before ffmpeg read the first frame" in record.getMessage()
        for record in caplog.records
    ), "the dropped backlog was not logged"
    assert recorder.stats.frames_dropped == 0


def test_a_marker_taken_before_the_first_frame_lands_is_placed_at_the_start() -> None:
    """Until the first frame reaches ffmpeg there is no video to be at a time
    in. elapsed counted from start() then, and that stretch is now ffmpeg's
    launch plus its audio input opening, different on every take -- so a cue
    fired while the take started was marked late by it."""
    from wer.video.recorder import RecordingStats

    stats = RecordingStats(started_at=time.perf_counter() - 0.8)
    assert stats.elapsed == 0.0, f"a marker now would be written at {stats.elapsed:.2f}s"
    stats.note_frame()
    time.sleep(0.05)
    assert 0.04 <= stats.elapsed < 0.5, stats.elapsed


# ------------------------------------ an audio input delivering half its sound
#
# A USB webcam's microphone hands over its sound in gaps of about 50 ms in every
# 100. Every format it offers loses the same half, and so does ffmpeg reading it
# outside Wer, so it is the device. A ten-minute take had every frame of its
# picture, 50.0% of its sound missing, and not one line in the log. The gaps
# stay in the timestamps, which is what the recorder now reads out of ffmpeg's
# own per-frame statistics.


def sound_stats(seconds: float, arrives=lambda at: True, rate: int = 48000):
    """ffmpeg's sound statistics for a stretch of sound, frame by frame.

    ``arrives`` says whether the frame starting at that moment reaches the
    encoder. The ones it turns away leave their time in the timestamps with no
    samples in it, which is what sound missing at the input looks like.
    """
    samples, pts = 1024, 0
    while pts < seconds * rate:
        if arrives(pts / rate):
            yield f"wer-sound tb=1/{rate} pts={pts} samp={samples}"
        pts += samples


def half_of_every_tenth(at: float) -> bool:
    """The USB webcam microphone's pattern: about 50 ms in every 100."""
    return at % 0.1 < 0.05


def upto(lines, seconds: float, rate: int = 48000) -> tuple[list[str], list[str]]:
    """The statistics up to that moment by their own timestamps, and the rest."""
    lines = list(lines)
    cut = sum(
        1 for line in lines
        if int(line.split("pts=")[1].split()[0]) < seconds * rate
    )
    return lines[:cut], lines[cut:]


def listening_recorder(device: str = "Microphone (USB Color Camera)"):
    """A recorder mid-take with an audio input, and what it says about it.

    No ffmpeg: the statistics are handed to it directly, as the thread reading
    ffmpeg's stdout does.
    """
    said: list[str] = []
    recorder = Recorder(on_sound_warning=said.append)
    recorder.state = RecorderState.RECORDING
    recorder.settings = OutputSettings(audio_device=device)
    recorder._process = _Polled()
    return recorder, said


def feed(recorder: Recorder, lines, process=None) -> None:
    for line in lines:
        recorder._note_sound_stats(line, process or recorder._process)


def test_sound_that_arrives_whole_is_not_remarked_on() -> None:
    recorder, said = listening_recorder()
    feed(recorder, sound_stats(60))

    assert said == []
    share = recorder._sound.delivered / recorder._sound.timeline
    assert share == pytest.approx(1.0)


def test_an_input_delivering_half_its_sound_is_said_once(caplog) -> None:
    """Once a take: it is a fault of the device, and it does not improve."""
    import re

    recorder, said = listening_recorder()
    with caplog.at_level("WARNING", logger="wer.video.recorder"):
        feed(recorder, sound_stats(120, half_of_every_tenth))

    assert len(said) == 1, said
    assert "Microphone (USB Color Camera)" in said[0], said[0]
    percent = int(re.search(r"Only (\d+)%", said[0]).group(1))
    assert 45 <= percent <= 55, said[0]
    assert recorder.detail == said[0], "the Recording tab was not told"
    assert any(said[0] in record.getMessage() for record in caplog.records), (
        "nothing about it reached the log"
    )


def test_nothing_is_said_until_half_a_minute_has_been_judged() -> None:
    """Five seconds are left to settle and the judgement covers three ten-second
    windows, so the warning comes about 35 seconds into the file rather than on
    the first window that reads short."""
    recorder, said = listening_recorder()
    first, rest = upto(sound_stats(45, half_of_every_tenth), 34)

    feed(recorder, first)
    assert said == [], "said before half a minute of sound had been judged"
    feed(recorder, rest)
    assert len(said) == 1, said


def test_a_warning_that_lands_before_the_take_is_recording_is_not_lost() -> None:
    """ffmpeg's statistics start arriving while its launch is still being
    judged, and a take that is STARTING is not told it is still recording. Said
    at the second short window and never again, the warning was then lost for
    the whole file: the take below ran on at half its sound in silence."""
    recorder, said = listening_recorder()
    recorder.state = RecorderState.STARTING
    first, rest = upto(sound_stats(70, half_of_every_tenth), 40)

    feed(recorder, first)
    assert said == [], "nothing can be said while the take is still starting"
    recorder.state = RecorderState.RECORDING
    feed(recorder, rest)

    assert len(said) == 1, said


def test_a_gap_of_a_couple_of_seconds_is_not_an_input_that_cannot_deliver() -> None:
    """One stumble -- a device hiccup, a machine that stalled -- is not an
    input that cannot deliver its sound. The judgement covers half a minute, so
    what it tolerates is a gap of about three seconds in that half minute; five
    is an input losing a sixth of the sound, and that is said."""
    recorder, said = listening_recorder()
    feed(recorder, sound_stats(60, lambda at: not 20.0 <= at < 22.0))
    assert said == [], "a two-second stumble was called an input in trouble"

    worse, worse_said = listening_recorder()
    feed(worse, sound_stats(60, lambda at: not 20.0 <= at < 25.0))
    assert len(worse_said) == 1, worse_said


def test_the_first_seconds_of_a_file_are_not_judged() -> None:
    """An input can hand over a burst, or next to nothing, while it opens, and
    that is not the take's sound.

    The figure the take reports is what shows this: with those seconds counted,
    an opening like the one below holds the whole take's share down for the
    rest of the session, over sound that arrived whole from five seconds on.
    Two short windows in a row are needed for a warning, so the opening alone
    never raises one either way -- which is why this measures the share.
    """
    recorder, said = listening_recorder()
    feed(recorder, sound_stats(60, lambda at: at >= 5.0 or at % 1.0 < 0.1))

    assert said == []
    share = recorder._sound.delivered / recorder._sound.timeline
    assert share == pytest.approx(1.0, abs=0.005), (
        f"the seconds while the input was opening were counted: {share:.3f}"
    )


def test_an_input_whose_gaps_are_longer_than_a_window_is_still_caught() -> None:
    """Eleven seconds of sound and eleven of nothing is half the sound, and
    never two short windows in a row.

    Judged on a run of consecutive short windows it was never reported at all,
    however long the take ran, while 6 on 6 off was reported in 25 seconds. The
    judgement rolls over the last three windows instead, so what is reported is
    what the input delivers rather than how its gaps line up.
    """
    recorder, said = listening_recorder()
    feed(recorder, sound_stats(120, lambda at: at % 22.0 < 11.0))

    assert len(said) == 1, said
    assert recorder._sound.delivered / recorder._sound.timeline < 0.6


def test_an_input_that_stops_sending_is_not_scored_as_having_delivered_it_all() -> None:
    """Sound that stops leaves no statistics behind to be noticed by.

    A window closes only when a frame arrives, so a microphone that died three
    minutes into a ten-minute take was scored on the three minutes that worked:
    the take's summary said "100.0% of the sound arrived" over a file that was
    silent from there on, and nothing was said while it was still recording.
    The watchdog times the silence from outside instead.
    """
    recorder, said = listening_recorder()
    feed(recorder, sound_stats(30))
    assert said == [], "the sound arrived whole while it was arriving"

    recorder._sound_heard_at = time.perf_counter() - 30.0
    recorder._check_the_sound_is_arriving()

    assert len(said) == 1, said
    assert "No sound at all has arrived" in said[0], said[0]
    assert "30 seconds" in said[0], said[0]
    share = recorder._sound.delivered / recorder._sound.timeline
    assert share < 0.9, f"an input that stopped was scored {share:.1%} delivered"


def test_statistics_from_an_ffmpeg_already_replaced_are_not_this_files_sound() -> None:
    """A dying ffmpeg's last lines reach the reader after its replacement has
    started. Counted here they would blame the new file for what the old one
    lost."""
    recorder, said = listening_recorder()
    feed(recorder, sound_stats(60, half_of_every_tenth), process=_Polled())

    assert said == []
    assert recorder._sound.timeline == 0.0


def test_an_input_that_has_not_spoken_yet_is_not_called_silent() -> None:
    """ffmpeg opens the audio device only once frames reach it, so quiet before
    the first line is the take starting, not the input failing. A camera slow
    to deliver would otherwise be reported as a dead microphone."""
    recorder, said = listening_recorder()

    recorder._check_the_sound_is_arriving()

    assert said == []
    assert recorder._sound.timeline == 0.0


def test_a_statistics_line_it_cannot_read_is_said_once(caplog) -> None:
    """Quietly not checking is exactly the failure this check is for."""
    recorder, said = listening_recorder()
    with caplog.at_level("WARNING", logger="wer.video.recorder"):
        feed(recorder, ["wer-sound tb=1/48000 pts=0", "wer-sound and again"])

    complaints = [
        record for record in caplog.records
        if "sound statistics" in record.getMessage()
    ]
    assert len(complaints) == 1, [record.getMessage() for record in complaints]
    assert "wer-sound tb=1/48000 pts=0" in complaints[0].getMessage()


def test_a_line_that_would_divide_by_nothing_is_read_as_unreadable(caplog) -> None:
    """The thread parsing these is the only one draining ffmpeg's stdout, and a
    pipe nobody drains stops ffmpeg taking frames within seconds. A zero time
    base raised out of the handler and took that thread with it, which reaches
    the operator as a stalled encoder with no hint of the cause."""
    recorder, said = listening_recorder()
    with caplog.at_level("WARNING", logger="wer.video.recorder"):
        feed(recorder, ["wer-sound tb=1/0 pts=0 samp=1024"] * 3)
        feed(recorder, sound_stats(45, half_of_every_tenth))

    assert len(said) == 1, "the reader carried on and still judged the sound"
    assert not [
        record for record in caplog.records if record.exc_info is not None
    ], "the handler raised where it must not"


def test_a_bug_in_the_parsing_does_not_stop_the_drain(monkeypatch, caplog) -> None:
    """The thread that parses these is the only one draining ffmpeg's stdout,
    and a pipe nobody drains stops ffmpeg taking frames within seconds -- which
    reaches the operator as a stalled encoder with no hint of the cause. The
    watchdog guards itself for the same reason."""
    recorder, said = listening_recorder()
    raised: list[int] = []
    whole = _SoundDelivery.note

    def raise_once_then_carry_on(self, start, duration):
        if not raised:
            raised.append(1)
            raise RuntimeError("a bug in here must not stop the reading")
        return whole(self, start, duration)

    monkeypatch.setattr(_SoundDelivery, "note", raise_once_then_carry_on)
    with caplog.at_level("ERROR", logger="wer.video.recorder"):
        feed(recorder, sound_stats(45, half_of_every_tenth))

    assert raised, "the stand-in was never reached"
    assert len(said) == 1, "the reading stopped at the first raise"
    assert any(record.exc_info for record in caplog.records), "it was not logged"


# An input can hand over every sample its timestamps promise and have nothing
# in any of them. The laptop's microphone array, gated by its voice effects,
# delivered exact zeros to DirectShow except while somebody spoke -- peaking at
# -84.3 dBFS, two LSB of 16-bit -- and a capture device whose HDMI carries no
# sound looks exactly the same. The delivery check above reads 1.000 for both,
# and the clap test finds nothing, so the peak level ffmpeg measures once a
# second is what catches them.


def sound_levels(seconds: float, peak=lambda at: float("-inf"), rate: int = 48000):
    """ffmpeg's report of each second's peak, as ametadata prints it.

    Two lines a second: the frame it measured, and then the measurement.
    ``peak`` says what that second's peak level was, in dBFS.
    """
    at = 0.0
    while at < seconds:
        yield f"frame:{int(at)}    pts:{int(at * rate)}       pts_time:{at:g}"
        level = peak(at)
        text = "-inf" if level == float("-inf") else f"{level:.6f}"
        yield f"{SOUND_LEVEL_KEY}={text}"
        at += 1.0


#: Two lines a second, so this many lines is that many seconds of them.
PER_SECOND = 2
#: Where the first warning can fall: five seconds settling, then the window.
SAYS_AT = int(SOUND_SETTLE_SECONDS + SOUND_SILENT_SECONDS)


def test_an_input_handing_over_nothing_but_zeros_is_said_once(caplog) -> None:
    """The whole point of the check: the sound is all arriving and there is
    nothing in it, which nothing else in the recorder can see."""
    recorder, said = listening_recorder()
    with caplog.at_level("WARNING", logger="wer.video.recorder"):
        feed(recorder, sound_levels(60))

    assert len(said) == 1, said
    assert said[0].startswith(
        "No sound from Microphone (USB Color Camera): only digital silence "
        f"for {SOUND_SILENT_SECONDS:.0f} s"
    ), said[0]
    assert "the take was not stopped" in said[0], said[0]
    # The file is named because the warning is pinned and a console start does
    # not take a pin down: read over the next act, an unnamed one is the next
    # act's. Nothing is recording here, so it is "the recording".
    assert "so the recording has none in it" in said[0], said[0]
    assert recorder.detail == said[0], "the Recording tab was not told"
    assert any(said[0] in record.getMessage() for record in caplog.records), (
        "nothing about it reached the log"
    )


def test_the_warning_names_the_file_it_is_about(tmp_path: Path) -> None:
    """It is pinned to the status bar, and a take started from the console
    leaves whatever is pinned where it is. So the same warning is still in
    front of the booth over the next act's take, and it has to say which file
    had no sound in it rather than leave "the take" to be read as this one."""
    recorder, said = listening_recorder()
    recorder.stats.output_path = tmp_path / "act-one.mkv"
    feed(recorder, sound_levels(60))

    assert len(said) == 1, said
    assert "act-one.mkv" in said[0], said[0]


def test_nothing_is_said_about_silence_until_the_window_has_passed() -> None:
    """Five seconds are left to settle -- an input can hand over zeros while it
    opens -- and the silence must then run for the whole window. Sooner than
    that and a blackout between scenes, or a house waiting on a cue, would
    raise it."""
    recorder, said = listening_recorder()
    lines = list(sound_levels(60))

    feed(recorder, lines[:PER_SECOND * SAYS_AT])
    assert said == [], "said before the settle and the window had passed"
    feed(recorder, lines[PER_SECOND * SAYS_AT:])
    assert len(said) == 1, said


def test_one_second_of_sound_is_an_input_that_is_working() -> None:
    """Silence broken by anything at all is an input delivering, however long
    the quiet stretches around it are. That is the difference between a stage
    holding still and a microphone gated shut."""
    recorder, said = listening_recorder()
    feed(recorder, sound_levels(
        300, lambda at: -12.0 if at % 10.0 < 1.0 else float("-inf")
    ))

    assert said == []


def test_a_quiet_house_is_not_digital_silence() -> None:
    """The threshold is not "quiet", it is "zeros". A live input's own noise
    floor sits far above it however still the auditorium is; what falls under
    is a muted, gated or unconnected input.

    Two minutes at -60 dBFS, which is a still house on a real input and 60 dB
    clear of nothing at all. The figure is written out rather than taken from
    the constant, so that a threshold raised towards the room fails here
    instead of putting a warning on the status bar of a quiet scene.
    """
    recorder, said = listening_recorder()
    feed(recorder, sound_levels(120, lambda at: -60.0))

    assert said == [], "a house at -60 dBFS was called silence"

    # And the threshold itself is sound, not silence: at exactly the figure
    # the input is still delivering.
    recorder, said = listening_recorder()
    feed(recorder, sound_levels(120, lambda at: SOUND_SILENT_BELOW_DBFS))

    assert said == []


def test_the_speech_a_gated_array_passes_is_still_digital_silence() -> None:
    """The other side of the threshold, and the one the check exists for.

    The laptop's microphone array handed over exact zeros except while
    somebody spoke, and the speech it did pass peaked at -84.3 dBFS -- two LSB
    of 16-bit, measured on the bundled build. A threshold under that figure
    would count every line of dialogue as the input working, the stretch would
    never reach fifteen seconds, and the take would record an hour of nothing
    without a word said about it.
    """
    recorder, said = listening_recorder()
    feed(recorder, sound_levels(
        60, lambda at: -84.288399 if at % 5.0 < 1.0 else float("-inf")
    ))

    assert len(said) == 1, said


def test_the_level_lines_are_not_read_as_statistics_nobody_can_read(caplog) -> None:
    """They share the pipe with the delivery statistics, and a line the reader
    does not know is reported as a check it cannot make. Reported for these,
    every take with sound would have opened with a warning about its own
    instrumentation."""
    recorder, said = listening_recorder()
    with caplog.at_level("WARNING", logger="wer.video.recorder"):
        feed(recorder, sound_stats(40))
        feed(recorder, sound_levels(40, lambda at: -20.0))

    assert said == []
    assert not [
        record for record in caplog.records
        if "sound statistics" in record.getMessage()
    ], [record.getMessage() for record in caplog.records]
    # And the delivery check reads the same as it did without them.
    assert recorder._sound.delivered / recorder._sound.timeline == pytest.approx(1.0)


def test_a_level_with_no_place_in_the_take_judges_nothing(caplog) -> None:
    """ffmpeg writes "N/A" for a frame it cannot place in the timeline. A level
    with no place in the take cannot be counted towards a stretch of it, and it
    is not an unreadable line either."""
    recorder, said = listening_recorder()
    lines = [
        line.split("pts_time:")[0] + "pts_time:N/A"
        if line.startswith("frame:") else line
        for line in sound_levels(60)
    ]
    with caplog.at_level("WARNING", logger="wer.video.recorder"):
        feed(recorder, lines)

    assert said == []
    assert not [
        record for record in caplog.records
        if "sound statistics" in record.getMessage()
    ]


def test_levels_from_an_ffmpeg_already_replaced_are_not_this_files_sound() -> None:
    """A dying ffmpeg's last lines reach the reader after its replacement has
    started, the same as the delivery statistics do."""
    recorder, said = listening_recorder()
    feed(recorder, sound_levels(60), process=_Polled())

    assert said == []


def test_a_take_whose_levels_never_arrive_says_nothing() -> None:
    """An older ffmpeg, a chain that would not build, an input whose sound
    never reaches the filter: the check simply does not run. It must not
    invent a warning out of silence it was never told about."""
    recorder, said = listening_recorder()
    feed(recorder, sound_stats(120))

    assert said == []


def test_an_ffmpeg_restart_starts_the_silence_over() -> None:
    """A restart opens the input again and writes a new file, whose first
    seconds are the settle all over again. The stretch measured cannot run
    across the join."""
    recorder, said = listening_recorder()
    lines = list(sound_levels(60))
    short_of_it = PER_SECOND * (SAYS_AT - 2)

    feed(recorder, lines[:short_of_it])
    assert said == []
    recorder._bank_ffmpeg_progress()

    feed(recorder, lines[:short_of_it])
    assert said == [], "the silence was carried across a restart"
    feed(recorder, lines[short_of_it:])
    assert len(said) == 1, said


def test_a_restart_discards_the_stretch_rather_than_re_reading_it() -> None:
    """The same join, with the new file's reports carrying on in time instead
    of starting from nothing.

    Fed reports that go backwards, the judgement starts the stretch again by
    itself, so the test above passes whether the restart clears it or not.
    What has to hold is that the restart clears it: an ffmpeg whose new file
    picked up the old timeline would otherwise inherit fourteen seconds of
    silence and warn on its second.
    """
    recorder, said = listening_recorder()
    lines = list(sound_levels(60))
    short_of_it = PER_SECOND * (SAYS_AT - 2)

    feed(recorder, lines[:short_of_it])
    recorder._bank_ffmpeg_progress()
    # Another dozen seconds, timed on from where the old file stopped. The new
    # file has had its settle and a few seconds of silence, and nothing like
    # the window.
    feed(recorder, lines[short_of_it:PER_SECOND * (SAYS_AT + 12)])

    assert said == [], "the stretch was measured across the restart"


def test_the_bundled_ffmpeg_reports_the_levels_the_recorder_reads(caplog) -> None:
    """The filter chain, the line format and the judgement, against the build
    that runs them, with both writers on the one pipe.

    Synthetic sound: a silent source, which is what a gated microphone and a
    soundless HDMI feed both hand over, and a sine, which is not. No devices,
    and nothing written to disk.
    """
    def lines(source: str) -> list[str]:
        result = subprocess.run(
            [
                str(ffmpeg_path()), "-hide_banner", "-loglevel", "warning",
                "-f", "lavfi", "-i", source, "-t", "40", "-c:a", "aac",
                *sound_stats_args(), *sound_level_args(48000),
                "-f", "null", "NUL",
            ],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr[-800:]
        return result.stdout.splitlines()

    silent = lines("anullsrc=r=48000:cl=stereo")
    sine = lines("sine=frequency=440:sample_rate=48000")
    # Both of ffmpeg's writers on the one pipe, and every line of both whole.
    for printed in (silent, sine):
        assert any(line.startswith("wer-sound ") for line in printed)
        assert any(line.startswith(f"{SOUND_LEVEL_KEY}=") for line in printed)
    assert any(line == f"{SOUND_LEVEL_KEY}=-inf" for line in silent), (
        "exact zeros did not come out as -inf"
    )

    recorder, said = listening_recorder()
    with caplog.at_level("WARNING", logger="wer.video.recorder"):
        feed(recorder, silent)
    assert len(said) == 1, said
    assert said[0].startswith("No sound from"), said[0]
    assert not [
        record for record in caplog.records
        if "sound statistics" in record.getMessage()
    ], "ffmpeg's own lines were called unreadable"

    recorder, said = listening_recorder()
    feed(recorder, sine)
    assert said == [], said
    # The delivery check reads the same on both, which is the fault it cannot
    # see and this one can.
    assert recorder._sound.delivered / recorder._sound.timeline == pytest.approx(
        1.0, abs=0.01
    )


def prints_sound_with_gaps(path: Path, seconds: float = 80.0) -> str:
    """Takes frames like a healthy ffmpeg, and reports sound arriving in gaps.

    It leaves a file big enough for the recorder to take on trust, prints the
    statistics a second of sound at a time, and drains the pipe afterwards.
    Paced, at about twenty times real time, because a burst of them all at once
    would be over while the take was still starting -- which is not how an
    input delivers sound, and would leave the test proving something else. And
    eighty seconds of it, four of wall time, because start() is still waiting
    for two of those: no frame reaches this stand-in until its statistics are
    all out, so start() announces the take only at START_READY_WAIT, and forty
    seconds' worth was over by then.
    """
    from wer.video.recorder import PROBE_BELOW_BYTES

    return (
        "import sys, collections, time\n"
        f"open({str(path)!r}, 'wb').write(bytes({PROBE_BELOW_BYTES + 1}))\n"
        "pts = 0\n"
        f"while pts < {int(48000 * seconds)}:\n"
        "    if (pts / 48000) % 0.1 < 0.05:\n"
        "        sys.stdout.write(f'wer-sound tb=1/48000 pts={pts} samp=1024\\n')\n"
        "    if pts % 48000 < 1024:\n"
        "        sys.stdout.flush()\n"
        "        time.sleep(0.05)\n"
        "    pts += 1024\n"
        "sys.stdout.flush()\n"
        "collections.deque(iter(lambda: sys.stdin.buffer.read(1 << 20), b''), maxlen=0)\n"
    )


def test_the_recorder_hears_about_the_sound_through_ffmpegs_own_pipe(
    tmp_path: Path, monkeypatch
) -> None:
    """End to end with a stand-in ffmpeg: the statistics go out on its stdout,
    the recorder reads them while the take runs, and the saved take says how
    much of the sound arrived. No audio device is opened; the name is only
    carried through."""
    from wer.video import recorder as recorder_module

    output = tmp_path / "gappy.mkv"
    monkeypatch.setattr(recorder_module, "_validate_audio_device", lambda name: None)
    monkeypatch.setattr(
        recorder_module, "build_ffmpeg_command",
        lambda *args, **kwargs: stand_in(prints_sound_with_gaps(output)),
    )
    said: list[str] = []
    recorder = Recorder(on_sound_warning=said.append)
    assert recorder.start(
        OutputSettings(audio_device="Microphone (USB Color Camera)"), output,
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    try:
        deadline = time.perf_counter() + 10.0
        for frame in frames(300):
            if said or time.perf_counter() > deadline:
                break
            recorder.submit(frame)
            time.sleep(0.02)
        assert said, "the sound statistics never reached the recorder"
    finally:
        recorder.stop()

    assert recorder.stats.sound_delivered_share == pytest.approx(0.5, abs=0.05)
    assert "of the sound arrived" in recorder.detail, recorder.detail


def _real_input_share(device: str, drop_half: bool, seconds: float = 8.0) -> float:
    """Record a real audio input and measure how much of its sound arrives.

    The samples over the time their timestamps cover, without the windowing --
    what is being asked here is whether the timestamps hold the gaps at all.
    Returns nan if ffmpeg fails or the input does not deliver in time, because
    a sick device must not hang a test run: this laptop has an input that
    answered at half its sound one minute and did not finish a 25-second
    capture in 85 the next.
    """
    command = [
        str(ffmpeg_path()), "-hide_banner", "-loglevel", "error",
        "-f", "dshow", "-audio_buffer_size", "50", "-rtbufsize", "64M",
        "-i", f"audio={device}",
    ]
    if drop_half:
        command += ["-af", r"aselect=lt(mod(t\,0.1)\,0.05)"]
    command += ["-t", f"{seconds:g}", "-c:a", "aac", *sound_stats_args(),
                "-f", "null", "NUL"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=seconds + 12,
        )
    except subprocess.TimeoutExpired:
        return float("nan")
    if result.returncode != 0:
        return float("nan")
    sound = 0.0
    first = last = None
    for line in result.stdout.splitlines():
        match = Recorder._SOUND_STATS.match(line.strip())
        assert match is not None, f"could not read {line!r}"
        num, den, pts, samples = (int(value) for value in match.groups())
        start, duration = pts * num / den, samples * num / den
        sound += duration
        first = start if first is None else first
        last = start + duration
    if first is None or last is None or last - first <= 0:
        return float("nan")
    return sound / (last - first)


def test_sound_missing_from_a_real_input_leaves_holes_in_its_timestamps() -> None:
    """The premise the whole check rests on, on a real capture rather than lavfi.

    Sound missing at the input has to leave its time in the timestamps with no
    samples in it. If a DirectShow capture instead closed the timeline up
    behind what it lost -- handing over a shorter run of samples with no hole
    -- the samples would match their span, and the check would read 100% over
    a take missing half its sound, which is the very failure it exists to
    catch. So a healthy input is recorded twice, as it comes and with half of
    every tenth of a second selected away. Measured on this laptop's
    microphone array: 100.0% as it comes and 50.1% halved, against the 50.0%
    the USB webcam's microphone lost by itself.

    The input is the one named by WER_LIVE_AUDIO_INPUT, not the first listed:
    this laptop's first one delivers about half its sound, and is the USB Color
    Camera's microphone, which a test run is not to open.
    """
    device = _live_audio_input()
    whole = _real_input_share(device, drop_half=False)
    if not whole > SOUND_SHORT_BELOW:
        pytest.skip(f"{device} does not deliver its sound whole ({whole:.1%})")

    halved = _real_input_share(device, drop_half=True)
    if halved != halved:                         # nan: the input gave up
        pytest.skip(f"{device} stopped delivering part way through")
    assert halved < 0.6, (
        f"{device} read {halved:.1%} with half its sound dropped, so the "
        f"gaps are not in its timestamps"
    )


def test_the_bundled_ffmpeg_prints_the_statistics_the_recorder_reads(
    tmp_path: Path
) -> None:
    """The flags, the line format and the share, against the build that runs
    them. Synthetic sound: a sine, and the same sine with half of every tenth
    of a second selected away. No devices, and nothing written to disk."""
    from wer.video.encoder import sound_stats_args

    def statistics(chain: str) -> list[str]:
        result = subprocess.run(
            [
                str(ffmpeg_path()), "-hide_banner", "-loglevel", "warning",
                "-f", "lavfi", "-i", chain, "-t", "40", "-c:a", "aac",
                *sound_stats_args(), "-f", "null", "NUL",
            ],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr[-800:]
        return result.stdout.splitlines()

    sine = "sine=frequency=440:sample_rate=48000:samples_per_frame=480"
    whole = statistics(sine)
    gappy = statistics(rf"{sine},aselect=lt(mod(t\,0.1)\,0.05)")
    assert whole and gappy, "ffmpeg printed no sound statistics at all"

    recorder, said = listening_recorder()
    feed(recorder, whole)
    assert said == [], "sound that lost nothing was called short"
    assert recorder._sound.delivered / recorder._sound.timeline == pytest.approx(
        1.0, abs=0.01
    )

    recorder, said = listening_recorder()
    feed(recorder, gappy)
    assert len(said) == 1, said
    assert recorder._sound.delivered / recorder._sound.timeline == pytest.approx(
        0.53, abs=0.05
    )
