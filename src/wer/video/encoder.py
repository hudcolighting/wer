"""Output settings and ffmpeg command construction.

No Qt. Building the command line is a pure function, so it is tested by
inspecting arguments rather than by encoding video.

Output is deliberately decoupled from capture
---------------------------------------------
What the camera delivers and what gets written to disk are separate decisions.
A Blackmagic feeding 4K does not oblige us to *record* 4K: capture 4K and record
1080p, or record 4K at a lower quality, as the situation needs.

That matters for more than taste. Raw BGR frames are piped to ffmpeg, and raw
video is enormous:

    1920x1080 BGR24 @ 30 fps  =  6.2 MB/frame   ~187 MB/s
    3840x2160 BGR24 @ 30 fps  = 24.9 MB/frame   ~747 MB/s

747 MB/s through a Windows pipe, sustained for four hours, is not something to
take on trust. Scaling before the pipe is the difference between comfortable and
marginal, so the scale happens in our pipeline, not in ffmpeg's filter chain.

Quality flags are per-encoder
-----------------------------
`-crf` is an x264 concept. NVENC wants `-cq`, Quick Sync wants `-global_quality`,
AMF wants `-qp_i`/`-qp_p`. Passing `-crf` to `h264_nvenc` is silently ignored,
which produces a file at whatever default bitrate NVENC felt like - one of the
easier ways to record a four-hour tech at the wrong quality and only find out
afterwards.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from wer.paths import ffmpeg_path

log = logging.getLogger(__name__)

__all__ = [
    "AUTO_ENCODER",
    "resolve_encoder_name",
    "forget_detected_encoders",
    "remember_detected_encoders",
    "Container",
    "QualityPreset",
    "VideoEncoder",
    "OutputSettings",
    "QUALITY_PRESETS",
    "EncoderAvailability",
    "detect_encoders",
    "build_ffmpeg_command",
    "SOUND_STATS_FORMAT",
    "sound_stats_args",
    "SOUND_LEVEL_KEY",
    "sound_level_chain",
    "sound_level_args",
    "output_pix_fmt",
    "profile_args",
    "raw_bitrate_mbps",
]


class Container(str, Enum):
    #: Default. A crash or power loss four hours into a tech must not destroy
    #: the file, and MKV survives an incomplete write where MP4 does not.
    MKV = "mkv"
    MP4 = "mp4"


@dataclass(frozen=True, slots=True)
class VideoEncoder:
    """One H.264 encoder and how to ask it for a quality level."""

    name: str
    label: str
    #: The flag this encoder uses for constant-quality mode.
    quality_flag: str
    #: Sensible default for that flag. The scales are NOT comparable across
    #: encoders - NVENC's cq 23 is not x264's crf 23.
    quality_default: int
    quality_min: int
    quality_max: int
    #: Speed/efficiency preset flag values, slowest-best first.
    presets: tuple[str, ...]
    preset_default: str
    is_hardware: bool

    def quality_args(self, quality: int) -> list[str]:
        clamped = max(self.quality_min, min(self.quality_max, quality))
        if self.name == "h264_amf":
            # AMF has no single quality knob; it wants per-frame-type QP.
            return ["-rc", "cqp", "-qp_i", str(clamped), "-qp_p", str(clamped)]
        if self.name == "h264_mf":
            # Media Foundation ignores -quality unless it has been told that
            # quality, rather than a bitrate, is the target at all.
            return ["-rate_control", "quality", "-quality", str(clamped)]
        return [self.quality_flag, str(clamped)]

    def quality_for_offset(self, offset: int) -> int:
        """Put a quality preset's offset onto this encoder's own scale.

        Every encoder here except one counts *downwards* - a bigger CRF or QP
        is a worse picture. Media Foundation counts upwards on a 0-100 scale,
        and coarsely enough that one of its points is worth about half of a
        CRF point, so the offset is both negated and widened.
        """
        if self.name == "h264_mf":
            target = self.quality_default - offset * 2
        else:
            target = self.quality_default + offset
        return max(self.quality_min, min(self.quality_max, target))


#: Software first, deliberately. It is the one that works on every machine, and
#: the acceptance test is a strange laptop with nothing installed.
SOFTWARE_ENCODER = VideoEncoder(
    name="libx264",
    label="Software (x264)",
    quality_flag="-crf",
    quality_default=20,
    quality_min=0,
    quality_max=51,
    presets=("slow", "medium", "veryfast", "ultrafast"),
    preset_default="veryfast",
    is_hardware=False,
)

HARDWARE_ENCODERS = (
    # NVENC's modern preset names are p1 (fastest) through p7 (best quality).
    # The old slow/medium/fast names still parse but are deprecated aliases.
    VideoEncoder("h264_nvenc", "NVIDIA (NVENC)", "-cq", 23, 0, 51,
                 ("p7", "p5", "p4", "p2", "p1"), "p4", True),
    VideoEncoder("h264_qsv", "Intel Quick Sync", "-global_quality", 23, 1, 51,
                 ("veryslow", "medium", "veryfast"), "medium", True),
    VideoEncoder("h264_amf", "AMD (AMF)", "-qp_i", 23, 0, 51,
                 ("quality", "balanced", "speed"), "balanced", True),
    # The one that works when the vendor's own encoder will not.
    #
    # h264_nvenc has a compiled-in NVENC API version that the driver must be
    # new enough to serve. Our ffmpeg wants 13.1; a perfectly good RTX 5070 Ti
    # on driver 592.01 offers 13.0, and nvenc refuses outright -- so the
    # machine with the most encoding silicon in it fell back to the Intel iGPU.
    # Media Foundation is Windows asking the installed driver for whatever
    # hardware H.264 encoder it provides, with no version handshake to fail.
    # On that same laptop it is handed "NVIDIA H.264 Encoder MFT": the
    # dedicated card, through the ffmpeg we already ship, on the driver that
    # is already installed.
    #
    # It carries no -preset (there is no such option) and, left alone, encodes
    # Constrained Baseline; build_ffmpeg_command sets High explicitly.
    VideoEncoder("h264_mf", "GPU (Media Foundation)", "-quality", 70, 0, 100,
                 (), "", True),
)

ALL_ENCODERS = (SOFTWARE_ENCODER, *HARDWARE_ENCODERS)
#: The encoder setting meaning "whichever hardware encoder this machine has,
#: and software if it has none".
AUTO_ENCODER = "auto"


@dataclass(frozen=True, slots=True)
class QualityPreset:
    """A named quality level, in language a lighting designer can act on.

    The underlying number is exposed too - someone who knows what CRF means
    should not have to guess which preset maps to what.
    """

    key: str
    label: str
    description: str
    #: Offset applied to the encoder's default quality value. Positive is
    #: lower quality. Most encoders here count that way natively (a bigger CRF
    #: or QP is a worse picture); VideoEncoder.quality_for_offset turns this
    #: into whichever direction the encoder actually uses.
    quality_offset: int


QUALITY_PRESETS = (
    QualityPreset(
        "archive", "Archive",
        "Near-transparent. Largest files. For a reference recording you intend "
        "to keep or edit from.", -6),
    QualityPreset(
        "standard", "Standard",
        "Good quality at a sane size. The right default for documenting a tech.",
        0),
    QualityPreset(
        "compact", "Compact",
        "Visibly compressed on fine detail, but cue text stays legible. For long "
        "sessions or a nearly full disk.", +6),
    QualityPreset(
        "tiny", "Tiny",
        "Heavily compressed. For sending a run to a designer who is not in the "
        "room, over a connection that will not carry more.", +12),
)


@dataclass(frozen=True, slots=True)
class OutputSettings:
    """Everything about the recorded file, independent of the camera."""

    #: None means "whatever the camera is delivering". Setting these smaller
    #: than the capture size is the main lever for making 4K workable.
    width: int | None = None
    height: int | None = None
    fps: float | None = None

    encoder: str = AUTO_ENCODER
    quality: int | None = None          # None -> encoder default + preset offset
    quality_preset: str = "standard"
    speed_preset: str | None = None     # None -> encoder default

    container: Container = Container.MKV
    #: MP4 written with fragmented headers so an interrupted file is still
    #: playable. Ignored for MKV, which does not need it.
    fragmented_mp4: bool = True

    audio_device: str | None = None
    audio_bitrate_kbps: int = 192
    #: The sample format to ask the audio input for. None leaves the device on
    #: its own default -- which is what happens whenever the device has not
    #: been probed, and must stay the safe outcome. Only ever filled from a
    #: format the device actually listed; see wer.video.devices.
    audio_sample_rate: int | None = None
    audio_sample_bits: int | None = None
    audio_channels: int | None = None

    #: Positive delays audio relative to video; negative delays video. Chosen
    #: for the camera and audio input in use by wer.core.avsync -- typed by the
    #: operator, applied from a clap test, or the automatic value -- and fixed
    #: when a take is armed. It was manual only to begin with; Hudson changed
    #: that on 12 Sep 2026.
    av_offset_ms: int = 0

    #: Keyframe interval in frames. Two seconds' worth is the usual compromise
    #: between seek granularity and size.
    keyframe_interval: int | None = None

    def __post_init__(self) -> None:
        # Container subclasses str, and this is read from a show file and set
        # from a QComboBox. `is Container.MP4` would be False for "mp4".
        object.__setattr__(self, "container", Container(self.container))

    def resolve_encoder(self) -> VideoEncoder:
        wanted = resolve_encoder_name(self.encoder)
        for candidate in ALL_ENCODERS:
            if candidate.name == wanted:
                return candidate
        log.warning("Unknown encoder %r; falling back to software", self.encoder)
        return SOFTWARE_ENCODER

    def resolve_quality(self) -> int:
        """The quality number actually passed to the encoder."""
        encoder = self.resolve_encoder()
        if self.quality is not None:
            return self.quality
        offset = next(
            (p.quality_offset for p in QUALITY_PRESETS if p.key == self.quality_preset),
            0,
        )
        return encoder.quality_for_offset(offset)

    def output_size(self, capture_width: int, capture_height: int) -> tuple[int, int]:
        """Recorded size, given what the camera is delivering.

        Odd dimensions are rounded down to even: yuv420p chroma is subsampled
        by two, and libx264 refuses odd sizes outright.
        """
        width = self.width or capture_width
        height = self.height or capture_height
        return width - (width % 2), height - (height % 2)


def raw_bitrate_mbps(width: int, height: int, fps: float) -> float:
    """Megabytes per second of raw BGR24 down the pipe.

    Surfaced in the UI because it is the number that explains why 4K behaves
    differently from 1080p, and it is not obvious until someone shows you.
    """
    return (width * height * 3 * fps) / 1_000_000


@dataclass(frozen=True, slots=True)
class EncoderAvailability:
    """Whether an encoder works here, and if not, why not.

    The reason matters. When NVENC is missing because the driver is a version
    behind, "NVIDIA (NVENC) - driver too old" is a problem the user can fix in
    ten minutes; an encoder that has silently vanished from a dropdown is not.
    No silent failures.
    """

    encoder: VideoEncoder
    available: bool
    reason: str = ""
    #: The chip this actually reached, when the encoder can tell us. Only
    #: Media Foundation can - it is a broker for someone else's encoder, so
    #: "GPU (Media Foundation)" alone does not say whose GPU. Set when its test
    #: recording failed as well: an encoder that is there and cannot start an
    #: MKV is a different problem from having none.
    hardware: str = ""

    def __str__(self) -> str:
        if not self.available:
            return f"{self.encoder.label} - unavailable: {self.reason}"
        if self.hardware:
            return f"{self.encoder.label} on {self.hardware}"
        return self.encoder.label


def detect_encoders(*, test_encode: bool = True) -> list[EncoderAvailability]:
    """Test every encoder and report what worked, with reasons for what did not.

    Presence in ``ffmpeg -encoders`` is **not** proof: `h264_nvenc` is compiled
    into the binary regardless of whether a usable NVIDIA GPU is present, and
    fails only when you try to use it. This was not hypothetical during
    development - a machine with an RTX 5070 Ti reported NVENC as present and
    then refused to open it, because the driver was one API version behind.

    So this actually records one frame of colour bars per candidate, into a
    real MKV the way a take writes one; _test_encode says why encoding to
    nothing was not enough. It costs well under a second each and is the only
    honest answer.
    """
    exe = ffmpeg_path()
    if exe is None:
        return [
            EncoderAvailability(SOFTWARE_ENCODER, True),
            *(
                EncoderAvailability(e, False, "ffmpeg not found")
                for e in HARDWARE_ENCODERS
            ),
        ]

    compiled = _compiled_encoders(exe)
    results = [EncoderAvailability(SOFTWARE_ENCODER, True)]

    for encoder in HARDWARE_ENCODERS:
        if encoder.name not in compiled:
            results.append(
                EncoderAvailability(
                    encoder, False, "not included in this ffmpeg build"
                )
            )
            continue
        if not test_encode:
            results.append(EncoderAvailability(encoder, True))
            continue
        ok, reason, hardware = _test_encode(exe, encoder.name)
        results.append(EncoderAvailability(encoder, ok, reason, hardware))
        log.info(
            "Encoder %s: %s", encoder.name,
            (f"works on {hardware}" if hardware else "works") if ok else reason,
        )

    return results


def _compiled_encoders(exe: Path) -> set[str]:
    try:
        result = subprocess.run(
            [str(exe), "-hide_banner", "-encoders"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        log.exception("Could not list ffmpeg encoders")
        return set()
    return {
        candidate.name
        for candidate in ALL_ENCODERS
        if f" {candidate.name} " in result.stdout
    }


def profile_args(encoder_name: str) -> list[str]:
    """Ask for High profile explicitly, whatever this encoder would default to.

    Left alone the encoders disagree, and the disagreement is invisible: x264
    and Quick Sync produce High, NVENC produces Main, and Media Foundation
    produces Constrained Baseline. So "Archive" meant a different thing
    depending on which machine happened to be in the booth, and the weakest
    result went to the machine with the most hardware in it.

    High is the right target for all of them. It is the 2004 profile every
    player and every editor has handled for twenty years, and it is the one
    that allows the 8x8 transform and CABAC -- the difference is small on
    moving video and large on the flat blocks of colour and small text that a
    lighting overlay is made of.

    Media Foundation is the odd one out in HOW it is asked. The profile names
    ffmpeg documents fail to parse there; it wants the numeric H.264 profile
    idc, where 100 is High.
    """
    if encoder_name == "h264_mf":
        return ["-profile:v", "100"]
    return ["-profile:v", "high"]


def output_pix_fmt(encoder_name: str) -> str:
    """The pixel format to hand this encoder.

    Quick Sync cannot take yuv420p: it warns and converts to nv12 every frame,
    a needless pass whose output is 4:2:0 either way. Media Foundation cannot
    take it *at all* -- it refuses to open with "format negotiation failed"
    rather than converting. Everything else gets yuv420p, which plays
    everywhere.

    This is shared with the detection probe on purpose. The probe existed to
    answer "does this encoder work here", and for a while it answered for a
    command the recorder would never run: it asked Media Foundation for
    yuv420p, got a refusal, and reported a perfectly good NVIDIA card as
    "Windows offers no hardware H.264 encoder here".
    """
    return "nv12" if encoder_name in ("h264_qsv", "h264_mf") else "yuv420p"


#: Media Foundation names the encoder it was handed, but only at debug level.
_MFT_NAME = re.compile(r"MFT name:\s*'([^']+)'")

# ffmpeg's own words for the outcomes the probe tells apart, copied from the
# strings in the bundled 8.1.2 binary rather than written from memory.
#: Media Foundation found no hardware H.264 encoder at all.
_MF_NONE_LISTED = "could not find any MFT for the given media type"
#: It could not start one. Without the line above, that is not the same as
#: having none.
_MF_NONE_STARTED = "could not create MFT"
#: The encoder opened with no stream header (SPS/PPS) to hand over. Printed at
#: debug level; seen from Intel's Quick Sync MFT on the development laptop.
_MF_NO_STREAM_HEADER = "Didn't get extradata"
#: The file could not begin, so no frame reaches it. Every failed MKV take on
#: that laptop printed both.
_HEADER_FAILED = "Could not write header"
_NOTHING_WRITTEN = "Nothing was written into output file"

#: A line ffmpeg itself logged as an error. With "level+" in -loglevel every
#: line carries its severity after any component prefix, as in 8.1.2's
#: "[vost#0:0 @ 0000022d97026dc0] [fatal] Unknown encoder 'no_such_encoder'"
#: or "[error] Error opening output file -." with no component at all.
_TAGGED_ERROR = re.compile(r"^(?:\[[^\]]+\]\s)*\[(?:error|fatal|panic)\]\s(.*)$")


def _test_encode(exe: Path, encoder_name: str) -> tuple[bool, str, str]:
    """Record one frame of colour bars into a real MKV, as a take would.

    Returns (worked, reason it did not, what hardware it reached).

    This used to encode to nothing (-f null), which answered a question no
    take asks. On the development laptop Media Foundation is handed Intel's
    Quick Sync MFT, and that gives no stream header (SPS/PPS) until it has
    encoded a frame. The null muxer never wants one, so detection logged
    "Encoder h264_mf: works on Intel" and offered it. Matroska wants one
    before it can write its own header, so every take on it failed within a
    second with "Could not write header", and a soak take restarted seven
    times before giving up with "No video was written". A take writes MKV
    unless the operator chose MP4 (OutputSettings.container), so that is what
    this writes.

    Every hardware encoder is probed this way, not only Media Foundation. An
    encoder that cannot hand over a stream header up front breaks an MKV take
    whoever made it, and "works" should mean a take would start. The price was
    measured with ffmpeg's rawvideo standing in for the encoder, so that no GPU
    was involved: the temporary folder, a real MKV and removing both took a
    one-frame run from a median of 28 ms to 33 ms over ten runs each, which is
    about 17 ms at launch across the four hardware encoders. h264_nvenc and
    h264_qsv each wrote MKV takes for an hour on that laptop, so they lose
    nothing by it.
    """
    try:
        folder = Path(tempfile.mkdtemp(prefix="wer-encoder-test-"))
    except OSError as exc:
        # Not a quiet fall back to -f null: that is the probe that passed an
        # encoder no take could use. Unproven is unavailable, and "auto" then
        # records on software, which is the safe way to be wrong.
        log.exception("No temporary folder for the %s test recording", encoder_name)
        return False, f"could not make a test recording ({exc.__class__.__name__})", ""
    try:
        return _record_test_take(
            exe, encoder_name, folder / f"encoder-test.{Container.MKV.value}"
        )
    finally:
        _remove_test_folder(folder)


def _record_test_take(
    exe: Path, encoder_name: str, target: Path
) -> tuple[bool, str, str]:
    """Run the probe into ``target`` and judge what ffmpeg said. See _test_encode."""
    is_mf = encoder_name == "h264_mf"
    command = [
        str(exe), "-hide_banner",
        # Media Foundation only says whose encoder it opened at debug level,
        # and the answer decides whether it is worth preferring, so this one
        # candidate is probed loudly. Its output is a few hundred lines into a
        # pipe, not the console, and it happens once per launch. "level+" tags
        # each of those lines with its severity, which is how the errors are
        # found among them when none of the failures named above fits.
        "-loglevel", "level+debug" if is_mf else "error",
        "-f", "lavfi", "-i", "testsrc=size=640x480:rate=30:duration=0.1",
        "-c:v", encoder_name, "-frames:v", "1",
        # Same format the recorder will send. Probing with anything else asks
        # a question about a command that will never be run.
        "-pix_fmt", output_pix_fmt(encoder_name),
    ]
    if is_mf:
        # Without this it will happily fall back to a software MFT, report
        # success, and leave us calling the CPU a GPU.
        command += ["-hw_encoding", "1"]
    command += ["-f", "matroska", "-y", str(target)]
    try:
        result = subprocess.run(
            command, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return False, "the encoder did not respond", ""
    except (OSError, subprocess.SubprocessError) as exc:
        log.exception("Test encode for %s could not run", encoder_name)
        return False, f"could not run ffmpeg ({exc.__class__.__name__})", ""

    stderr = result.stderr or ""
    found = _MFT_NAME.search(stderr) if is_mf else None
    chip = _tidy_hardware_name(found.group(1)) if found else ""
    # Judged by ffmpeg's words as well as its exit status. They are what the
    # failing takes printed, and a run that printed them recorded nothing,
    # whatever it exits with.
    never_started = _HEADER_FAILED in stderr or _NOTHING_WRITTEN in stderr
    if result.returncode == 0 and not never_started:
        return True, "", chip
    if is_mf:
        # The reason below is Wer's reading of ffmpeg's words. Keep the words
        # themselves beside it, so a wrong reading on a machine nobody here
        # has seen can be caught from the log alone.
        log.info(
            "Media Foundation test recording failed; ffmpeg's errors: %s",
            " | ".join(_tagged_errors(stderr))[:400] or "none logged as errors",
        )
        return False, _media_foundation_reason(stderr, chip), chip
    return False, _summarise_encoder_error(stderr), ""


def _media_foundation_reason(stderr: str, chip: str) -> str:
    """Why Media Foundation failed, from ffmpeg's words rather than a guess.

    Every failure used to read "Windows offers no hardware H.264 encoder
    here". On the laptop where it mattered that was false: Windows offered
    Intel's, it opened, and only the MKV could not start. Someone told there
    is no hardware encoder goes looking for a driver problem that is not there.
    """
    if _MF_NONE_LISTED in stderr:
        return "Windows offers no hardware H.264 encoder here"
    if _MF_NONE_STARTED in stderr:
        return "Windows lists a hardware H.264 encoder here, but it would not start"
    encoder = f"the {chip} encoder it reaches" if chip else "the hardware encoder it reaches"
    if _HEADER_FAILED in stderr and _MF_NO_STREAM_HEADER in stderr:
        # MP4 takes do work on this encoder. Measured on the development
        # laptop with ffmpeg 8.1.2 and the same encoder options: .mp4 and
        # fragmented .mp4 (+frag_keyframe+empty_moov) record, and so does
        # -f null; only Matroska fails. The probe does not go on to try MP4 --
        # that is a second encode at every launch -- and Wer does not switch a
        # take's container to suit an encoder. Recording MP4 is the operator's
        # call; meanwhile "auto" picks an encoder that can start the default.
        return (
            f"cannot start an MKV recording: {encoder} only produces its "
            "stream header after the first frame"
        )
    if _HEADER_FAILED in stderr:
        return (
            f"cannot start an MKV recording on {encoder}: ffmpeg could not "
            "write the file's header"
        )
    detail = _summarise_encoder_error("\n".join(_tagged_errors(stderr)))
    return f"a test recording on {encoder} failed: {detail}" if chip else detail


def _tagged_errors(stderr: str) -> list[str]:
    """The messages ffmpeg logged as errors, from a level-tagged transcript."""
    return [
        match.group(1)
        for line in stderr.splitlines()
        if (match := _TAGGED_ERROR.match(line.strip()))
    ]


def _remove_test_folder(folder: Path) -> None:
    """Delete the probe's folder, and say where it is if Windows will not."""
    try:
        shutil.rmtree(folder)
    except OSError:
        # A few kilobytes left in the temporary folder harm nothing, so
        # detection carries on. The path is logged so that a folder appearing
        # there at every launch has an owner.
        log.warning("Could not remove the encoder test folder %s", folder, exc_info=True)


