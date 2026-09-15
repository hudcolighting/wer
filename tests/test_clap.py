"""The clap test's measuring, on sound and pictures whose timing is known.

The research behind wer.video.clap timed synthetic claps; these tests pin the
behaviour that research chose -- per-channel timing, the regular series, the
offset rule -- and one real round trip through the vendored ffmpeg, where a
flash and a click were made at the same instant and the click was then
delayed by a known 100 ms.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np
import pytest

from wer.video import clap
from wer.video.clap import (
    AudioTrack,
    Clap,
    ClapTestError,
    FrameStrip,
    SilentTakeError,
    combine,
    corrected_offset,
    describe_lag,
    find_claps,
    frame_period_ms,
    peak_dbfs,
    picture_time,
    read_audio,
    read_frames,
    regular_series,
    suggest_contact,
)

SR = 48_000


def _burst(rng: np.random.Generator, amplitude: float, precursor: bool = True) -> np.ndarray:
    """A clap: a faint collision, then a sharp burst that dies within ~10 ms."""
    length = int(0.08 * SR)
    t = np.arange(length) / SR
    body = rng.standard_normal(length) * np.exp(-t / 0.004) * np.minimum(1.0, t / 0.0005)
    sound = amplitude * body
    if precursor:
        # The collision of the hands, about 30 dB down and 8 ms before the
        # clap proper; a noise-referenced detector would time the clap here.
        lead = int(0.008 * SR)
        faint = 0.03 * amplitude * rng.standard_normal(lead)
        sound = np.concatenate([faint, sound])
    return sound


def _track(
    onsets: list[float],
    *,
    seconds: float = 8.0,
    amplitude: float = 0.6,
    channels: int = 2,
    noise: float = 0.002,
    seed: int = 7,
    clip: bool = False,
    invert_second: bool = False,
    start: float = 0.0,
) -> AudioTrack:
    rng = np.random.default_rng(seed)
    mono = noise * rng.standard_normal(int(seconds * SR))
    for onset in onsets:
        sound = _burst(rng, amplitude)
        lead = int(0.008 * SR)
        first = int(round(onset * SR)) - lead
        mono[first : first + sound.size] += sound[: mono.size - first]
    if clip:
        mono = np.clip(mono, -1.0, 1.0)
    samples = np.vstack([mono * (-1.0 if (invert_second and index == 1) else 1.0) for index in range(channels)])
    return AudioTrack(samples=samples.astype(np.float32), sample_rate=SR, start=start)


ONSETS = [1.0, 2.013, 2.991, 4.006, 5.0, 5.994]


def test_each_clap_is_timed_to_its_sharp_start_not_the_faint_collision_before_it() -> None:
    found = find_claps(_track(ONSETS))
    assert len(found) == len(ONSETS), [c.time for c in found]
    for expected, got in zip(ONSETS, found):
        assert abs(got.time - expected) <= 0.002, (expected, got.time)
        assert got.impulsive and not got.weak and not got.clipped and not got.flam


def test_times_are_on_the_file_timeline_not_counted_from_sample_zero() -> None:
    """A positive offset starts the sound late in the file; sample 0 is not time 0."""
    found = find_claps(_track(ONSETS, start=0.25))
    assert abs(found[0].time - (ONSETS[0] + 0.25)) <= 0.002


def test_silence_and_claps_too_quiet_to_trust_find_nothing() -> None:
    assert find_claps(_track([])) == []
    assert find_claps(_track(ONSETS, amplitude=0.004)) == []


def test_a_track_of_exact_zeros_is_refused_before_any_clap_is_looked_for() -> None:
    """An input that delivered nothing is not a clap nobody could hear, and
    reporting it as "no claps were found" sends someone back on stage to clap
    louder at an input that is not listening."""
    silent = _track([], noise=0.0)
    assert peak_dbfs(silent) == -math.inf
    with pytest.raises(SilentTakeError, match="digital silence"):
        find_claps(silent)
    # The dialog's worker catches ClapTestError, so this must stay one.
    assert issubclass(SilentTakeError, ClapTestError)


def test_a_room_recorded_far_too_quietly_still_has_its_claps_found() -> None:
    """The line is at nothing delivered, not at a low level: a take 20 dB down
    is thin, not silent, and its claps are still there to be timed."""
    quiet = _track(ONSETS, amplitude=0.05, noise=0.0005)
    assert peak_dbfs(quiet) > clap.SILENCE_DBFS
    found = find_claps(quiet)
    assert len(found) == len(ONSETS)
    for expected, got in zip(ONSETS, found):
        assert abs(got.time - expected) <= 0.002, (expected, got.time)


def test_the_clap_test_and_the_recorder_call_silence_at_the_same_level() -> None:
    """Two judgements of one fault, so they must be the same judgement.

    The take that warned "only digital silence" mid-record is the very take
    somebody then puts through the clap test, and a clap test that accepted it
    -- or refused it at some other level, for some other reason -- would leave
    the booth with two different stories about one dead input.
    """
    from wer.video.recorder import SOUND_SILENT_BELOW_DBFS

    assert clap.SILENCE_DBFS == SOUND_SILENT_BELOW_DBFS


def test_a_clipped_clap_is_still_timed_and_says_it_clipped() -> None:
    found = find_claps(_track(ONSETS, amplitude=3.0, clip=True))
    assert len(found) == len(ONSETS)
    assert all(c.clipped for c in found)
    for expected, got in zip(ONSETS, found):
        assert abs(got.time - expected) <= 0.003


def test_a_channel_wired_out_of_polarity_does_not_cancel_the_claps() -> None:
    """Summed, the two channels of this track are silence."""
    found = find_claps(_track(ONSETS, invert_second=True))
    assert len(found) == len(ONSETS)


def test_the_series_keeps_the_rhythm_and_leaves_out_a_stray_sound() -> None:
    stray = 3.45
    found = find_claps(_track(sorted(ONSETS + [stray])))
    assert any(abs(c.time - stray) < 0.003 for c in found), "precondition: the stray is detected"
    series = regular_series(found)
    assert [round(c.time, 2) for c in series] == [round(t, 2) for t in ONSETS]


def test_too_few_claps_make_no_series() -> None:
    claps = [Clap(time=t, strength_db=40.0, channel=0) for t in (1.0, 2.0, 3.0)]
    assert regular_series(claps) == []


def test_flams_and_non_impulsive_sounds_are_not_matched() -> None:
    claps = [Clap(time=float(t), strength_db=40.0, channel=0) for t in range(1, 7)]
    claps[2] = Clap(time=3.0, strength_db=40.0, channel=0, impulsive=False)
    series = regular_series(claps)
    assert 3.0 not in [c.time for c in series]


# ------------------------------------------------------------------ combining


def test_an_outlier_is_set_aside_and_the_rest_are_averaged() -> None:
    lags = [61.0, 58.0, 64.0, 60.0, 59.0, 200.0]
    result = combine(lags, frame_period=33.3)
    assert result.set_aside == (5,)
    assert result.used == (0, 1, 2, 3, 4)
    assert abs(result.lag_ms - 60.4) < 0.01
    assert result.can_apply, result.reasons
    assert math.isclose(result.pick_uncertainty_ms, 33.3 / math.sqrt(60))


def test_too_few_claps_are_shown_but_not_offered_to_apply() -> None:
    result = combine([60.0, 62.0, 61.0], frame_period=33.3)
    assert not result.can_apply
    assert "at least 5" in result.reasons[0]


def test_claps_that_disagree_by_more_than_two_frames_are_not_offered() -> None:
    result = combine([0.0, 30.0, 45.0, 10.0, 49.0, 20.0], frame_period=16.7)
    assert not result.can_apply
    assert any("disagree" in reason for reason in result.reasons)


def test_nothing_confirmed_is_said_plainly() -> None:
    result = combine([], frame_period=33.3)
    assert not result.can_apply and math.isnan(result.lag_ms)
    assert describe_lag(result.lag_ms) == "The claps could not be measured."


@pytest.mark.parametrize(
    ("recorded", "lag", "expected"),
    [
        (0, 62.0, -62),       # sound late: bring the offset down, delaying the picture
        (100, -40.0, 140),    # sound early: raise it, delaying the sound more
        (-62, 0.4, -62),      # already right: leave it
        (0, -2500.0, 2000),   # clamped to the Recording tab's range
        (0, 2500.0, -2000),
    ],
)
def test_the_offset_to_apply_cancels_what_the_take_measured(recorded, lag, expected) -> None:
    assert corrected_offset(recorded, lag) == expected


def test_the_lag_is_said_in_words() -> None:
    assert describe_lag(62.4) == "The sound arrives 62 ms after the picture."
    assert describe_lag(-40.0) == "The sound arrives 40 ms before the picture."
    assert describe_lag(0.2) == "The sound and the picture line up."


# -------------------------------------------------------------------- picture


def _strip(change: list[float], duplicate: list[bool] | None = None, period: float = 1 / 30) -> FrameStrip:
    count = len(change)
    return FrameStrip(
        region=(0, 0, 4, 4),
        frames=np.zeros((count, 4, 4, 3), np.uint8),
        times=np.arange(count) * period,
        change=np.asarray(change, float),
        duplicate=np.asarray(duplicate or [False] * count),
    )


def test_the_cursor_goes_to_the_frame_where_the_movement_ends() -> None:
    #          0    1    2    3     4     5    6    7    8
    change = [0.0, 0.3, 0.2, 6.0, 12.0, 11.0, 0.4, 0.3, 0.2]
    assert suggest_contact(_strip(change), audio_time=5 / 30) == 5


def test_the_cursor_steps_back_to_the_first_frame_of_a_repeat() -> None:
    change = [0.0, 0.3, 6.0, 12.0, 0.1, 0.2, 0.3]
    duplicate = [False, False, False, False, True, False, False]
    strip = _strip([0.0, 0.3, 6.0, 12.0, 11.0, 0.2, 0.3], duplicate)
    assert suggest_contact(strip, audio_time=4 / 30) == 3
    assert picture_time(_strip(change, duplicate), 4) == pytest.approx(3 / 30)


def test_no_movement_near_the_sound_gives_no_cursor() -> None:
    assert suggest_contact(_strip([0.0, 0.3, 0.2, 0.3, 0.2, 0.3]), audio_time=0.1) is None


def test_the_frame_period_comes_from_the_frames_themselves() -> None:
    assert frame_period_ms([0.0, 0.0333, 0.0667, 0.1, 0.1333]) == pytest.approx(33.3, abs=0.1)
    with pytest.raises(ClapTestError):
        frame_period_ms([1.0])


# ------------------------------------------------ a real file, through ffmpeg


def _vendored_ffmpeg() -> Path:
    exe = clap.ffmpeg_path()
    if exe is None:
        pytest.skip("the vendored ffmpeg is not present")
    return exe


LAG_S = 0.100
BOX = (100, 80, 120, 80)


@pytest.fixture(scope="module")
def clap_take(tmp_path_factory) -> Path:
    """Six seconds: a white box flashes on at k+0.5 s and a click sounds at the
    same instants, and the click's input is delayed by 100 ms, so the file's
    sound arrives 100 ms after its picture."""
    exe = _vendored_ffmpeg()
    output = tmp_path_factory.mktemp("clap") / "take.mkv"
    x, y, w, h = BOX
    video = (
        f"color=c=black:s=320x240:r=30:d=6,"
        f"drawbox=x={x}:y={y}:w={w}:h={h}:color=white:t=fill:"
        r"enable='between(mod(t\,1)\,0.5\,0.8)'"
    )
    audio = r"aevalsrc='0.8*sin(2*PI*2000*t)*between(mod(t\,1)\,0.5\,0.504)':s=48000:d=6"
    command = [
        str(exe), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", video,
        "-itsoffset", f"{LAG_S:.3f}", "-f", "lavfi", "-i", audio,
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", str(output),
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=120)
    return output


def test_a_real_take_measures_the_lag_it_was_made_with(clap_take: Path) -> None:
    track = read_audio(clap_take)
    assert track.sample_rate == 48_000
    assert track.start == pytest.approx(LAG_S, abs=0.002), "the delayed sound starts late in the file"
    assert track.gaps == 0

    series = regular_series(find_claps(track))
    assert len(series) >= 5, [c.time for c in find_claps(track)]

    lags = []
    period = None
    for heard in series:
        strip = read_frames(clap_take, start=heard.time - 0.4, duration=0.8, region=BOX)
        period = frame_period_ms(strip.times)
        # What a person does: the first frame in which the box is lit.
        lit = int(np.flatnonzero(strip.frames.reshape(strip.frames.shape[0], -1).mean(axis=1) > 128)[0])
        lags.append((heard.time - picture_time(strip, lit)) * 1000)
    assert period == pytest.approx(33.3, abs=0.5)

    result = combine(lags, frame_period=period)
    assert result.can_apply, result.reasons
    assert result.lag_ms == pytest.approx(LAG_S * 1000, abs=5.0), lags
    assert corrected_offset(0, result.lag_ms) == pytest.approx(-100, abs=5)


def test_the_suggested_frame_on_a_real_take_is_the_flash(clap_take: Path) -> None:
    track = read_audio(clap_take)
    heard = regular_series(find_claps(track))[0]
    strip = read_frames(clap_take, start=heard.time - 0.4, duration=0.8, region=BOX)
    suggested = suggest_contact(strip, heard.time)
    assert suggested is not None
    assert picture_time(strip, suggested) == pytest.approx(heard.time - LAG_S, abs=0.02)


def test_a_take_with_no_sound_says_so(tmp_path) -> None:
    exe = _vendored_ffmpeg()
    silent = tmp_path / "silent.mkv"
    subprocess.run(
        [str(exe), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=black:s=64x48:r=30:d=1", "-c:v", "libx264", "-preset", "ultrafast", str(silent)],
        check=True, capture_output=True, timeout=60,
    )
    with pytest.raises(ClapTestError, match="no sound"):
        read_audio(silent)
