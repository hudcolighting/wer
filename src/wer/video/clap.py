"""The clap test: how far a take's sound is from its picture, measured in the file.

No Qt. A clap test is a short take recorded through Wer's own path while
someone claps sharply on stage, in view of the camera. Nothing a device or an
API reports says how late a camera's picture or a capture card's sound reaches
the file -- DirectShow's IAMLatency describes buffering only, WASAPI describes
a stream Wer does not record through, and ffmpeg and OBS read neither
(research, 12 Sep 2026). So the take itself is measured: each clap is found in
the sound to about a millisecond, a person confirms the frame where the hands
meet, and the difference between the two, over several claps, is the offset.

"In sync" means matched to the stage (Hudson, 12 Sep 2026). The clap is made on
stage, so the travel time from the stage to a camera or booth microphone --
about 2.9 ms a metre -- is inside the measurement and is taken out along with
the equipment's own delay.

Both halves are read from the file as Wer wrote it, on one timeline:

- Sound is decoded with -copyts to float and timed as the first decoded
  timestamp plus the sample index. Sample 0 is not time 0: a positive offset
  starts the track late, and in MP4 the decoded stream starts early and still
  carries the AAC encoder's priming.
- Picture times are showinfo's pts, again with -copyts and with
  -fps_mode passthrough, so no frame is duplicated or dropped on the way out.
  OpenCV's CAP_PROP_POS_MSEC read 21 ms late in H.264+AAC Matroska files and
  is not used.

Precision is set by the picture: one frame is 33 ms at 30 fps and 67 ms at
15 fps, and a person's pick of the contact frame spreads over about a frame
divided by the square root of 12. Averaging several claps narrows that; the
sound is timed far more finely and is not the limit.
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from wer.paths import ffmpeg_path

log = logging.getLogger(__name__)

__all__ = [
    "AudioTrack",
    "Clap",
    "ClapTestError",
    "FrameStrip",
    "Measurement",
    "SilentTakeError",
    "combine",
    "corrected_offset",
    "describe_lag",
    "find_claps",
    "frame_period_ms",
    "peak_dbfs",
    "picture_time",
    "read_audio",
    "read_frames",
    "read_still",
    "regular_series",
    "suggest_contact",
]

# ---------------------------------------------------------------- the sound
#
# Every number here comes from the research of 12 Sep 2026, which timed
# synthetic claps -- a collision precursor, a room, noise, clipping -- through
# ffmpeg's own AAC encoder at Wer's settings. None of it has yet been checked
# against claps recorded in a real theatre; the first owner clap tests are that
# check.

#: A track whose loudest sample is below this is digital silence rather than
#: quiet sound, and is refused before any clap is looked for. -80 dBFS is about
#: three counts of a 16-bit sample; a room recorded far too quietly still sits
#: tens of dB above it, so nothing that carries a clap at all is caught here.
#: The failure this is for is an input delivering nothing -- a laptop microphone
#: array gating everything that is not speech, or a capture device whose HDMI
#: carries no audio -- which the clap search would otherwise report as "no claps
#: were found", sending someone away to clap louder at an input that is not
#: listening (the laptop's Realtek array, 13 Sep 2026).
SILENCE_DBFS = -80.0

#: A candidate must rise this far above the quietest moment of the 5-25 ms
#: before it. Measured against the room simulation: 29-30 of 30 claps found
#: whenever the clap's direct sound was 25 dB or more above the noise.
CLAP_RISE_DB = 12.0
#: ...and stand this far above the room's floor, taken as the 20th percentile
#: of the second before. Not a median: reverberation tails from claps a second
#: apart lift a median until the next clap no longer clears it (1 of 30 found
#: at 2.5 s reverberation, against 29 of 30 with the percentile).
CLAP_FLOOR_MARGIN_DB = 12.0
#: Below this much above the floor a clap is still used, but marked weak.
WEAK_CLAP_DB = 20.0
#: Each clap is timed at the first moment its 1 ms envelope comes within this
#: of its own peak. Thresholds referred to the noise, or 20 dB down, caught the
#: faint sound of the hands colliding up to 10 ms early; picking the peak
#: itself landed 13-25 ms late at 20 m in a live room.
ONSET_BELOW_PEAK_DB = 12.0
#: Two candidates closer than this are one clap.
REFRACTORY_S = 0.2
#: A clap with another sound this close is marked as a flam.
FLAM_S = 0.15
#: A clap is only impulsive if its onset is this close before its peak.
IMPULSIVE_S = 0.005

#: The shortest regular series of claps worth matching to the picture, and how
#: far each gap may stray from the series' own spacing. The tolerance is a
#: design choice: how evenly people clap was not found in any source read.
SERIES_MINIMUM = 4
SERIES_TOLERANCE = 0.25
SERIES_GAP_S = (0.5, 2.0)

# ---------------------------------------------------------------- the picture

#: A frame whose whole-picture change from the one before is below this share
#: of the strip's typical change is marked as a likely repeat. Encoded repeats
#: are not bit-identical (x264 mean difference 0.37 against 1.35 for genuine
#: frame pairs, and the two overlap), so this is a hint for the person picking,
#: to be calibrated on real footage, never a decision.
DUPLICATE_RATIO = 0.45
#: How far either side of a clap's sound the suggested frame is looked for.
SUGGEST_WINDOW_S = 0.4
#: The size the whole picture is shrunk to for measuring change outside the
#: marked region: enough to see a lighting cue, cheap to difference.
THUMB_SIZE = (160, 90)

# ---------------------------------------------------------------- combining

#: Claps whose lag is further than this many frame periods from the median are
#: set aside and shown, never silently dropped.
OUTLIER_FRAMES = 1.5
#: Fewer confirmed claps than this, or claps that disagree by more than this
#: many frame periods, and the result is shown but not offered to apply.
MIN_CLAPS_TO_APPLY = 5
MAX_SPREAD_FRAMES = 2.0
#: The Recording tab's A/V offset range.
OFFSET_LIMIT_MS = 2000

_FFMPEG_TIMEOUT_S = 120.0


class ClapTestError(Exception):
    """A clap test could not be read. The message is written for the booth."""


class SilentTakeError(ClapTestError):
    """The take's sound is digital silence, so there is no clap to find.

    A type of its own because the answer is a different one. Everything else
    the clap test refuses is answered by clapping again; this one is answered
    by recording from an input that delivers sound at all, and the dialog says
    so rather than asking for a louder clap.
    """


# =================================================================== sound


@dataclass(frozen=True)
class AudioTrack:
    """A take's sound, decoded to float, on the file's own timeline."""

    #: float32, shape (channels, samples).
    samples: np.ndarray
    sample_rate: int
    #: File time of the first sample, in seconds.
    start: float
    #: Places where the decoded timestamps jump by more than a millisecond
    #: between frames, and the largest jump in seconds. DirectShow puts such
    #: holes in routinely -- 3,021 in an hour on the UltraStudio with every
    #: sample present -- so they are reported, not refused.
    gaps: int = 0
    largest_gap: float = 0.0

    @property
    def duration(self) -> float:
        return self.samples.shape[1] / self.sample_rate

    def time_of(self, index: float) -> float:
        """File time, in seconds, of a (fractional) sample index."""
        return self.start + index / self.sample_rate


@dataclass(frozen=True)
class Clap:
    """One clap found in the sound."""

    #: File time of the clap's onset, in seconds.
    time: float
    #: How far the clap stood above the room's floor, in dB.
    strength_db: float
    #: The channel it was timed on: the one where it stood out most. Channels
    #: are never summed, because one can be wired out of polarity.
    channel: int
    weak: bool = False
    clipped: bool = False
    impulsive: bool = True
    flam: bool = False


def read_audio(
    video: Path, *, ffmpeg: Path | None = None, timeout: float = _FFMPEG_TIMEOUT_S
) -> AudioTrack:
    """Decode a take's first sound track, timed as the file times it."""
    exe = _ffmpeg(ffmpeg)
    with tempfile.TemporaryDirectory(prefix="wer-clap-") as scratch:
        frames = Path(scratch) / "frames.txt"
        command = [
            str(exe), "-hide_banner", "-nostdin", "-loglevel", "error",
            "-copyts", "-i", str(video),
            "-map", "0:a:0", "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1",
            # The same decode again, as one line per decoded frame: its pts,
            # duration and size. That is where the first timestamp, the channel
            # count and any holes come from.
            "-map", "0:a:0", "-c:a", "pcm_f32le", "-f", "framecrc", str(frames),
        ]
        result = _run(command, timeout)
        if result.returncode != 0:
            raise ClapTestError(_explain_failure(result.stderr, video, "sound"))
        listing = frames.read_text(encoding="utf-8", errors="replace")
    return _build_track(result.stdout, listing)