def _tidy_hardware_name(mft: str) -> str:
    """"NVIDIA H.264 Encoder MFT" -> "NVIDIA". Just the vendor is the useful part."""
    for vendor in ("NVIDIA", "AMD", "Radeon", "Intel", "Qualcomm"):
        if vendor.lower() in mft.lower():
            return vendor
    return mft.strip()


def _summarise_encoder_error(stderr: str) -> str:
    """Reduce ffmpeg's error spew to one line a person can act on.

    ffmpeg reports a hardware-encoder failure as a dozen lines of cascading
    pipeline errors, only the first of which says anything useful. Showing the
    lot in a settings dialog is the same as showing nothing.
    """
    interesting = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip() and "Error sending frames" not in line
        and "Task finished" not in line
        and "Terminating thread" not in line
        and "Could not open encoder before EOF" not in line
    ]
    if not interesting:
        return "the encoder could not be opened"

    # Strip ffmpeg's "[h264_nvenc @ 000001abc]" component prefixes.
    first = re.sub(r"^\[[^\]]+\]\s*", "", interesting[0])

    # The driver-version case is common enough, and fixable enough, to deserve
    # phrasing that says what to do rather than quoting API version numbers.
    if "nvenc API version" in first or "minimum required Nvidia driver" in first:
        return "the graphics driver is too old; updating it should enable this"
    # Every probe writes MKV, as a take does, so this line is a take that could
    # not begin, whichever encoder it was. ffmpeg's own wording names neither
    # the container nor what could not start.
    if _HEADER_FAILED in first:
        return "cannot start an MKV recording: ffmpeg could not write the file's header"
    return first[:160]


def build_ffmpeg_command(
    settings: OutputSettings,
    *,
    capture_width: int,
    capture_height: int,
    capture_fps: float,
    output_path: Path,
    ffmpeg: Path | None = None,
) -> list[str]:
    """Assemble the ffmpeg command for a recording.

    Frames arrive on stdin as raw BGR24 at the *capture* size. Any scaling to
    the output size is ffmpeg's job here; doing it upstream in numpy is an
    option we may take later for very large frames, but ffmpeg's scaler is
    better and this keeps one code path.
    """
    exe = ffmpeg or ffmpeg_path()
    if exe is None:
        raise RuntimeError(
            "ffmpeg was not found. Recording is unavailable; run "
            "tools/fetch-ffmpeg.ps1 in a development checkout."
        )

    encoder = settings.resolve_encoder()
    out_width, out_height = settings.output_size(capture_width, capture_height)
    out_fps = settings.fps or capture_fps
    scaling = (out_width, out_height) != (capture_width, capture_height)

    command = [str(exe), "-hide_banner", "-loglevel", "warning", "-stats"]

    # Video input: our pipe, timed by when frames actually arrive.
    #
    # THERE IS DELIBERATELY NO -r ON THIS INPUT, and that is the whole point.
    # An input -r stamps presentation timestamps at that nominal rate no matter
    # when the frames really turned up, which silently overrides
    # -use_wallclock_as_timestamps. A ten-minute take from a camera delivering
    # 16.8 fps into a pipeline told 30 came out as a 5m36s file playing 1.79x
    # too fast, with 40 of its 93 chapters landing past the end of the video --
    # and nothing in the app noticed, because zero frames were dropped and the
    # stop was clean. Measured, not reasoned: feeding 15 fps into a pipeline
    # told 30 reproduces it at exactly 2.000x, and removing the input -r fixes
    # it to 1.002x. The nominal rate belongs on the OUTPUT instead, below.
    #
    # -framerate 1000 is NOT that mistake, though it reads like it, and must
    # never be "tidied" into one. It is not an ffmpeg option: it goes only to
    # the rawvideo demuxer, where it sets the time base each frame's arrival is
    # rounded onto (ffmpeg 8.1.2, rawvideodec.c), and nothing re-times the
    # frames. At the default of 25 every arrival was rounded to the nearest
    # 40 ms, which put up to 20 ms of error on the first frame of every take and
    # skewed where -fps_mode cfr placed the frames after it; at 1000 it is the
    # nearest millisecond. Measured 13 Sep 2026 on synthetic feeds: the file's
    # length still matched the wall clock at 10, 15, 16.8, 25, 29.97, 30 and
    # 60 fps, while the input -r control fed 16.8 fps came out 1.79x too fast
    # again; and at 60 fps ffmpeg's own drop count fell from about 60 to 1.
    #
    # -fpsprobesize 0 has to come with it. ffmpeg counts a 1/1000 time base as
    # unreliable and would otherwise read up to 41 frames to guess a rate
    # before the input counted as open, stopping only at its 5 MB probe size:
    # measured, 22 frames at 320x240 and 8 at 640x360, against exactly one
    # with it at every size. The guess can also snap to a standard rate, which
    # brings the rounding skew back.
    video_input: list[str] = []
    if settings.av_offset_ms < 0:
        # Negative offset means video needs delaying. -itsoffset belongs to the
        # input whose options it precedes, so it stays inside this block.
        video_input += ["-itsoffset", f"{abs(settings.av_offset_ms) / 1000:.3f}"]
    video_input += [
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{capture_width}x{capture_height}",
        "-framerate", "1000",
        "-fpsprobesize", "0",
        "-use_wallclock_as_timestamps", "1",
        "-i", "pipe:0",
    ]

    has_audio = bool(settings.audio_device)
    if not has_audio:
        command += video_input
        command += ["-map", "0:v"]
    else:
        audio_input: list[str] = []
        if settings.av_offset_ms > 0:
            audio_input += ["-itsoffset", f"{settings.av_offset_ms / 1000:.3f}"]
        # -rtbufsize is how much captured sound DirectShow keeps for ffmpeg to
        # read. The default is 3 MB, and from 62% full DirectShow throws
        # packets away -- one in four, then more -- which at 48 kHz stereo is
        # about ten seconds of sound waiting. Ten seconds is exactly what
        # -shortest used to hold back (see -shortest_buf_duration below), so
        # every take sat on that edge. 64 MB is minutes of sound at any format
        # Wer records, and is only filled while ffmpeg is behind, so a stall
        # that would have cost sound now costs nothing. Measured on the
        # UltraStudio's input: a minute that lost 6.5% of its sound at the
        # default lost none with the buffer raised.
        audio_input += ["-f", "dshow", "-audio_buffer_size", "50", "-rtbufsize", "64M"]
        # Input options, so they belong before -i. Unset, DirectShow hands
        # over the first format the pin lists, which on the inputs measured is
        # 44.1 kHz -- so HDMI audio at 48 kHz was resampled in the driver on
        # every take. These are only ever set from a format the device listed:
        # DirectShow refuses to open one it did not, and a guessed rate would
        # lose the audio input outright.
        if settings.audio_sample_rate:
            audio_input += ["-sample_rate", str(settings.audio_sample_rate)]
        if settings.audio_sample_bits:
            audio_input += ["-sample_size", str(settings.audio_sample_bits)]
        if settings.audio_channels:
            audio_input += ["-channels", str(settings.audio_channels)]
        audio_input += ["-i", f"audio={settings.audio_device}"]

        # THE AUDIO INPUT GOES FIRST, and the recorder launches ffmpeg on a
        # frame already queued (Recorder.start), so that one is waiting in the
        # pipe. ffmpeg opens its inputs one at a time, in command-line
        # order, each through its own probe, and zeroes each on its own first
        # timestamp: the picture on the first frame it reads, the sound on its
        # first sample. With the pipe first, that frame was read at launch and
        # DirectShow was opened only after it, so every take's sound was early
        # by however long DirectShow took to open -- measured 12-13 Sep 2026 at
        # 280 to 790 ms early, different on every take, with the first 8 to 24
        # frames frozen while the second waited for the open. Audio first, the
        # frame is read once the sound is already flowing: on the development
        # laptop's camera and microphone array, sound 58 to 67 ms late, the same
        # take after take, and no frozen start. Each -itsoffset stays with its
        # own block; moved with the wrong one, a positive offset would delay the
        # picture instead of the sound.
        command += audio_input + video_input
        # The picture stays output stream 0: output streams follow the -map
        # order, not the inputs'. Measured with the reordered command: the same
        # #0:0 Video and #0:1 Audio, and the same sound statistics, as before.
        command += ["-map", "1:v", "-map", "0:a"]

    if scaling:
        # flags=bicubic rather than the default: at the downscales that matter
        # here (4K to 1080p) it holds small text together noticeably better,
        # and legible cue numbers are the entire point of the recording.
        stretching = (
            capture_width > 0 and capture_height > 0
            and abs(capture_width / capture_height - out_width / out_height) > 0.01
        )
        if stretching:
            # Pillarbox rather than stretch. A plain scale=W:H ignores the
            # shapes entirely: measured, a 200x200 square from a 640x480 camera
            # came out 400x300 when recorded at 1280x720, a 1.333x horizontal
            # stretch held for the whole take with nothing said about it. It is
            # not an exotic pairing -- best_format scores formats only on how
            # close the height is to 1080, so a 4:3 capture card lands on
            # 1440x1080 by itself and "Record at 1920x1080" is the obvious next
            # click. Black bars are honest; a squashed stage is not.
            # force_divisible_by=2 keeps the scaled picture even-sided before
            # it is padded. yuv420p subsamples chroma by two, and an odd
            # intermediate is a needless way to have a take refused.
            command += [
                "-vf",
                f"scale={out_width}:{out_height}"
                f":force_original_aspect_ratio=decrease:force_divisible_by=2"
                f":flags=bicubic,"
                f"pad={out_width}:{out_height}:(ow-iw)/2:(oh-ih)/2",
            ]
        else:
            command += ["-vf", f"scale={out_width}:{out_height}:flags=bicubic"]

    if has_audio:
        # WITHOUT THIS, FFMPEG NEVER EXITS. It finishes when all inputs are
        # exhausted, and a live dshow microphone is never exhausted -- so
        # closing the video pipe at the end of a take left ffmpeg waiting on
        # audio forever, and Stop hung for the full shutdown timeout. -shortest
        # ends the output when the shortest input ends, which is the video pipe.
        #
        # -shortest_buf_duration is how far -shortest lets one stream run
        # ahead of the others while it waits to see which ends first. Left at
        # its default of 10 s, the sound ran the full ten seconds ahead for
        # the whole take and backed up into DirectShow's buffer until packets
        # were dropped: an hour-long webcam soak on libx264 was missing 238 s
        # of sound (6.6%) while every frame of picture was there, and NVENC
        # reached the same 62%-full mark in a one-minute test. Measured with
        # Wer's libx264 command on the UltraStudio's input, one minute each:
        # 6.5% of the sound lost at 10 s, 47% at 30 s, none at 1 s -- with
        # ffmpeg still finishing as the picture ended and the sound ending
        # within 0.02 s of it. A second is still far longer than a frame takes
        # to reach the encoder, so -shortest still ends the take where the
        # video pipe closes.
        command += ["-shortest", "-shortest_buf_duration", "1"]

    # Constant frame rate on the output, from the real arrival timestamps.
    # ffmpeg duplicates frames to fill gaps when the camera runs slow and drops
    # them when it runs fast, so the file is always the length it was recorded
    # for. VFR would also give the right duration and a smaller file, but it
    # declares a guessed nominal rate that players show wrongly, and a tech
    # recording is scrubbed far more often than it is watched through.
    # Duplicated frames cost almost nothing: they encode as near-empty P-frames.
    command += ["-fps_mode", "cfr", "-r", f"{out_fps:g}"]

    command += ["-c:v", encoder.name]
    command += encoder.quality_args(settings.resolve_quality())

    if encoder.name == "h264_mf":
        # Media Foundation has no -preset; passing one is a hard error. What it
        # has instead is a switch demanding real hardware.
        command += ["-hw_encoding", "1"]
    else:
        command += ["-preset", settings.speed_preset or encoder.preset_default]

    command += profile_args(encoder.name)

    command += [
        "-pix_fmt", output_pix_fmt(encoder.name),
        "-g", str(settings.keyframe_interval or max(1, int(round(out_fps * 2)))),
    ]

    if has_audio:
        command += ["-c:a", "aac", "-b:a", f"{settings.audio_bitrate_kbps}k"]
        command += sound_stats_args()
        command += sound_level_args(settings.audio_sample_rate)

    # Write each packet through as it is muxed instead of waiting for ffmpeg's
    # 256 KiB output buffer to fill. Without it the file on disk is literally
    # zero bytes until the first block is full, and how long that takes is
    # purely a function of bitrate: measured on a dark, static stage at 720p
    # Compact -- a lighting rehearsal with the house out -- the first byte
    # landed 94 seconds in, and a hard kill before that left a file that would
    # not open at all. That defeats the reason MKV is the default container
    # (line 66): surviving an incomplete write is worth nothing when there is
    # nothing on disk to survive. The writes are sequential and the OS
    # coalesces them, so the cost is not measurable.
    command += ["-flush_packets", "1"]

    if settings.container is Container.MP4 and settings.fragmented_mp4:
        # +delay_moov is not decoration. Fragmentation alone -- either flag, on
        # its own -- makes the mov muxer drop the edit list that expresses a
        # track's start delay, so a POSITIVE A/V offset was silently discarded:
        # +0 ms, +500 ms and +1000 ms all produced identically aligned files
        # while ffmpeg exited 0 and said nothing. (Negative offsets survived,
        # being realised as duplicated leading video frames, so the control
        # worked one way and not the other.) delay_moov holds the moov box back
        # until the first fragment, which restores the offset -- measured 478 ms
        # for a 500 ms request, the same 22 ms of AAC priming delay an
        # unfragmented MP4 shows -- and the file still carries moof boxes, so an
        # interrupted recording still plays.
        command += ["-movflags", "+frag_keyframe+empty_moov+delay_moov"]
        # Close a fragment every two seconds as well as on a keyframe, so what
        # an interrupted MP4 loses is bounded by time rather than by bitrate.
        command += ["-frag_duration", "2000000"]

    command += ["-y", str(output_path)]
    return command