def _build_track(pcm: bytes, listing: str) -> AudioTrack:
    time_base = None
    rows: list[tuple[int, int, int]] = []
    for line in listing.splitlines():
        if line.startswith("#tb 0:"):
            num, den = line.split(":", 1)[1].strip().split("/")
            time_base = (int(num), int(den))
        elif line[:1].isdigit():
            fields = [part.strip() for part in line.split(",")]
            # stream, dts, pts, duration, size, checksum
            rows.append((int(fields[2]), int(fields[3]), int(fields[4])))
    if time_base is None or not rows:
        raise ClapTestError("The take has no sound to measure.")
    first_pts, first_duration, first_size = rows[0]
    if first_duration <= 0:
        raise ClapTestError("The take's sound could not be read: its first frame is empty.")
    num, den = time_base
    # For PCM the duration is counted in samples when the time base is one
    # sample, which is how ffmpeg decodes audio.
    sample_rate = round(den / num)
    channels = first_size // (4 * first_duration)
    if channels < 1:
        raise ClapTestError("The take's sound could not be read: no channels.")
    samples = np.frombuffer(pcm, dtype="<f4")
    usable = samples.size - samples.size % channels
    samples = samples[:usable].reshape(-1, channels).T

    gaps = 0
    largest = 0.0
    for (pts, duration, _size), (next_pts, _d, _s) in zip(rows, rows[1:]):
        jump = (next_pts - (pts + duration)) * num / den
        if abs(jump) > 0.001:
            gaps += 1
            largest = max(largest, abs(jump))
    if gaps:
        log.info(
            "Clap test sound has %d timestamp jump(s) over 1 ms, largest %.1f ms; "
            "timed by sample count from the first timestamp", gaps, largest * 1000,
        )
    return AudioTrack(
        samples=samples,
        sample_rate=sample_rate,
        start=first_pts * num / den,
        gaps=gaps,
        largest_gap=largest,
    )