#: The line ffmpeg writes to its stdout for every frame of sound on its way into
#: the audio encoder: the encoder's time base, where the frame starts in it, and
#: how many samples it holds. Recorder reads these to tell how much of its sound
#: an input is really delivering.
SOUND_STATS_FORMAT = "wer-sound tb={tb} pts={pts} samp={samp}"


def sound_stats_args() -> list[str]:
    """Output options asking ffmpeg for SOUND_STATS_FORMAT lines on its stdout.

    An audio input can deliver only part of its sound while everything else
    looks healthy. A USB webcam's microphone delivered half, in gaps of about
    50 ms every 100 ms, at every format it offered: a ten-minute take came out
    with every frame of picture, 50.0% of its sound missing, and nothing from
    ffmpeg or in Wer's log to say so. The gaps stay in the timestamps, so the
    samples reaching the encoder, counted against the time their timestamps
    cover, find them. Nothing else ffmpeg reports does: its progress time
    follows the timestamps, and that take's sound still ended within 0.06 s of
    its picture.

    On stdout because nothing else goes there -- the take is written to a
    file. On stderr these lines were broken up by ffmpeg's progress lines,
    which would have put fragments in the log and pushed real errors out of
    the recorder's tail.
    """
    return ["-stats_enc_pre:a", "pipe:1", "-stats_enc_pre_fmt:a", SOUND_STATS_FORMAT]


#: The metadata key the chain below prints, one line for each second of sound:
#: "lavfi.astats.Overall.Peak_level=-84.288399", and "=-inf" for a second whose
#: samples are all exactly zero. A true dBFS figure: measured on the bundled
#: build, full scale reads 0.000000, half scale -6.020600, one LSB of 16-bit
#: -90.308999 and two of them -84.288399 -- which is as loud as the laptop's
#: microphone array ever got.
SOUND_LEVEL_KEY = "lavfi.astats.Overall.Peak_level"


def sound_level_chain(sample_rate: int | None) -> str:
    """An audio filter chain reporting the peak level once a second on stdout.

    The delivery check above cannot see digital silence: an input handing over
    exact zeros delivers every sample its timestamps promise, and scores
    1.000. The laptop's microphone array, behind Elevoc and Voice Clarity, did
    exactly that on the night of 13 Sep 2026 -- exact zeros to DirectShow
    except while somebody spoke -- and a capture device whose HDMI carries no
    sound looks identical. Only the level tells them from a quiet house, and
    ffmpeg reports it for nothing: astats is in the bundled build already.

    asetnsamples gathers the sound into frames of about a second, so astats,
    reset every frame, measures a second at a time; ametadata prints the one
    measurement that is wanted. direct=1 writes each line straight out instead
    of holding it until a buffer fills, so the reports arrive while the take
    is running rather than at the end of it.

    Onto pipe:1, beside the delivery statistics. Measured on the bundled 8.1.2
    build, ten seconds with both writers on that pipe: 469 delivery lines and
    10 level lines, every one of them whole. The take itself is unchanged. The
    chain holds a second of sound back before it passes any on, which is the
    thing to check, and it costs none: measured in the shape Wer records in --
    an audio input that never ends, the video pipe closing the file, -shortest
    ending it there -- takes of 10.3, 20 and 30.7 seconds came out the same
    length with the chain as without, to within the one AAC frame of 21 ms
    that the sound is rounded to either way.

    ``sample_rate`` is the rate the input was opened at, where it is known.
    Unset, DirectShow picks its own -- 44.1 kHz on the inputs measured -- and
    a frame of 48000 samples then covers 1.09 seconds instead of 1.00. That is
    near enough: what is judged is the stretch the reports' own timestamps
    cover, not the number of them.
    """
    # The colon in "pipe:1" is escaped twice over because it passes two
    # parsers: the filter description's, which eats one backslash, and the
    # filter's own options, which splits on colons. Escaped once, ffmpeg reads
    # the value as "pipe" and refuses the whole chain.
    return (
        f"asetnsamples=n={sample_rate or 48000},"
        f"astats=metadata=1:reset=1:measure_overall=Peak_level"
        f":measure_perchannel=none,"
        f"ametadata=mode=print:key={SOUND_LEVEL_KEY}:file=pipe\\\\:1:direct=1"
    )