def peak_dbfs(track: AudioTrack) -> float:
    """The loudest sample in the whole track, in dBFS.

    -inf for a track of exact zeros. The samples are already in memory, so this
    reads them where they are: np.abs would copy the whole track first, and a
    long take decoded to float is already the largest thing the clap test holds.
    """
    if track.samples.size == 0:
        return -math.inf
    peak = max(abs(float(track.samples.max())), abs(float(track.samples.min())))
    if peak <= 0.0:
        return -math.inf
    return 20.0 * math.log10(peak)


def find_claps(track: AudioTrack) -> list[Clap]:
    """Every clap-like sound in the track, earliest first.

    Raises SilentTakeError if the whole track is below SILENCE_DBFS: an input
    that delivered nothing is a different fault from a clap nobody could hear,
    and it is settled before any searching.

    Candidates are found on a 1 ms grid of 2 ms energy of the first-differenced
    signal, which favours the sharp attack of a clap over rumble and hum. Each
    is then timed on the channel where it stood out most, at the first moment
    its 1 ms envelope comes within ONSET_BELOW_PEAK_DB of its peak.
    """
    # Not "peak": the candidate loop below uses that name for a sample index.
    loudest = peak_dbfs(track)
    if loudest < SILENCE_DBFS:
        measured = (
            "exact zeros" if math.isinf(loudest) else f"peak {loudest:.0f} dBFS"
        )
        raise SilentTakeError(
            f"The take's sound is digital silence ({measured}). The audio input "
            "delivered nothing to record, so there is no clap in the take to find."
        )
    sr = track.sample_rate
    hop = max(1, sr // 1000)
    found: list[tuple[int, int, float, float]] = []  # sample, channel, above floor, peak
    for channel in range(track.samples.shape[0]):
        signal = track.samples[channel].astype(np.float64)
        if signal.size < 60 * hop:
            continue
        diff = np.diff(signal, prepend=signal[:1])
        energy = _energy_db(diff, hop, 2 * hop)
        floor = _trailing_floor(energy, window=1000, stride=50, percentile=20.0)
        for peak in _candidates(energy, floor):
            found.append((peak * hop + hop, channel, float(energy[peak] - floor[peak]), float(energy[peak])))
    if not found:
        return []

    found.sort()
    merged: list[tuple[int, int, float, float]] = []
    for candidate in found:
        if merged and candidate[0] - merged[-1][0] <= 5 * hop:
            if candidate[2] > merged[-1][2]:
                merged[-1] = candidate
        else:
            merged.append(candidate)

    claps: list[Clap] = []
    for coarse, channel, above_floor, _peak_db in merged:
        signal = track.samples[channel].astype(np.float64)
        diff = np.diff(signal, prepend=signal[:1])
        onset, peak = _onset(diff, coarse, sr)
        claps.append(
            Clap(
                time=track.time_of(onset),
                strength_db=above_floor,
                channel=channel,
                weak=above_floor < WEAK_CLAP_DB,
                clipped=_clipped(signal, onset, sr),
                impulsive=(peak - onset) <= IMPULSIVE_S * sr,
            )
        )
    for index, clap in enumerate(claps):
        before = claps[index - 1].time if index else -math.inf
        after = claps[index + 1].time if index + 1 < len(claps) else math.inf
        if min(clap.time - before, after - clap.time) < FLAM_S:
            claps[index] = Clap(**{**clap.__dict__, "flam": True})
    return claps


def _energy_db(diff: np.ndarray, hop: int, width: int) -> np.ndarray:
    squared = np.concatenate(([0.0], np.cumsum(diff * diff)))
    starts = np.arange(0, diff.size - width + 1, hop)
    mean = (squared[starts + width] - squared[starts]) / width
    return 10.0 * np.log10(np.maximum(mean, 1e-20))


def _trailing_floor(energy: np.ndarray, *, window: int, stride: int, percentile: float) -> np.ndarray:
    """The room's floor under each grid point, from the second before it.

    Evaluated every ``stride`` points and held in between, which is plenty for a
    floor and keeps a 30 s take to a few hundred percentile calls.
    """
    floor = np.full(energy.size, np.inf)
    for point in range(stride, energy.size + stride, stride):
        history = energy[max(0, point - window) : min(point, energy.size)]
        if history.size < 25:
            continue
        floor[min(point, energy.size - 1) : point + stride] = np.percentile(history, percentile)
    return floor


def _candidates(energy: np.ndarray, floor: np.ndarray) -> list[int]:
    if energy.size < 50:
        return []
    lows = sliding_window_view(energy, 20).min(axis=1)  # lows[j] = min energy[j:j+20]
    index = np.arange(25, energy.size)
    rise = energy[index] - lows[index - 25]
    trigger = index[(rise >= CLAP_RISE_DB) & (energy[index] - floor[index] >= CLAP_FLOOR_MARGIN_DB)]
    peaks: list[int] = []
    allowed = 0
    refractory = int(REFRACTORY_S * 1000)
    for start in trigger:
        if start < allowed:
            continue
        end = min(energy.size, start + 20)
        peak = int(start + np.argmax(energy[start:end]))
        peaks.append(peak)
        allowed = peak + refractory
    return peaks


def _onset(diff: np.ndarray, coarse: int, sr: int) -> tuple[float, int]:
    """Sample index of a clap's onset, and of its envelope peak."""
    width = max(1, sr // 1000)
    first = max(0, coarse - int(0.060 * sr))
    last = min(diff.size, coarse + int(0.030 * sr))
    segment = diff[first:last]
    envelope = np.convolve(segment * segment, np.ones(width) / width, mode="same")
    centre = coarse - first
    lo = max(0, centre - int(0.015 * sr))
    hi = min(envelope.size, centre + int(0.015 * sr) + 1)
    peak = lo + int(np.argmax(envelope[lo:hi]))
    threshold = envelope[peak] * 10.0 ** (-ONSET_BELOW_PEAK_DB / 10.0)
    search = max(0, peak - int(0.040 * sr))
    crossing = search + int(np.flatnonzero(envelope[search : peak + 1] >= threshold)[0])
    onset = float(crossing)
    if crossing > 0 and envelope[crossing] > envelope[crossing - 1]:
        fraction = (threshold - envelope[crossing - 1]) / (envelope[crossing] - envelope[crossing - 1])
        onset = crossing - 1 + min(max(fraction, 0.0), 1.0)
    # A centred window shows energy half a window before it arrives. That much
    # is corrected; the rest of the small early bias measured through AAC
    # (0.5-1.7 ms) is left, being far inside a frame.
    return first + onset + width / 2, first + peak


def _clipped(signal: np.ndarray, onset: float, sr: int) -> bool:
    first = max(0, int(onset - 0.002 * sr))
    last = min(signal.size, int(onset + 0.020 * sr))
    hot = (np.abs(signal[first:last]) >= 0.99).astype(np.int8)
    return hot.size >= 3 and int(np.convolve(hot, np.ones(3, np.int8), mode="valid").max()) >= 3


def regular_series(
    claps: Sequence[Clap],
    *,
    minimum: int = SERIES_MINIMUM,
    tolerance: float = SERIES_TOLERANCE,
    gap_range: tuple[float, float] = SERIES_GAP_S,
) -> list[Clap]:
    """The longest evenly spaced run of claps, or nothing if none is long enough.

    A door, a dropped prop or a cough passes the rise test too. What they rarely
    do is fall into the rhythm of someone clapping, so the claps offered for
    matching to the picture are the ones that do; the rest are shown greyed out.
    Every pair of claps is tried as the spacing, and the series then follows it
    forward and back, taking at each step the clap nearest where the next one
    should be.
    """
    usable = [clap for clap in claps if clap.impulsive and not clap.flam]
    times = [clap.time for clap in usable]
    best: list[int] = []
    shortest, longest = gap_range
    for first in range(len(usable)):
        for second in range(first + 1, len(usable)):
            spacing = times[second] - times[first]
            if spacing > longest:
                break
            if spacing < shortest:
                continue
            chosen = _follow(times, first, spacing, tolerance)
            if len(chosen) > len(best):
                best = chosen
    if len(best) < minimum:
        return []
    return [usable[index] for index in best]


def _follow(times: list[float], seed: int, spacing: float, tolerance: float) -> list[int]:
    chosen = [seed]
    for direction in (1, -1):
        last = seed
        while True:
            expected = times[last] + direction * spacing
            nearest = min(
                (index for index in range(len(times))
                 if abs(times[index] - expected) <= tolerance * spacing
                 and (index > last if direction > 0 else index < last)),
                key=lambda index: abs(times[index] - expected),
                default=None,
            )
            if nearest is None:
                break
            chosen.append(nearest)
            last = nearest
    return sorted(set(chosen))


# ================================================================= picture


@dataclass(frozen=True)
class FrameStrip:
    """The frames of a stretch of the take, cropped to the marked region."""

    #: x, y, width, height of the region, in the take's own pixels.
    region: tuple[int, int, int, int]
    #: uint8 BGR, shape (frames, height, width, 3).
    frames: np.ndarray
    #: File time of each frame, in seconds.
    times: np.ndarray
    #: How much the region changed into each frame, with the change in the rest
    #: of the picture (a lighting cue, codec noise) taken off. 0 for the first.
    change: np.ndarray
    #: Frames that look like a repeat of the one before. A hint; see
    #: DUPLICATE_RATIO.
    duplicate: np.ndarray
    #: The take's own frame size.
    size: tuple[int, int] = field(default=(0, 0))


def read_still(
    video: Path, time: float, *, ffmpeg: Path | None = None, timeout: float = _FFMPEG_TIMEOUT_S
) -> tuple[np.ndarray, float]:
    """The whole frame showing at ``time``, and that frame's own file time.

    For marking the region around the clapper's hands.
    """
    exe = _ffmpeg(ffmpeg)
    command = [
        str(exe), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
        "-copyts", "-ss", f"{max(0.0, time):.3f}", "-i", str(video),
        "-map", "0:v:0", "-frames:v", "1", "-vf", "showinfo,format=bgr24",
        "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1",
    ]
    result = _run(command, timeout)
    if result.returncode != 0:
        raise ClapTestError(_explain_failure(result.stderr, video, "picture"))
    times, sizes = _parse_showinfo(result.stderr)
    if not times or not sizes:
        raise ClapTestError("The take has no picture at that point.")
    width, height = sizes[0]
    if len(result.stdout) < width * height * 3:
        raise ClapTestError("The take's picture could not be read.")
    image = np.frombuffer(result.stdout[: width * height * 3], np.uint8).reshape(height, width, 3)
    return image.copy(), times[0]


def read_frames(
    video: Path,
    *,
    start: float,
    duration: float,
    region: tuple[int, int, int, int],
    ffmpeg: Path | None = None,
    timeout: float = _FFMPEG_TIMEOUT_S,
) -> FrameStrip:
    """Every frame from ``start`` for ``duration`` seconds, cropped to ``region``.

    Cropped at full resolution, because a clapper's hands on a stage 20 m away
    are a few dozen pixels of a 1080p picture and would not survive shrinking.
    The whole picture is also read, shrunk to THUMB_SIZE, only to measure the
    change outside the region and to spot repeated frames.
    """
    exe = _ffmpeg(ffmpeg)
    x, y, width, height = (int(value) for value in region)
    if width < 2 or height < 2:
        raise ClapTestError("Mark a larger area around the clapper's hands.")
    thumb_w, thumb_h = THUMB_SIZE
    graph = (
        "[0:v:0]showinfo,split=2[r][t];"
        f"[r]format=bgr24,crop={width}:{height}:{x}:{y}[roi];"
        f"[t]scale={thumb_w}:{thumb_h},format=gray[thumb]"
    )
    with tempfile.TemporaryDirectory(prefix="wer-clap-") as scratch:
        thumbs_path = Path(scratch) / "thumbs.raw"
        command = [
            str(exe), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
            "-copyts", "-ss", f"{max(0.0, start):.3f}", "-t", f"{duration:.3f}",
            "-i", str(video),
            "-filter_complex", graph,
            "-map", "[roi]", "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1",
            "-map", "[thumb]", "-fps_mode", "passthrough", "-f", "rawvideo", str(thumbs_path),
        ]
        result = _run(command, timeout)
        if result.returncode != 0:
            raise ClapTestError(_explain_failure(result.stderr, video, "picture"))
        thumbs_raw = thumbs_path.read_bytes() if thumbs_path.is_file() else b""
    times, sizes = _parse_showinfo(result.stderr)
    per_frame = width * height * 3
    count = min(len(times), len(result.stdout) // per_frame, len(thumbs_raw) // (thumb_w * thumb_h))
    if count == 0:
        raise ClapTestError("No frames were found in that part of the take.")
    frames = np.frombuffer(result.stdout[: count * per_frame], np.uint8).reshape(count, height, width, 3)
    thumbs = np.frombuffer(thumbs_raw[: count * thumb_w * thumb_h], np.uint8).reshape(count, thumb_h, thumb_w)
    size = sizes[0] if sizes else (0, 0)
    change, duplicate = _measure_change(frames, thumbs, (x, y, width, height), size)
    return FrameStrip(
        region=(x, y, width, height),
        frames=frames.copy(),
        times=np.asarray(times[:count], dtype=np.float64),
        change=change,
        duplicate=duplicate,
        size=size,
    )


_PTS_TIME = re.compile(r"pts_time:\s*(-?[\d.]+)")
_SIZE = re.compile(r"\bs:(\d+)x(\d+)")


def _parse_showinfo(stderr: bytes) -> tuple[list[float], list[tuple[int, int]]]:
    times: list[float] = []
    sizes: list[tuple[int, int]] = []
    for line in stderr.decode("utf-8", errors="replace").splitlines():
        if "Parsed_showinfo" not in line:
            continue
        match = _PTS_TIME.search(line)
        if not match:
            continue
        times.append(float(match.group(1)))
        size = _SIZE.search(line)
        if size:
            sizes.append((int(size.group(1)), int(size.group(2))))
    return times, sizes


def _measure_change(
    frames: np.ndarray, thumbs: np.ndarray, region: tuple[int, int, int, int], size: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    count = frames.shape[0]
    grey = [
        cv2.blur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32), (3, 3))
        for frame in frames
    ]
    inside = np.zeros(count, np.float64)
    for index in range(1, count):
        inside[index] = float(np.mean(np.abs(grey[index] - grey[index - 1])))

    small = thumbs.astype(np.float32)
    mask = np.ones(small.shape[1:], bool)
    width, height = size
    if width and height:
        x, y, w, h = region
        sx, sy = small.shape[2] / width, small.shape[1] / height
        mask[int(y * sy) : int(math.ceil((y + h) * sy)), int(x * sx) : int(math.ceil((x + w) * sx))] = False
    outside = np.zeros(count, np.float64)
    whole = np.zeros(count, np.float64)
    for index in range(1, count):
        delta = np.abs(small[index] - small[index - 1])
        whole[index] = float(delta.mean())
        if mask.any():
            outside[index] = float(np.median(delta[mask]))
    change = np.maximum(0.0, inside - outside)

    duplicate = np.zeros(count, bool)
    typical = float(np.median(whole[1:])) if count > 1 else 0.0
    if typical > 0:
        duplicate[1:] = whole[1:] < DUPLICATE_RATIO * typical
    return change, duplicate


def suggest_contact(strip: FrameStrip, audio_time: float) -> int | None:
    """A frame to put the cursor on for the clap heard at ``audio_time``.

    Only a starting point for the person picking. Measured in simulation, no
    automatic reading of the picture was trustworthy on its own: the quietest
    frame after the burst of movement was off by -84 to +198 ms. The cursor goes
    to the last frame of the biggest burst of movement near the sound -- the
    change into a frame is movement that ended there, and hands that have just
    met stop moving -- stepped back to the first frame of any repeat.
    """
    count = strip.times.size
    if count < 3:
        return None
    near = np.flatnonzero(np.abs(strip.times - audio_time) <= SUGGEST_WINDOW_S)
    if near.size == 0:
        return None
    peak = int(near[np.argmax(strip.change[near])])
    background = float(np.median(strip.change[1:]))
    if strip.change[peak] <= max(3.0 * background, 0.5):
        return None
    end = peak
    while end + 1 < count and strip.change[end + 1] >= 0.4 * strip.change[peak]:
        end += 1
    return _run_start(strip, end)


def picture_time(strip: FrameStrip, index: int) -> float:
    """File time of a picked frame, taken from the first frame of its repeat run.

    A repeated frame shows the same picture later; the picture first appeared
    at the start of the run.
    """
    return float(strip.times[_run_start(strip, index)])


def _run_start(strip: FrameStrip, index: int) -> int:
    while index > 0 and strip.duplicate[index]:
        index -= 1
    return index


def frame_period_ms(times: Sequence[float] | np.ndarray) -> float:
    """The take's frame period, from the times of consecutive frames."""
    steps = np.diff(np.asarray(times, dtype=np.float64))
    steps = steps[steps > 0]
    if steps.size == 0:
        raise ClapTestError("The take's frame rate could not be worked out.")
    return float(np.median(steps) * 1000.0)


# ================================================================ combining


@dataclass(frozen=True)
class Measurement:
    """What the confirmed claps say, and whether it is good enough to apply."""

    #: Sound time minus picture time for each confirmed clap, in ms. Positive
    #: means the sound arrives after the picture.
    lags_ms: tuple[float, ...]
    used: tuple[int, ...]
    set_aside: tuple[int, ...]
    #: Mean lag of the claps used, in ms.
    lag_ms: float
    median_ms: float
    #: How far apart the claps used are, in frame periods.
    spread_frames: float
    standard_error_ms: float
    #: The spread expected from picking whole frames alone: T / sqrt(12 N).
    pick_uncertainty_ms: float
    frame_period_ms: float
    can_apply: bool
    reasons: tuple[str, ...]


def combine(lags_ms: Sequence[float], frame_period: float) -> Measurement:
    """Combine per-clap lags into one measurement."""
    values = np.asarray(lags_ms, dtype=np.float64)
    if values.size == 0:
        return Measurement((), (), (), math.nan, math.nan, 0.0, math.nan, math.nan,
                           frame_period, False, ("No claps were confirmed.",))
    median = float(np.median(values))
    keep = np.abs(values - median) <= OUTLIER_FRAMES * frame_period
    kept = values[keep]
    used = tuple(int(index) for index in np.flatnonzero(keep))
    set_aside = tuple(int(index) for index in np.flatnonzero(~keep))
    count = kept.size
    mean = float(kept.mean())
    standard_error = float(kept.std(ddof=1) / math.sqrt(count)) if count > 1 else math.nan
    spread = float((kept.max() - kept.min()) / frame_period) if count else 0.0
    reasons: list[str] = []
    if count < MIN_CLAPS_TO_APPLY:
        reasons.append(
            f"Only {count} clap{'s' if count != 1 else ''} could be used; "
            f"at least {MIN_CLAPS_TO_APPLY} are needed."
        )
    if spread > MAX_SPREAD_FRAMES:
        reasons.append(
            f"The claps disagree by {spread:.1f} frames; more than "
            f"{MAX_SPREAD_FRAMES:.0f} means something is off. Check the picked frames."
        )
    return Measurement(
        lags_ms=tuple(float(value) for value in values),
        used=used,
        set_aside=set_aside,
        lag_ms=mean,
        median_ms=median,
        spread_frames=spread,
        standard_error_ms=standard_error,
        pick_uncertainty_ms=frame_period / math.sqrt(12 * count),
        frame_period_ms=frame_period,
        can_apply=not reasons,
        reasons=tuple(reasons),
    )


def corrected_offset(recorded_offset_ms: int, lag_ms: float) -> int:
    """The A/V offset that cancels ``lag_ms`` in a take recorded at ``recorded_offset_ms``.

    The offset a take was recorded with is already in the file's timestamps, so
    the lag measured is what is left over. A positive offset delays the sound,
    so sound arriving late (a positive lag) needs the offset brought down by
    that much.
    """
    value = round(recorded_offset_ms - lag_ms)
    return max(-OFFSET_LIMIT_MS, min(OFFSET_LIMIT_MS, value))


def describe_lag(lag_ms: float) -> str:
    """The lag in words, the way the booth should read it."""
    if math.isnan(lag_ms):
        return "The claps could not be measured."
    if abs(lag_ms) < 0.5:
        return "The sound and the picture line up."
    if lag_ms > 0:
        return f"The sound arrives {lag_ms:.0f} ms after the picture."
    return f"The sound arrives {-lag_ms:.0f} ms before the picture."


# ================================================================= plumbing


def _ffmpeg(explicit: Path | None) -> Path:
    exe = explicit or ffmpeg_path()
    if exe is None:
        raise ClapTestError(
            "ffmpeg was not found, so the clap test cannot read the take."
        )
    return exe


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise ClapTestError(
            f"Reading the take took longer than {timeout:.0f} s and was stopped."
        ) from exc
    except OSError as exc:
        raise ClapTestError(f"ffmpeg could not be started: {exc}") from exc


def _explain_failure(stderr: bytes, video: Path, what: str) -> str:
    text = stderr.decode("utf-8", errors="replace")
    if "matches no streams" in text:
        return f"The take has no {what} to measure."
    last = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")
    log.error("Clap test could not read the %s of %s: %s", what, video, text[-2000:])
    return f"The take's {what} could not be read: {last or 'ffmpeg failed'}."