def sound_level_args(sample_rate: int | None) -> list[str]:
    """sound_level_chain as the audio stream's filter, for an ffmpeg command.

    One -filter:a for the stream, and one only. Anything else Wer ever filters
    the sound with has to join this chain rather than arrive as a second -af:
    ffmpeg keeps the last of them and drops the rest, saying so in a warning
    among a hundred other lines, and the filter that was dropped simply never
    happens.
    """
    return ["-filter:a", sound_level_chain(sample_rate)]


@dataclass(frozen=True, slots=True)
class _Detected:
    """What a background detection found, for "auto" to choose from.

    Detection never runs on demand. It test-encodes a frame with each
    candidate, which costs seconds, and a setting is resolved wherever an
    ffmpeg command is built -- including while the main window is being
    constructed, where probing on demand put five seconds on startup and
    tripped the test that exists to keep slow work off the UI thread. So the
    UI probes once in the background and calls remember_detected_encoders().
    Until it does, "auto" resolves to software, which is the safe way to be
    wrong: it records.

    One object, replaced whole. It used to be two module globals, the encoders
    and Media Foundation's vendor, assigned one after the other, while "auto"
    is resolved on whichever thread builds a command -- and the recorder builds
    one for every part it restarts, not only on the interface thread. A reader
    landing between the two assignments would pair one detection's encoders
    with another's vendor. Rebinding one name is a single step, and
    resolve_encoder_name reads it once for both halves of its decision.
    """

    encoders: tuple[VideoEncoder, ...]
    #: Which vendor's chip Media Foundation was handed here, if detection saw it.
    mf_vendor: str = ""


_DETECTED: _Detected | None = None

#: The encoder that talks to each vendor's silicon directly. Where one of these
#: works, it is the better way to reach that chip than going through Media
#: Foundation: same hardware, but a real rate-control interface instead of a
#: 0-100 dial.
_VENDOR_PREFERENCE = (
    # Dedicated before integrated. HARDWARE_ENCODERS lists Quick Sync ahead of
    # AMF for no reason that survives a laptop carrying both an Intel iGPU and
    # a Radeon, so the order that matters lives here instead.
    ("h264_nvenc", ("NVIDIA",)),
    ("h264_amf", ("AMD", "Radeon")),
    ("h264_qsv", ("Intel",)),
)


def remember_detected_encoders(
    results: "list[VideoEncoder] | list[EncoderAvailability]",
) -> None:
    """Record what a background probe found, for "auto" to choose from.

    Takes either the full detection results or just the encoders that worked.
    Pass the full results where you have them: they carry which chip Media
    Foundation reached, and that is what decides whether it is the best way to
    reach it.
    """
    global _DETECTED
    encoders: list[VideoEncoder] = []
    vendor = ""
    for item in results:
        if isinstance(item, EncoderAvailability):
            if not item.available:
                continue
            if item.encoder.name == "h264_mf":
                vendor = item.hardware
            encoders.append(item.encoder)
        else:
            encoders.append(item)
    _DETECTED = _Detected(tuple(encoders), vendor)


def forget_detected_encoders() -> None:
    """Drop the cache. Used by tests, and after a re-probe."""
    global _DETECTED
    _DETECTED = None


def resolve_encoder_name(
    wanted: str, available: "list[VideoEncoder] | None" = None
) -> str:
    """Turn an encoder setting into a real encoder name.

    "auto" picks the best working hardware encoder, else software. Anything
    else is honoured exactly, including a hardware encoder this machine cannot
    run -- the caller reports that, rather than quietly recording with
    something the user did not choose.

    Hardware is preferred for headroom, not for frame rate. It is worth being
    precise about that, because the frame-rate case was made here and it was
    wrong: an hour of 1080p30 on x264 spent 32.5% of its seconds under 29 fps
    against Quick Sync's 11.9%, and this docstring concluded that software
    encoding starves the capture thread. It does not. Reading the same camera
    with no encoder attached and Wer not running at all still spends 20% of
    its seconds under 29, and the real cause turned out to be the webcam's
    auto-exposure hunting in low light -- lock the exposure and the dips stop
    whatever is encoding. What hardware encoding actually buys is a CPU free
    to do everything else during a four-hour tech.
    """
    if wanted and wanted != AUTO_ENCODER:
        return wanted
    # Read once, for the encoders and the vendor alike; see _Detected.
    detected = _DETECTED
    if available is None:
        encoders = list(detected.encoders) if detected is not None else []
    else:
        encoders = available
    mf_vendor = detected.mf_vendor if detected is not None else ""
    hardware = [e for e in encoders if e.is_hardware]
    if not hardware:
        return SOFTWARE_ENCODER.name

    names = {e.name for e in hardware}
    # Walk the vendors best-first. For each: its own encoder if that works,
    # else Media Foundation if Media Foundation is what reaches that chip.
    # Media Foundation still earns its place -- it is what puts a dedicated
    # NVIDIA card to work on a driver nvenc refuses to open -- but it only
    # answers which chip it reaches. It no longer decides the order.
    #
    # It used to. The old rule asked Media Foundation first and returned the
    # native encoder of whichever chip it was handed -- and which chip that is
    # depends on how the machine happens to be set up, not on which GPU is
    # best. It follows the adapter driving the display. On the same laptop and
    # the same driver, hours apart, detection said "works on NVIDIA" in the
    # morning and "works on Intel" in the evening; in between, the Intel iGPU
    # had been re-enabled while debugging a Thunderbolt port, and it took the
    # panel back. Because that check returned before a working h264_nvenc was
    # ever considered, the evening's recordings went to Quick Sync on the iGPU
    # while the dedicated RTX 5070 Ti sat idle. Nothing failed and nothing was
    # logged. Toggling a GPU, docking, or plugging a display into a port wired
    # to the other adapter is ordinary booth life, so Media Foundation reports
    # a chip here and does not get to choose one.
    for native, vendors in _VENDOR_PREFERENCE:
        if native in names:
            return native
        if "h264_mf" in names and mf_vendor in vendors:
            return "h264_mf"

    # Media Foundation on a chip with no native encoder here (or one whose
    # vendor detection could not name) is still better than software.
    return hardware[0].name
