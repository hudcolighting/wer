"""The recorder: frames down a pipe into ffmpeg.

No Qt. Runs a writer thread that pulls frames from a queue and writes them to
ffmpeg's stdin, plus a reader thread that drains ffmpeg's stderr so the process
cannot deadlock on a full pipe.

This is the part that must not fail, so the failure modes are handled
explicitly rather than left to chance:

- **ffmpeg dies mid-recording.** Detected within a frame. Rather than losing
  the rest of the session, the recorder starts a fresh ffmpeg writing a
  numbered continuation file and carries on. Three hours into a tech, an
  encoder hiccup should cost a two-second gap, not the remaining acts. The
  failure is still reported, and the parts are listed so nothing is hidden.
- **ffmpeg stays alive but stops taking frames.** The write into its pipe has
  no timeout and nothing on the writer thread runs while it is stuck, so a
  watchdog thread times each write: the operator is told, and ffmpeg is
  restarted into a new part only once a pause that might recover plainly
  has not.
- **The audio input goes away.** Once ffmpeg keeps failing and the evidence
  points at the audio input, the take carries on without sound and says so.
  With no evidence either way it is still tried without sound before the take
  is given up, and said to be a trial. The picture is the part that cannot be
  had again.
- **The audio input delivers nothing but digital silence.** An input can go on
  handing over samples, every one of them zero, while ffmpeg reports nothing
  wrong and the check above reads a perfect 100%. The peak level ffmpeg
  measures once a second is the only thing that tells that from a quiet house,
  and the take says so and carries on.
- **The encoder cannot keep up.** Counted and surfaced live, apart from the
  frames lost to ffmpeg failing, so that neither is blamed for the other.
  Silent frame drops are worse than a visible warning.
- **The disk fills.** Checked before arming and monitored during.
- **A crash or power cut.** MKV by default, which survives an unclosed file
  where MP4 does not.
- **stderr fills.** A subprocess whose stderr pipe is never read will block on
  write and hang forever. ffmpeg with `-stats` is chatty, so this is a real
  risk, not a theoretical one.
"""

from __future__ import annotations

import logging
import math
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

import numpy as np

from wer.paths import ffmpeg_path
from wer.video.capture import Frame
from wer.video.encoder import (
    SOUND_LEVEL_KEY,
    OutputSettings,
    build_ffmpeg_command,
)

log = logging.getLogger(__name__)

__all__ = [
    "RecorderState",
    "RecordingStats",
    "RecordingPart",
    "Recorder",
    "DiskSpace",
    "DiskWarning",
    "check_disk",
    "SOUND_SETTLE_SECONDS",
    "SOUND_WINDOW_SECONDS",
    "SOUND_JUDGED_WINDOWS",
    "SOUND_SHORT_BELOW",
    "SOUND_SILENCE_SECONDS",
    "SOUND_SILENT_SECONDS",
    "SOUND_SILENT_BELOW_DBFS",
]

#: How long to wait for ffmpeg to finish writing after stdin closes. A long
#: recording has buffered frames to flush; cutting it short truncates the file.
SHUTDOWN_TIMEOUT = 30.0

#: How often to look at free space while recording. A stat call is cheap and
#: four hours is a long time for a disk to fill without anyone noticing.
DISK_CHECK_INTERVAL = 10.0

#: How many ffmpeg failures IN A ROW the recorder restarts from before giving
#: up. A single hiccup is worth surviving; a process that dies immediately every
#: time is a real fault and retrying forever would just produce a directory
#: full of empty files.
#:
#: In a row, not in a take. It used to count every restart since Record was
#: pressed and never refill, so five unrelated deaths spread across a
#: four-hour tech ended the session at the sixth, though every part had
#: recorded for most of an hour -- which is not the fault this limit is for. A
#: part that records for HEALTHY_PART_SECONDS clears the count.
MAX_RESTARTS = 5

#: How long a part must record before the failures that led up to it stop
#: counting against MAX_RESTARTS. A minute is far longer than a process that
#: dies "immediately" survives, and far shorter than the gap between failures
#: that are merely unlucky.
HEALTHY_PART_SECONDS = 60.0

#: The pause before each restart in a run of failures, indexed by how many in
#: a row. No pause before the first, so an isolated hiccup still costs the
#: shortest gap it can. A judgement, not a measurement: with no pause at all a
#: fault lasting a few seconds -- a GPU driver resetting, an audio interface
#: re-enumerating -- used up every restart inside those seconds and ended the
#: take, and these add up to 44 seconds of trying. Frames that arrive meanwhile
#: are thrown away and counted as dropped, because there is no ffmpeg to take
#: them; see _wait_before_restarting.
RESTART_DELAYS = (0.0, 1.0, 3.0, 10.0, 30.0)

#: How long one write into ffmpeg's pipe may take before the recorder says
#: ffmpeg has stopped taking frames. The write has no timeout of its own, and
#: while it is stuck nothing else on the writer thread runs -- not the check
#: for a dead ffmpeg, not the camera stall check, not the disk check -- so a
#: take whose ffmpeg stayed alive but stopped reading sat in RECORDING dropping
#: every frame until Stop, and was then reported as "Saved". Ten seconds rather
#: than less because ffmpeg opens a hardware encoder only when the first frame
#: reaches it, which is not instant, and the writes some ten frames into a part
#: wait for that; a warning that fires at the start of every take is one people
#: learn to ignore. The FIRST write to an ffmpeg with an audio input waits for
#: something else, the input opening, and has limits of its own:
#: FIRST_WRITE_WARN_AFTER.
WRITE_STALL_WARN_AFTER = 10.0

#: How long before a stuck ffmpeg is killed so the take can continue into a new
#: part. Much longer than the warning, on purpose. A disk that pauses and
#: recovers -- an SMR drive flushing its cache, a network share hiccuping --
#: lets the write finish, and killing ffmpeg during it would turn a short gap
#: into a cut, lose the frames it had buffered and leave that file without a
#: proper ending. A minute of nothing is past any pause worth waiting out: at
#: 30 fps it is already 1,800 dropped frames. A judgement, not a measurement --
#: nothing here has wedged a real hardware encoder.
WRITE_STALL_RESTART_AFTER = 60.0

#: The longest start() waits for ffmpeg to show it is recording before it
#: announces the take anyway. start() runs on the interface thread, so this is
#: a cap and not a hold: a healthy take is announced the moment its second frame
#: lands in ffmpeg's pipe, which with the development laptop's microphone array
#: was 0.37-0.41 s after launch (13 Sep 2026), and a failure is refused the
#: moment ffmpeg exits or says so; see _wait_until_recording. Two seconds is
#: about five times that. One budget for the whole wait, including a take with
#: sound waiting for a frame to launch ffmpeg on
#: (_wait_for_a_frame_to_launch_on), so a camera slow to deliver shortens what
#: is left for ffmpeg rather than holding the interface longer. Reached with no
#: frame in flight, the camera has not delivered one, which the writer's stall
#: check reports; reached with one in flight, the audio input is still opening,
#: which FIRST_WRITE_WARN_AFTER covers. It replaced a fixed 0.6 s wait that,
#: measured with the bundled 8.1.2 build, caught none of an unknown encoder, an
#: output ffmpeg could not open or an audio input it could not open, because no
#: frame reached ffmpeg until the wait was over and ffmpeg found none of them
#: before its first.
START_READY_WAIT = 2.0

#: How long the first write to an ffmpeg with an audio input may wait before
#: the operator is told that input has not opened. ffmpeg opens the audio input
#: before it reads a single frame (see build_ffmpeg_command), so that write
#: lasts as long as DirectShow's open, and an open that hangs says nothing at
#: all: measured with a stand-in input that never delivered, the write stayed
#: blocked until ffmpeg was killed 7.1 s later, with nothing on stderr. Five
#: seconds is several times the slowest open measured -- about 0.85 s from
#: opening the device to its first sound -- and nothing is recorded meanwhile.
FIRST_WRITE_WARN_AFTER = 5.0

#: How long that first write may wait before ffmpeg is killed and the take goes
#: down the restart path, where a failing audio input is dealt with. Fifteen
#: seconds because that is what Wer gives its own DirectShow queries
#: (devices._run_ffmpeg): a device that has not finished opening in the time
#: Wer would give up on listing it is not going to. Far shorter than
#: WRITE_STALL_RESTART_AFTER, rightly: that one waits out a pause that may
#: recover, where killing ffmpeg would lose the frames already inside it, and a
#: first write has nothing inside ffmpeg to lose. A judgement, not a
#: measurement -- the Scarlett, the Blackmagic's Line In and the USB camera's
#: microphone have not had their opens timed.
#:
#: Two such kills in a row are taken as evidence against the input, and the
#: take goes on without sound (_reason_to_drop_audio). An open that hangs
#: prints nothing and leaves the device listed, which was all that counted, so
#: every kill was a failure with nothing pointing at the input: at these values
#: the picture was lost for six kills and every pause between them, about
#: 164 s, before it was even tried without sound. Now about 34 s, three of
#: them making sure the camera is still there (_camera_still_there).
FIRST_WRITE_RESTART_AFTER = 15.0

#: How fast a first write to an ffmpeg with an audio input must land, and how
#: long after launch it must have set off, for the recorder to conclude that
#: ffmpeg was already reading its pipe when the frame set off. ffmpeg reads the
#: pipe only once the audio input has opened, and times the sound from its first
#: sample and the picture from that read, so the input had then opened with no
#: frame to read, and the sound is late against the picture, for the whole part,
#: by however long it waited. Measured 12-13 Sep 2026: a frame into an ffmpeg
#: already reading landed in 1.5-2.3 ms, and the fastest DirectShow open, from
#: launch to its first sound delivered, took about 0.37 s. A tenth of a second
#: is some forty times the one and under a third of the other. See
#: _note_if_the_sound_is_late.
FIRST_FRAME_IN_STEP_WITHIN = 0.1

#: How often the watchdog looks at the write in flight.
WATCHDOG_INTERVAL = 1.0

#: How often the writer measures what the take has put on disk. The Recording
#: tab shows that figure and must not measure it itself; see
#: RecordingStats.file_size_mb.
SIZE_SAMPLE_INTERVAL = 2.0

#: The low-disk estimate uses the write rate over this recent window as well as
#: over the whole take, whichever is faster. See _check_disk_if_due.
RATE_WINDOW_SECONDS = 300.0

#: A window shorter than this is not used. Over a few seconds the size moves in
#: steps as the muxer writes, and the rate says more about those than about the
#: stage.
RATE_WINDOW_MIN_SPAN = 30.0

#: The low-disk warning is given again as the time left falls past each of
#: these, in minutes. It used to be given once and latch, so if that first
#: estimate was hours out, it was still the only one anyone got.
LOW_DISK_REANNOUNCE_MINUTES = (60, 30, 10)

#: Once frames are being dropped, the running total for each reason is logged
#: at most this often while it keeps rising. It used to be logged at 1, 10, 100
#: and 1,000 and never again, so the morning log could not tell 1,001 drops
#: from 300,000.
DROP_REPORT_INTERVAL = 60.0

#: Counts of dropped frames that are always logged, for each reason: what
#: someone searching the log for the moment it started will look for.
DROP_MILESTONES = (1, 10, 100, 1000)

#: A line ffmpeg keeps repeating is logged the first time, then at most this
#: often, with a count. See _RepeatGate.
FFMPEG_REPEAT_INTERVAL = 60.0

#: How many different kinds of ffmpeg line are tracked separately. Past this,
#: new kinds are counted together and reported on the same interval, so no
#: pattern of output can make the log grow faster than a line or two a minute
#: per kind.
FFMPEG_LINE_KINDS = 20

#: An absolute floor, used only if the configured stop level is set lower
#: than this or disabled. A disk that fills mid-write does not fail politely:
#: ffmpeg's writes start failing partway through a frame, and Windows itself
#: starts misbehaving with no space for temp files.
CRITICAL_FREE_BYTES = 500_000_000

#: How long the queue may stay empty before the recorder says the camera has
#: stopped delivering. Generous on purpose: the slowest format the app will
#: choose is 15 fps, so three seconds is forty-odd missing frames, not a
#: hiccup.
STALL_TIMEOUT = 3.0

#: How stale the newest write may be before the measured rate stops being
#: reported at all. See RecordingStats.write_fps.
FRAME_RATE_STALE_AFTER = 1.0

#: How long stop() will wait to get the sentinel into a full queue.
SENTINEL_TIMEOUT = 5.0

#: How long stop() waits for the writer to drain. Generous: at 30 fps a full
#: 90-frame queue is three seconds of footage, and it is worth waiting for.
WRITER_JOIN_TIMEOUT = 30.0

#: Above this, a part is taken on trust and not probed. Measured: the smallest
#: cleanly-closed file the app can produce is a single frame of a dark stage at
#: Compact, 1.5 KB, and a second of it is ~3 KB -- so 64 KB is tens of seconds
#: of the quietest possible recording. Anything larger holds pictures; anything
#: smaller is cheap to check properly. See _verify_parts.
PROBE_BELOW_BYTES = 65_536

#: Last-resort floor, used only when ffmpeg cannot be asked. A container header
#: on its own is 592-594 bytes here across every preset; the smallest file that
#: holds even one frame is 1,425. See _verify_parts.
HEADER_ONLY_BYTES = 1_024


class RecorderState(str, Enum):
    IDLE = "Idle"
    STARTING = "Starting"
    RECORDING = "Recording"
    STOPPING = "Finishing"
    ERROR = "Error"


@dataclass
class DiskSpace:
    free_bytes: int
    total_bytes: int

    @property
    def free_gb(self) -> float:
        return self.free_bytes / 1_000_000_000

    def hours_at(
        self, megabytes_per_hour: float, *, reserve_bytes: int | None = None
    ) -> float:
        """Hours of recording that fit in the free space.

        WITHOUT ``reserve_bytes`` this counts down to zero bytes free, which is
        NOT where a recording ends: the recorder stops itself at
        max(stop_below_bytes, CRITICAL_FREE_BYTES) so the file can be closed
        and its chapters embedded, and embedding rewrites the file, which
        briefly needs its size again. At the shipped 10 GB floor that makes
        this number roughly double the truth, and it disagrees with the live
        low-disk warning, which was corrected to count down to the floor: at
        20 GB free this says "about 5 hours" where the mid-take warning says
        "about 150 minutes".

        Pass the recorder's ``stop_below_bytes`` to get the honest figure. The
        default is left at zero reserve on purpose rather than guessed, because
        the floor is a per-show setting (1-500 GB) that only the caller knows
        -- the one call site is record_panel.py's idle "about N hours" line,
        and passing its own configured floor is all that is needed to make the
        two numbers on that tab mean the same thing.
        """
        if megabytes_per_hour <= 0:
            return float("inf")
        floor = 0 if reserve_bytes is None else max(reserve_bytes, CRITICAL_FREE_BYTES)
        usable = max(0, self.free_bytes - floor)
        return (usable / 1_000_000) / megabytes_per_hour


def _validate_audio_device(name: str | None) -> str | None:
    """Check an audio device exists before handing it to ffmpeg.

    ffmpeg now reports a missing device itself, and at once: it opens its audio
    input before it reads a frame (see build_ffmpeg_command) and exits when
    that fails -- a stand-in for it exited 26 ms after launch -- and start()
    refuses the take with ffmpeg's words. Checked here first anyway, because
    this answer lists the devices that ARE there, which is what someone at the
    desk needs next, and ffmpeg's "Could not find audio only device" is not.

    Returns an error message, or None if the device is fine.
    """
    if not name:
        return None
    try:
        from wer.video.devices import enumerate_audio_devices

        available = [device.name for device in enumerate_audio_devices()]
    except Exception:  # noqa: BLE001
        # If enumeration fails we must not block recording over it; let ffmpeg
        # try, and video will still be captured either way.
        log.exception("Could not enumerate audio devices; proceeding anyway")
        return None

    if not available:
        return (
            f"No audio capture devices were found, so \"{name}\" cannot be "
            "used. Set Audio to None to record video only."
        )
    if name not in available:
        listed = "\n  ".join(available)
        return (
            f'The audio device "{name}" is not available.\n\n'
            f"Devices found:\n  {listed}\n\n"
            "Pick one of those, or set Audio to None to record video only."
        )
    return None


def _audio_device_listed(name: str) -> bool | None:
    """Whether DirectShow still lists the audio input ``name``.

    For a restart mid-take, where _validate_audio_device's message is no use.
    None means the question could not be answered, which is not the same as
    "no". An empty list counts as not knowing too: it is also what a probe
    that failed or timed out hands back, and a probe timing out is no evidence
    that the device has gone.
    """
    try:
        from wer.video.devices import enumerate_audio_devices

        available = [device.name for device in enumerate_audio_devices()]
    except Exception:  # noqa: BLE001 - asking must never end the take
        log.exception("Could not list audio inputs while restarting ffmpeg")
        return None
    if not available:
        return None
    return name in available


def _clock(seconds: float) -> str:
    """A point in the take as H:MM:SS, the way the Recording tab shows it."""
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def check_disk(path: Path) -> DiskSpace | None:
    """Free space on the volume holding ``path``, or None if unknowable."""
    probe = path if path.exists() else path.parent
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not probe.exists():
        # The walk reached the drive root and even that is not there: an
        # external drive that is not plugged in. Expected, not exceptional --
        # the Recording tab already says so in words, and this runs on a
        # timer, so a traceback per tick would bury the log for no gain.
        log.debug("No such volume to measure: %s", probe)
        return None
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        log.warning("Could not check free space on %s: %s", probe, exc)
        return None
    return DiskSpace(free_bytes=usage.free, total_bytes=usage.total)


def _ffmpeg_can_open(path: Path) -> bool | None:
    """Whether ffmpeg will open ``path`` and find video in it.

    None means the question could not be asked -- no bundled ffmpeg, or the
    probe itself failed -- which is not the same answer as "no" and must not be
    treated as one: refusing to hand over a recording because a probe misfired
    would be a worse failure than the one this guards.

    `ffmpeg -i` with no output named reads the header, prints the stream list
    and exits non-zero. It does not decode, so this costs the same on a
    four-hour take as on a one-second one -- measured at about a tenth of a
    second. The timeout is short because one caller is the writer thread
    (a restart verifies the part that just died), and a writer that stops
    writing is dropped frames.
    """
    exe = ffmpeg_path()
    if exe is None:
        return None
    try:
        # NOT text=True. That decodes with the locale codec -- cp1252 on a UK
        # or US Windows -- while ffmpeg echoes the path back as UTF-8. Any
        # character whose UTF-8 uses a byte cp1252 leaves undefined (0x81,
        # 0x8D, 0x8F, 0x90, 0x9D) then kills the reader thread, subprocess
        # hands back stderr=None, and `"Video:" in None` raises TypeError --
        # which is not an OSError or a SubprocessError, so it sails straight
        # out of the except below. Reproduced on this machine with a file
        # named show_a<U+0101>.mkv: text=True raised, this does not. Latvian
        # a-macron, Polish L-stroke and Czech c-caron are all in that set, and
        # the show name goes into the filename template.
        result = subprocess.run(
            [str(exe), "-hide_banner", "-i", str(path)],
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        log.exception("Could not inspect %s", path)
        return None
    return "Video:" in result.stderr


@dataclass
class DiskWarning:
    """What the UI needs to say about a disk that is filling up."""

    free_bytes: int
    #: Estimated minutes of recording left at the current write rate. None when
    #: there is not yet enough history to say.
    minutes_remaining: float | None
    critical: bool = False

    @property
    def free_gb(self) -> float:
        return self.free_bytes / 1_000_000_000

    def describe(self) -> str:
        if self.critical:
            return (
                f"Only {self.free_gb:.1f} GB left. Recording has been stopped "
                "so the file closes properly and nothing is lost."
            )
        if self.minutes_remaining is not None:
            return (
                f"{self.free_gb:.1f} GB left — about "
                f"{self.minutes_remaining:.0f} minutes of recording at this rate."
            )
        return f"{self.free_gb:.1f} GB left."


@dataclass
class RecordingPart:
    """One output file within a recording session.

    There is normally exactly one. A second appears only when ffmpeg died and
    the recorder continued into a new file.
    """

    path: Path
    #: Seconds from the start of the SESSION at which this part begins. Markers
    #: are timed against the session, so this is what converts them into times
    #: within this particular file.
    session_offset: float
    duration: float = 0.0
    frames: int = 0
    #: False when the file ended up empty, which a hard kill can cause.
    usable: bool = True
    #: Bytes on disk when this part was last measured. Kept so the take's size
    #: can be totalled without statting every finished part again.
    size_bytes: int = 0

    @property
    def exists(self) -> bool:
        return self.path.is_file()


@dataclass
class RecordingStats:
    frames_written: int = 0
    #: Every frame the recorder turned away while the take was recording,
    #: whatever the reason. The Recording tab, the finish message and the soak
    #: harness all read it as the total, so the total is what it stays.
    frames_dropped: int = 0
    #: Of frames_dropped, those lost to ffmpeg failing rather than to an
    #: encoder falling behind: turned away with no ffmpeg taking frames --
    #: from one being found dead until its replacement's frame count moved,
    #: the pause before a restart included -- or while the writer waited on
    #: one that then died or had to be killed. The rest were turned away while
    #: ffmpeg was encoding, too slowly. See Recorder._note_drop.
    frames_dropped_ffmpeg_failing: int = 0
    #: The longest run of ffmpeg failures in a row this take. A run ends when a
    #: part records for HEALTHY_PART_SECONDS. The finish message says ffmpeg
    #: "kept failing" from this, and not from how many files the take is in:
    #: two deaths hours apart, each recovered from, also leave it in three files.
    most_ffmpeg_failures_in_a_row: int = 0
    bytes_written: int = 0
    started_at: float = 0.0
    #: When the FIRST frame actually reached ffmpeg. This, not started_at, is
    #: the video's own t=0: the file is timed by when frames arrive
    #: (-use_wallclock_as_timestamps), and ffmpeg reads its first frame only
    #: once its audio input has opened, a few hundred milliseconds after start()
    #: was called and different on every take.
    first_frame_at: float = 0.0
    #: When the most recent frame reached ffmpeg.
    last_frame_at: float = 0.0
    output_path: Path | None = None
    #: What the whole take -- every part -- had put on disk when it was last
    #: measured, and when that was (perf_counter; 0.0 for never). Written by
    #: the recorder's own threads. Anyone may read them: reading them costs
    #: nothing, which is the point.
    bytes_on_disk: int = 0
    size_sampled_at: float = 0.0
    #: Frames ffmpeg itself duplicated and dropped to hold the output at a
    #: constant rate (-fps_mode cfr), over every part. From its progress lines,
    #: which in the bundled 8.1 build carry dup= and drop=. Not lost frames --
    #: a camera running slow is padded with repeats and a fast one thinned --
    #: but the record of how far the picture was bent to fit the rate.
    ffmpeg_duplicated: int = 0
    ffmpeg_dropped: int = 0
    #: Seconds of sound that reached the audio encoder, and seconds of timeline
    #: their timestamps spanned, over every stretch judged in every part. Both
    #: 0.0 for a take without sound. See _SoundDelivery.
    sound_delivered_seconds: float = 0.0
    sound_timeline_seconds: float = 0.0
    _recent: deque[float] = field(default_factory=lambda: deque(maxlen=90))
    #: (when, bytes_on_disk) pairs reaching back about RATE_WINDOW_SECONDS.
    _sizes: deque[tuple[float, int]] = field(default_factory=deque)

    @property
    def sound_delivered_share(self) -> float | None:
        """The share of its sound the audio input delivered, or None if unmeasured."""
        if self.sound_timeline_seconds <= 0:
            return None
        return self.sound_delivered_seconds / self.sound_timeline_seconds

    def note_size(self, when: float, total_bytes: int) -> None:
        """Record a measurement of what the take has put on disk."""
        self.bytes_on_disk = total_bytes
        self.size_sampled_at = when
        sizes = self._sizes
        sizes.append((when, total_bytes))
        # Keep just enough to span the window: the oldest goes only while the
        # one after it still reaches back far enough by itself.
        while len(sizes) > 2 and when - sizes[1][0] >= RATE_WINDOW_SECONDS:
            sizes.popleft()

    def note_frame(self) -> None:
        now = time.perf_counter()
        self.frames_written += 1
        if not self.first_frame_at:
            self.first_frame_at = now
        self.last_frame_at = now
        self._recent.append(now)

    @property
    def elapsed(self) -> float:
        """Seconds since the video began -- the timebase markers are taken on.

        Measured from the first frame written, not from start(), and 0.0 until
        one has been, so a marker taken while the take is still starting lands
        where the video starts rather than late. Measured, not reasoned: with a
        white flash burned into the frames at known moments, markers timed from
        started_at landed +0.581s from the flash at 3s, 7s and 12s alike,
        because start() then returned only after a 0.6s launch check. A
        constant offset on every marker in every take is exactly the kind of
        thing nobody notices until they are cutting -- and counting from
        started_at before the first frame would now not even be constant: that
        stretch is ffmpeg launching and opening its audio input, which differs
        take to take.
        """
        origin = self.first_frame_at
        return time.perf_counter() - origin if origin else 0.0

    @property
    def video_elapsed(self) -> float:
        """Seconds of footage actually written. Never more than the file holds."""
        if self.first_frame_at and self.last_frame_at > self.first_frame_at:
            return self.last_frame_at - self.first_frame_at
        return 0.0

    @property
    def write_fps(self) -> float:
        if len(self._recent) < 2:
            return 0.0
        if time.perf_counter() - self._recent[-1] > FRAME_RATE_STALE_AFTER:
            # Frames have stopped arriving. The deque is only appended to on a
            # successful write, so without this check the span never moves and
            # the property keeps returning the last healthy rate for as long as
            # the app is open -- measured, a steady "30.0 fps" over a camera
            # that had been dead for ten seconds. That is the one number an
            # operator would use to notice exactly this, so it must not lie.
            return 0.0
        span = self._recent[-1] - self._recent[0]
        return (len(self._recent) - 1) / span if span > 0 else 0.0

    @property
    def file_size_mb(self) -> float:
        """The take's size on disk, every part included, as last measured.

        Never touches the disk, so it is safe on the GUI thread. It used to
        stat the current part on every call, and the Recording tab calls it
        every 500 ms: on a recordings folder whose network share has dropped
        that is the kind of call measured at half a minute before it raises,
        freezing the window the way _DiskProbe in record_panel.py exists to
        prevent. It also counted only the current part, so after a
        continuation a 12.7 GB take read as 670 MB. The recorder measures it on
        its own threads instead; size_sampled_at says how fresh it is.
        """
        return self.bytes_on_disk / 1_000_000

    @property
    def megabytes_per_hour(self) -> float:
        return self.bytes_per_second * 3600 / 1_000_000

    @property
    def bytes_per_second(self) -> float:
        """Measured write rate over the whole take, for estimating what will fit.

        Measured rather than assumed: a quiet stage at Compact quality and a
        busy one at Archive differ by more than an order of magnitude, and a
        generic estimate would be wrong in whichever direction matters.

        Every part, over the time up to when the size was measured. It used to
        be the CURRENT part over the whole take, which after a continuation is
        minutes of bytes over hours: ffmpeg dying at 3:00 on a 4 GB/hour take,
        and the warning firing at 3:10, announced about 2,822 minutes left to a
        recording that stopped itself 148 minutes later.
        """
        # Gated on its own origin, as it always was, and not on elapsed: elapsed
        # now reads 0.0 until the first frame lands, so that markers taken while
        # a take starts land at its start. That is a rule for markers, and the
        # estimate's five seconds are left as they were.
        origin = self.first_frame_at or self.started_at
        if not origin or not self.size_sampled_at or time.perf_counter() - origin < 5:
            return 0.0
        span = self.size_sampled_at - origin
        return self.bytes_on_disk / span if span > 0 else 0.0

    @property
    def recent_bytes_per_second(self) -> float:
        """Write rate over the last few minutes; 0.0 until that is long enough.

        The whole-take average hides a change of pace, and a tech changes pace
        all the time. Two hours with the house dark and then a lit act
        averaged 0.36 MB/s while the stage was writing 0.96, so a warning
        promised 470 minutes and the take stopped itself after 174.
        """
        sizes = self._sizes
        try:
            (then, before), (now, after) = sizes[0], sizes[-1]
        except IndexError:
            return 0.0
        span = now - then
        if span < RATE_WINDOW_MIN_SPAN:
            return 0.0
        return max(0.0, (after - before) / span)

    @property
    def drop_percent(self) -> float:
        """Share of the frames offered to the recorder that it had to drop."""
        offered = self.frames_written + self.frames_dropped
        return 100.0 * self.frames_dropped / offered if offered else 0.0


class _RepeatGate:
    """Which of ffmpeg's lines get a line in the log, and how often.

    The first line of each kind goes straight through. Repeats are counted,
    and reported with their count at most once per FFMPEG_REPEAT_INTERVAL;
    whatever is still unreported when the take ends comes back from
    unreported(). A kind is the line with its numbers and addresses taken out,
    so a warning quoting a different figure each time, or the same warning
    from a restarted ffmpeg at a different "@ 000001c3a5f0e2c0", is one kind.

    Locked, because two reader threads can overlap for a moment: after a
    restart the new ffmpeg's reader starts while the dead one's may still be
    handing over its last lines.
    """

    _VARIABLE = re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{8,}\b|\d+(?:\.\d+)?")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: kind -> [when it was last logged, repeats since then, latest line]
        self._kinds: dict[str, list] = {}
        self._others = 0
        self._others_logged_at = float("-inf")

    @classmethod
    def kind(cls, line: str) -> str:
        """The line with its numbers and addresses taken out."""
        return cls._VARIABLE.sub("#", line)

    def offer(self, line: str) -> str | None:
        """The text to log for this line, or None to log nothing yet."""
        now = time.monotonic()
        kind = self.kind(line)
        with self._lock:
            seen = self._kinds.get(kind)
            if seen is None:
                if len(self._kinds) < FFMPEG_LINE_KINDS:
                    self._kinds[kind] = [now, 0, line]
                    return line
                self._others += 1
                if now - self._others_logged_at < FFMPEG_REPEAT_INTERVAL:
                    return None
                others, self._others = self._others, 0
                self._others_logged_at = now
                if others == 1:
                    return line
                return f"{line} (and {others - 1} earlier lines of other kinds, not shown)"
            seen[1] += 1
            seen[2] = line
            since = now - seen[0]
            if since < FFMPEG_REPEAT_INTERVAL:
                return None
            repeats = seen[1]
            seen[0], seen[1] = now, 0
            return f"{line} (said {repeats} times in the last {since:.0f}s)"

    def unreported(self) -> list[str]:
        """Repeats that were never reported, as lines for the end of the take."""
        with self._lock:
            said = [
                f"{latest} (said {repeats} more time{'' if repeats == 1 else 's'} "
                f"before the take ended)"
                for _, repeats, latest in self._kinds.values()
                if repeats
            ]
            for seen in self._kinds.values():
                seen[1] = 0
            if self._others:
                said.append(
                    f"{self._others} further lines of other kinds were not shown"
                )
                self._others = 0
        return said


#: Lines ffmpeg prints at warning level on a healthy take, logged at INFO.
#:
#: "Guessed Channel Layout: stereo" comes once at the start of a take whose
#: DirectShow audio input declares two channels but not their layout -- the
#: development laptop's microphone array, in an hour-long overnight soak with
#: the sound in the file correct. Logged at WARNING it made every healthy take
#: on that input look like it had something wrong with it, and a warning that
#: always appears teaches whoever reads the log to skip the warnings. Only
#: lines seen on healthy takes belong here.
_FFMPEG_ROUTINE = ("Guessed Channel Layout",)


def _ffmpeg_level(line: str) -> int:
    """The level one of ffmpeg's lines is logged at.

    ffmpeg runs at -loglevel warning, so everything it prints apart from its
    progress lines is a warning or worse by its own reckoning. Measured on the
    bundled build: a healthy libx264 take prints nothing else at all, so this
    adds nothing to a take that is going well -- apart from the routine lines
    in _FFMPEG_ROUTINE, which are still logged, at INFO.
    """
    lowered = line.lower()
    if "error" in lowered or "failed" in lowered or "invalid" in lowered:
        return logging.ERROR
    if any(routine in line for routine in _FFMPEG_ROUTINE):
        return logging.INFO
    return logging.WARNING


#: ffmpeg's closing lines, which say only that something before them failed.
#:
#: "Nothing was written into output file, because at least one of its streams
#: received no packets" is what ffmpeg prints whenever the output header was
#: never written, whatever stopped it. It ended every failure of the soak's
#: h264_mf take. Measured on the bundled 8.1.2 build with lavfi inputs, it also
#: ended a run whose libx264 encoder refused its options, and one whose audio
#: chain could not be set up while the video was fine. So it cannot tell one
#: failure from another, and is left out when they are compared. "Conversion
#: failed!" is in the same binary, though none of those runs printed it. See
#: Recorder._error_kinds.
_FFMPEG_EPITAPHS = (
    "Nothing was written into output file",
    "Conversion failed!",
)

#: Sound, by its own timestamps, that is not judged at the start of each file.
#: An input can hand over a burst, or nothing, while it opens.
SOUND_SETTLE_SECONDS = 5.0
#: How much of the timeline one window of the judgement covers.
SOUND_WINDOW_SECONDS = 10.0
#: How many windows the judgement covers, rolling. Judging a RUN of short
#: windows instead missed every input whose gaps are longer than a window: 11
#: seconds of sound and 11 seconds of nothing, repeated, is half the sound and
#: never two short windows in a row. Measured on that pattern, 6 on 6 off was
#: reported 25 s in, 10 on 10 off at 40 s, and 11 on 11 off -- and anything
#: slower -- never at all, though the take's own figure saw it plainly. Three
#: windows is half a minute of sound, over which a single gap of up to three
#: seconds still counts as delivering.
SOUND_JUDGED_WINDOWS = 3
#: A judgement below this share of the sound its timestamps span is short. A
#: healthy input measures 1.000, and the USB webcam microphone that prompted
#: the check about 0.5.
SOUND_SHORT_BELOW = 0.90
#: Wall time with nothing at all arriving from an input that had been
#: delivering, before it counts as silence. Sound that stops leaves no
#: statistics behind to be noticed by, so it is timed from outside; see
#: Recorder._check_the_sound_is_arriving.
SOUND_SILENCE_SECONDS = 10.0
#: Sound, by its own timestamps, that must be nothing but digital silence
#: before the input is called dead. Fifteen seconds is longer than any pause a
#: stage holds -- a blackout between scenes, a house waiting on a cue -- and
#: short enough that the booth hears about it inside the first scene rather
#: than at the interval. It is not the same fault as SOUND_SILENCE_SECONDS
#: above: there the statistics stop altogether, here they keep coming and say
#: the samples are zeros.
SOUND_SILENT_SECONDS = 15.0
#: A second whose peak is below this is digital silence rather than a quiet
#: room. It has to clear the laptop's microphone array as well as nothing at
#: all: gated by its voice effects, that array handed over exact zeros except
#: while somebody spoke, and even the speech it did pass peaked at only
#: -84.3 dBFS -- two LSB of 16-bit. A threshold under that figure would count
#: every line of dialogue as the input working and never say a word. Above it,
#: a live input's own noise floor is still far clear of -80 however still the
#: house is, so nothing but a muted, gated or unconnected input lands under.
SOUND_SILENT_BELOW_DBFS = -80.0


class _SoundDelivery:
    """How much of its sound an audio input is delivering, frame by frame.

    Fed from ffmpeg's statistics for each frame of sound reaching the encoder
    (see encoder.sound_stats_args): where the frame starts and how long it
    lasts, in seconds. Sound missing at the input leaves its time in the
    timestamps with no samples in it, so the sound in a stretch of timeline,
    over the stretch's length, is the share delivered. Gaps are never counted
    one by one: healthy takes have them too, from timing jitter, and lose a few
    hundredths of a per cent to them.

    Sound that stops altogether leaves nothing to count. A window closes only
    when a frame arrives, so an input that died mid-take was scored as having
    delivered every bit of its sound, and the take's summary said "100.0% of
    the sound arrived" over a file that was silent from the interval on. That
    is worse than not measuring at all, so note_silence closes windows for
    stretches that held no sound, timed from outside.
    """

    def __init__(self) -> None:
        self._first_start: float | None = None
        self._window_start: float | None = None
        self._window_sound = 0.0
        #: The last few windows closed, as (sound, span).
        self._judged: deque[tuple[float, float]] = deque(maxlen=SOUND_JUDGED_WINDOWS)
        #: Seconds of sound, and of the timeline it fell in, over every window
        #: judged so far.
        self.delivered = 0.0
        self.timeline = 0.0
        #: Whether an unreadable statistics line has been logged for this file.
        self.unreadable_said = False

    def note(self, start: float, duration: float) -> float | None:
        """Take one frame of sound.

        Returns the share delivered over the windows judged, once they fall
        short of SOUND_SHORT_BELOW, and None otherwise.
        """
        if self._first_start is None:
            self._first_start = start
        if start < self._first_start + SOUND_SETTLE_SECONDS:
            return None
        if self._window_start is None or start < self._window_start:
            # The first frame judged, or timestamps that went backwards: begin
            # the window here rather than judge a span that makes no sense.
            self._window_start, self._window_sound = start, 0.0
        self._window_sound += duration
        end = start + duration
        span = end - self._window_start
        if span < SOUND_WINDOW_SECONDS:
            return None
        sound, self._window_sound = self._window_sound, 0.0
        # The next window starts where this one ended, so a gap between two
        # frames always falls inside one window or the other.
        self._window_start = end
        return self._judge(sound, span)

    def note_silence(self, seconds: float) -> float | None:
        """Take a stretch in which nothing arrived at all, timed from outside.

        The window part-way through is abandoned rather than closed: what is
        known is that this stretch held no sound, and a window whose statistics
        stopped coming has no end of its own to be measured against.
        """
        if seconds <= 0:
            return None
        self._window_start, self._window_sound = None, 0.0
        return self._judge(0.0, seconds)

    def _judge(self, sound: float, span: float) -> float | None:
        """Close one window, and judge the last SOUND_JUDGED_WINDOWS of them.

        Rolling rather than consecutive, and never reset on a good window, so
        that an input alternating sound and silence is judged on what it
        delivers rather than on how its gaps happen to line up with the
        windows. Saying it once a take is the recorder's job, not this one's,
        which is why every short judgement is reported: the recorder can be in
        no state to pass the first one on -- ffmpeg's statistics start arriving
        while the take is still STARTING -- and reporting once would lose it
        for the whole file.
        """
        self.delivered += sound
        self.timeline += span
        self._judged.append((sound, span))
        if len(self._judged) < SOUND_JUDGED_WINDOWS:
            return None
        judged_sound = sum(part for part, _ in self._judged)
        judged_span = sum(part for _, part in self._judged)
        if judged_span <= 0:
            return None
        share = judged_sound / judged_span
        return share if share < SOUND_SHORT_BELOW else None


def _seconds_or_none(text: str) -> float | None:
    """A timestamp off one of ffmpeg's lines, or None where it has not got one.

    ffmpeg writes "N/A" for a frame it cannot place in the timeline. Anything
    else unreadable arrives the same way, and a level whose place in the take
    is unknown is left out rather than guessed at.
    """
    try:
        at = float(text)
    except ValueError:
        return None
    return at if math.isfinite(at) else None


class _SoundSilence:
    """Whether an audio input is handing over anything but digital silence.

    Fed from the peak level ffmpeg reports once a second for the sound going
    into the encoder (see encoder.sound_level_chain). _SoundDelivery above
    cannot see this fault and never will: an input handing over exact zeros
    delivers every sample its timestamps promise, so it scores 1.000 while the
    file it is describing is silent. The clap test finds nothing either -- it
    is looking for a peak in two inputs, and one of them has none.

    Judged on the reports' own timestamps rather than on how many arrive: the
    frames are about a second each, and exactly a second only when the input's
    rate was known when ffmpeg was launched.
    """

    def __init__(self) -> None:
        self._first_at: float | None = None
        #: Where the current run of silent seconds began, or None if the last
        #: second judged held sound.
        self._silent_from: float | None = None
        #: Where the peak that comes next was measured. ametadata prints a
        #: line naming the frame before the line carrying its value, so the
        #: two arrive one after the other and are read the same way.
        self._at: float | None = None

    def note_frame(self, at: float | None) -> None:
        """Take the line naming the frame whose peak follows.

        ``at`` is None when its timestamp could not be read, which leaves the
        peak after it judging nothing: a level with no place in the take
        cannot be counted towards a stretch of it.
        """
        self._at = at

    def note_peak(self, peak_dbfs: float) -> bool:
        """Take one second's peak level.

        Returns whether the input has now handed over nothing but digital
        silence for SOUND_SILENT_SECONDS. Goes on returning True for every
        silent second after that, the way _SoundDelivery._judge goes on
        reporting: saying it once a take is the recorder's job, and the
        recorder can be in no state to pass the first one on.
        """
        at, self._at = self._at, None
        if at is None:
            return False
        if self._first_at is None:
            self._first_at = at
        if at < self._first_at + SOUND_SETTLE_SECONDS:
            # The same settle the delivery check leaves: an input can hand
            # over silence, or a burst, while it is still opening.
            return False
        if peak_dbfs >= SOUND_SILENT_BELOW_DBFS:
            self._silent_from = None
            return False
        if self._silent_from is None or at < self._silent_from:
            # The first silent second, or timestamps that went backwards:
            # start the stretch here rather than measure one that makes no
            # sense.
            self._silent_from = at
            return False
        return at - self._silent_from >= SOUND_SILENT_SECONDS


#: What the writer is left holding when the queue had nothing for it: neither a
#: frame nor the stop sentinel, which is None.
_NOTHING = object()


class Recorder:
    """Feeds frames to a single ffmpeg process."""

    def __init__(
        self,
        *,
        on_state_change: Callable[[RecorderState, str], None] | None = None,
        on_disk_warning: Callable[[DiskWarning], None] | None = None,
        on_sound_warning: Callable[[str], None] | None = None,
    ) -> None:
        self.state = RecorderState.IDLE
        self.detail = ""
        self.stats = RecordingStats()
        self.settings: OutputSettings | None = None

        #: Warn below this many bytes free. Set from the show file.
        self.low_disk_bytes = 20_000_000_000
        #: STOP the recording at or below this many bytes free.
        #:
        #: Stopping early is deliberate. The remaining space is not spare: the
        #: file has to be closed, and embedding chapters rewrites it, which
        #: briefly needs its size again. Running to the last byte would risk
        #: all of that.
        self.stop_below_bytes = 10_000_000_000
        self.disk: DiskWarning | None = None
        self._warned_low = False
        self._next_disk_check = 0.0

        self._on_state_change = on_state_change
        self._on_disk_warning = on_disk_warning
        #: Told, from the thread reading ffmpeg's stdout, when the audio input
        #: is delivering too little of its sound. Once a take.
        self._on_sound_warning = on_sound_warning
        self._process: subprocess.Popen[bytes] | None = None
        #: One second of frames at 30 fps. It was 90 -- three seconds -- and
        #: the 30-minute soak described at capture.ENCODER_QUEUE_DEPTH saw this
        #: queue peak at 18, all of it in the first two minutes while ffmpeg
        #: was starting, and sit at 0 for the remaining 28 (p99 0, median 0).
        #: 30 clears that startup burst with room over it.
        #:
        #: Not smaller, and not much larger: the two queues together are frames
        #: held in memory, 6.2 MB each at 1080p and 25 MB at 4K, and for the
        #: reason given at ENCODER_QUEUE_DEPTH the ones held through a stall
        #: are dropped by the muxer anyway rather than recorded late.
        self._queue: queue.Queue[Frame | None] = queue.Queue(maxsize=30)
        self._writer: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        #: Reads the current ffmpeg's sound statistics from its stdout, in a take
        #: with sound. See _read_sound_stats.
        self._stdout_thread: threading.Thread | None = None
        #: What the current ffmpeg's sound statistics show so far, and the
        #: lock over it: the thread reading them and the watchdog both reach it.
        self._sound = _SoundDelivery()
        #: And what the peak levels on the same pipe show, which is a separate
        #: fault: see _SoundSilence.
        self._sound_silence = _SoundSilence()
        self._sound_lock = threading.Lock()
        #: perf_counter when a line of them last arrived, None until the first
        #: one does. What _check_the_sound_is_arriving times silence from.
        self._sound_heard_at: float | None = None
        #: Whether this take has said its sound is arriving with gaps.
        self._sound_short_said = False
        #: Whether this take has said its input is delivering digital silence.
        self._sound_silent_said = False
        self._stderr_tail: deque[str] = deque(maxlen=40)
        #: Emergency abort only. A normal stop sends a sentinel through the
        #: queue instead, so everything already queued still gets written.
        self._abort = threading.Event()
        self._lock = threading.RLock()
        self._frame_bytes = 0
        #: Set by the disk check when space runs out. The writer finishes the
        #: frames it has and closes cleanly rather than being killed.
        self._disk_stop_requested = False

        #: Continue into a new file if ffmpeg dies, rather than ending the
        #: session. On by default: losing two acts to a hiccup is far worse
        #: than a short gap and an extra file.
        self.restart_on_failure = True
        #: Every file this session has written, in order.
        self.parts: list[RecordingPart] = []
        self._part_started_at = 0.0
        #: perf_counter of the first frame written to the CURRENT part, which
        #: is where that file's own timeline starts.
        self._part_first_write = 0.0
        #: True while the camera has stopped delivering frames.
        self._stalled = False
        self._restarts = 0
        self._base_path: Path | None = None
        self._capture_geometry: tuple[int, int, float] = (0, 0, 0.0)
        #: ffmpeg failures in a row; see MAX_RESTARTS. _restarts above is every
        #: restart in the take, for the record.
        self._consecutive_failures = 0
        #: Seconds into the take at which the audio input was given up, or
        #: None while the take still has its sound (or never had any).
        self.audio_dropped_at: float | None = None

        #: perf_counter when the write now in flight began, 0.0 when none is.
        #: Written by the writer, read by the watchdog.
        self._write_began = 0.0
        #: Whether start() launched ffmpeg without a frame, after waiting out
        #: START_READY_WAIT for one; see _say_ffmpeg_is_still_starting.
        self._launched_without_a_frame = False
        #: The ffmpeg the watchdog has said is not taking frames, until a write
        #: to that same process lands. The process and not a flag, because a
        #: flag outlived the process it was about: once a stuck ffmpeg had been
        #: killed and replaced, the replacement's first frame announced "ffmpeg
        #: is taking frames again" -- a recovery that never happened -- and
        #: overwrote the detail saying which file the take had moved to, and
        #: that it had gone on WITHOUT SOUND when that had just been decided.
        self._stall_reported_for: subprocess.Popen[bytes] | None = None
        #: How long the write had been stuck when the watchdog killed ffmpeg
        #: for it, so the restart that follows can say why; 0.0 otherwise.
        self._killed_for_stalling = 0.0
        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        #: The CURRENT ffmpeg's own counters from its progress lines -- frame,
        #: dup, drop -- and when the frame count last moved.
        self._progress = (0, 0, 0)
        self._progress_moved_at = 0.0
        self._stderr_gate = _RepeatGate()

        self._next_size_sample = 0.0
        #: Whether the current part's file has been seen on disk yet. Before
        #: that, a file that is not there is one ffmpeg has not made yet.
        self._part_seen_on_disk = False
        #: The lowest LOW_DISK_REANNOUNCE_MINUTES step already announced.
        self._warned_step: int | None = None
        #: Frames still to write after the disk floor was reached: the ones
        #: that were already queued at that moment.
        self._disk_backlog = 0
        #: The ffmpeg the writer last found dead or had to kill, from then until
        #: a replacement's frame count moves; None while one is taking frames.
        #: The process and not a flag, for the reason _stall_reported_for
        #: gives. A flag could be cleared by the dead one's own progress line
        #: arriving late, and the frames turned away while it was replaced were
        #: then put down to an encoder falling behind. See _note_drop.
        self._failed_ffmpeg: subprocess.Popen[bytes] | None = None
        #: Frames turned away while ffmpeg was running that nothing has yet
        #: shown the reason for. See _note_drop.
        self._drops_pending = 0
        #: For each reason frames are dropped -- True for ffmpeg failing, False
        #: for an encoder falling behind -- the count last logged, and when.
        self._drops_reported: dict[bool, tuple[int, float]] = {}
        #: The kinds of error ffmpeg gave with sound before the take was tried
        #: without it, with nothing pointing at the audio input, while that
        #: attempt is the one running; None otherwise. See _verdict_on_the_sound.
        self._sound_trial_after: frozenset[str] | None = None

        #: Frames to write before anything more is taken off the queue: the
        #: frame a restart was launched on, or the first live frame found behind
        #: a stale backlog. The writer's own; see _write_frames.
        self._held: deque[Frame | None] = deque()
        #: Writes that have landed in the CURRENT ffmpeg's pipe. The writer
        #: counts them, start() and the watchdog read them, and each launch
        #: starts them again from zero.
        self._landed_writes = 0
        #: Whether the current ffmpeg was launched with an audio input.
        self._process_has_audio = False
        #: Set whenever something start() is waiting on may have happened.
        self._launch_news = threading.Event()
        #: From the moment start() has checked the audio input and is ready to
        #: launch ffmpeg: frames offered while the take is STARTING are taken.
        #: See submit().
        self._start_armed = False
        #: From that same moment until the take's first frame lands, or its
        #: first ffmpeg dies: a full queue gives up its oldest frame, uncounted.
        #: See _queue_before_the_take_begins.
        self._take_not_begun = False
        #: What that has cost, until it is logged.
        self._backlog_given_up = 0
        #: Set by the writer when it finds ffmpeg dead while the take is STARTING.
        self._died_while_starting = False
        #: The ffmpeg a take was announced over while its audio input was still
        #: opening, until that ffmpeg's first frame lands.
        self._opening_announced_for: subprocess.Popen[bytes] | None = None
        #: How long the current part's first write took to land: for an ffmpeg
        #: with an audio input, how long the input took to open.
        self._first_write_waited = 0.0
        #: How long the first write had waited for the audio input when the
        #: watchdog killed ffmpeg over it, so the restart can say why; 0.0
        #: otherwise.
        self._killed_for_not_opening = 0.0
        #: ffmpeg deaths in a row that were such kills. See _reason_to_drop_audio.
        self._opening_kills_in_a_row = 0
        #: What to say about a part whose sound came out late against its
        #: picture, until it has been said on screen. See
        #: _note_if_the_sound_is_late.
        self._sound_late_said: str | None = None

    # ------------------------------------------------------------------ state

    @property
    def is_recording(self) -> bool:
        return self.state in (RecorderState.RECORDING, RecorderState.STARTING)

    @property
    def is_writing(self) -> bool:
        """Whether picture is going into the file right now.

        What Wer's sACN status follows, and deliberately narrower than
        is_recording. STARTING is not enough: the take can still fail. And
        RECORDING stays set while nothing is written -- a camera that has
        stalled, the wait before a restart, a disk stop still draining -- so a
        frame must have been written within STALL_TIMEOUT, the same threshold
        the recorder's own "nothing is being written" uses. A Blackmagic that
        has lost its signal still writes black frames, and still counts.

        Safe from any thread: state is set under a lock, and the rest are
        single reads.
        """
        if self.state is not RecorderState.RECORDING or self._disk_stop_requested:
            return False
        last = self.stats.last_frame_at
        return bool(last) and time.perf_counter() - last <= STALL_TIMEOUT

    def _set_state(
        self,
        state: RecorderState,
        detail: str = "",
        *,
        only_if: RecorderState | None = None,
    ) -> bool:
        """Change state and tell the listener. False if ``only_if`` did not hold.

        ``only_if`` is for threads other than the writer. The writer is what
        ends a take, so its own changes cannot race each other, but the
        watchdog's can: an unconditional "still recording, but..." landing just
        after the writer gave up would put a finished take back into RECORDING,
        and a take that believes it is recording is the worst thing this class
        can report.
        """
        with self._lock:
            if only_if is not None and self.state is not only_if:
                return False
            self.state = state
            self.detail = detail
        log.info("Recorder -> %s %s", state.value, detail)
        if self._on_state_change is not None:
            try:
                self._on_state_change(state, detail)
            except Exception:  # noqa: BLE001
                log.exception("Recorder state listener raised")
        return True

    @property
    def ffmpeg_output(self) -> list[str]:
        """ffmpeg's recent stderr, for the UI. Its own words beat an exit code."""
        return list(self._stderr_tail)

    # ------------------------------------------------------------------ start

    def start(
        self,
        settings: OutputSettings,
        output_path: Path,
        *,
        capture_width: int,
        capture_height: int,
        capture_fps: float,
    ) -> bool:
        """Launch ffmpeg and begin accepting frames. False if it could not start.

        The caller must already be offering frames. They are taken from the
        moment the audio input has been checked, not from when this returns:
        ffmpeg opens its audio input and then reads the frame waiting in its
        pipe, and the whole take's sync rests on one being there (see
        build_ffmpeg_command). So a take with sound launches ffmpeg only once a
        frame is queued (_wait_for_a_frame_to_launch_on), and this returns once
        ffmpeg shows it is recording, or that it will not, and no more than
        START_READY_WAIT after it began waiting on either; see
        _wait_until_recording.
        """
        if self.is_recording:
            log.warning("start() while already recording")
            return False

        self._set_state(RecorderState.STARTING)
        self.settings = settings
        self.stats = RecordingStats(
            started_at=time.perf_counter(), output_path=output_path
        )
        self._frame_bytes = capture_width * capture_height * 3
        self._base_path = output_path
        self._capture_geometry = (capture_width, capture_height, capture_fps)
        self.parts = []
        self._restarts = 0
        self._consecutive_failures = 0
        self.audio_dropped_at = None
        self._sound_short_said = False
        self._sound_silent_said = False
        self._stalled = False
        self._part_started_at = self.stats.started_at
        self._part_first_write = 0.0
        self._stderr_tail.clear()
        self._stderr_gate = _RepeatGate()
        self._abort.clear()
        self.disk = None
        self._disk_stop_requested = False
        self._disk_backlog = 0
        self._warned_low = False
        self._warned_step = None
        self._next_disk_check = time.perf_counter() + DISK_CHECK_INTERVAL
        self._next_size_sample = 0.0
        self._failed_ffmpeg = None
        self._drops_pending = 0
        self._drops_reported = {}
        self._sound_trial_after = None
        self._write_began = 0.0
        self._stall_reported_for = None
        self._killed_for_stalling = 0.0
        self._killed_for_not_opening = 0.0
        self._opening_kills_in_a_row = 0
        self._died_while_starting = False
        self._opening_announced_for = None
        with self._lock:
            self._start_armed = False
            self._take_not_begun = False
            self._backlog_given_up = 0
            self._sound_late_said = None
        self._held.clear()
        self._discard_queued_frames()           # anything left from the last take

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_state(
                RecorderState.ERROR,
                f"Cannot create {output_path.parent}: {exc}",
            )
            return False

        problem = _validate_audio_device(settings.audio_device)
        if problem:
            self._set_state(RecorderState.ERROR, problem)
            return False

        # One budget for everything this waits on from here: it runs on the
        # interface thread. See START_READY_WAIT.
        deadline = time.perf_counter() + START_READY_WAIT
        with self._lock:
            # Frames are taken from here on; see submit(). Not before the audio
            # input was checked, which can take DirectShow fifteen seconds.
            self._start_armed = True
            self._take_not_begun = True
        self._launched_without_a_frame = bool(
            settings.audio_device
        ) and not self._wait_for_a_frame_to_launch_on(deadline)
        if not self._launch(output_path, start_writer=True):
            # _launch has said why. Frames were being taken by then.
            self._abandon_start()
            return False
        process = self._process
        # Before the wait, which the first frame can land during: it anchors
        # itself to this part.
        self.parts.append(RecordingPart(output_path, session_offset=0.0))

        ready, said = self._wait_until_recording(deadline)
        if ready and self._set_state(
            RecorderState.RECORDING, said, only_if=RecorderState.STARTING
        ):
            if said and self._landed_writes and process is not None:
                # Announced over an input still opening, and the first frame
                # landed in the moment between deciding that and saying it.
                self._say_the_audio_input_opened(process)
            self._say_the_sound_is_late()
            self._say_what_the_backlog_cost()
            return True
        if not ready:
            self._set_state(RecorderState.ERROR, said, only_if=RecorderState.STARTING)
        # Otherwise the writer found ffmpeg dead first, and has said why.
        self._abandon_start()
        return False

    def _launch(self, output_path: Path, *, start_writer: bool = False) -> bool:
        """Start an ffmpeg process writing to ``output_path``.

        Used both to begin a recording and to continue one into a new file
        after a failure. On a restart the writer thread is already running and
        must not be started again -- it simply finds a fresh process to write
        to.

        False only when ffmpeg could not be launched at all. Whether it then
        works is not waited for here. start() waits for that itself
        (_wait_until_recording); a restart finds out from its first write, and
        a process already dead by then is one more failure in a row for
        _handle_ffmpeg_death to count. That matters now ffmpeg opens its audio
        input before reading a frame: one whose input has gone exits within
        tens of milliseconds, and the 0.6 s check that used to sit here would
        have caught it and set ERROR -- ending the take at the first restart
        over a lost microphone, past MAX_RESTARTS, RESTART_DELAYS and carrying
        on without sound.
        """
        settings = self.settings
        if settings is None:
            return False
        width, height, fps = self._capture_geometry

        try:
            command = build_ffmpeg_command(
                settings,
                capture_width=width,
                capture_height=height,
                capture_fps=fps,
                output_path=output_path,
            )
        except RuntimeError as exc:
            self._set_state(RecorderState.ERROR, str(exc))
            return False

        log.info("ffmpeg: %s", " ".join(command))
        self._stderr_tail.clear()
        # A new process counts its frames from zero.
        self._progress = (0, 0, 0)
        self._progress_moved_at = 0.0
        # Before the new process is current, so the watchdog never reads it
        # with the last one's count.
        self._landed_writes = 0
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                # Only a take with sound has anything on stdout: its sound
                # statistics. Piped, it has to be read for as long as ffmpeg
                # runs, for the same reason stderr does; see _read_sound_stats.
                stdout=subprocess.PIPE if settings.audio_device else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                bufsize=0,
            )
        except OSError as exc:
            self._set_state(RecorderState.ERROR, f"Could not start ffmpeg: {exc}")
            return False

        self._process = process
        self._process_has_audio = bool(settings.audio_device)
        # Replaced after the process, so a late line from the ffmpeg before,
        # which no longer matches _process, cannot land in the new count.
        with self._sound_lock:
            self._sound = _SoundDelivery()
            self._sound_silence = _SoundSilence()
            self._sound_heard_at = None
        self.stats.output_path = output_path
        self._part_started_at = time.perf_counter()
        self._part_first_write = 0.0
        self._part_seen_on_disk = False

        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="ffmpeg-stderr", daemon=True
        )
        self._stderr_thread.start()
        self._stdout_thread = None
        if process.stdout is not None:
            self._stdout_thread = threading.Thread(
                target=self._read_sound_stats, args=(process,),
                name="ffmpeg-sound-stats", daemon=True,
            )
            self._stdout_thread.start()

        if start_writer:
            self._writer = threading.Thread(
                target=self._write_frames, name="ffmpeg-writer", daemon=True
            )
            self._writer.start()
            # Its own event rather than one shared across takes: a watchdog
            # left over from a start that failed must not be woken back up by
            # the next take clearing a flag they both hold.
            self._watchdog_stop = threading.Event()
            self._watchdog = threading.Thread(
                target=self._watch_writes,
                args=(self._writer, self._watchdog_stop),
                name="ffmpeg-watchdog", daemon=True,
            )
            self._watchdog.start()

        return True

    def _wait_for_a_frame_to_launch_on(self, deadline: float) -> bool:
        """Hold a take with sound's first launch until a frame is queued for it.

        ffmpeg opens its audio input, then reads the frame waiting in its pipe,
        and times the sound from its first sample and the picture from that
        read (see build_ffmpeg_command). With no frame waiting, the read waits
        for the next one, and the whole file's sound is late by that wait. A
        camera at 30 fps delivers one long before the fastest open measured,
        about 0.37 s, but the Blackmagic with nothing reaching its input sends a
        placeholder at 1.0 fps (capture.NO_SIGNAL_RATE): a take started over one
        launched ffmpeg with frames flowing and none in hand, and its sound was
        up to a second late for as long as it ran, with nothing said. A restart
        with sound waits the same way (_frame_to_restart_on).

        False when none came by ``deadline``. ffmpeg is launched without one
        then -- the picture is the part that cannot be had again, and a camera
        that has stalled at Record may come back -- and the writer says so if
        the sound came out late (_note_if_the_sound_is_late).
        """
        while True:
            # Cleared before looking, as in _wait_until_recording.
            self._launch_news.clear()
            if self._queue.qsize():
                return True
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            self._launch_news.wait(min(remaining, 0.05))
        settings = self.settings
        log.warning(
            "No frame had reached the recorder %.1fs after it was ready to "
            "launch ffmpeg; launching it without one. If the audio input %r "
            "opens before the first frame arrives, the sound will be late "
            "against the picture by the difference, and that is said when the "
            "frame lands.",
            START_READY_WAIT, settings.audio_device if settings is not None else None,
        )
        return False

    def _wait_until_recording(self, deadline: float) -> tuple[bool, str]:
        """Wait for the ffmpeg start() launched to show it is recording.

        (True, what to announce the take with) when it is -- or might be, once
        ``deadline``, START_READY_WAIT from when start() began waiting, has
        passed -- and (False, why) when it will not. Read
        from the pipe, because ffmpeg says nothing either way: at -loglevel
        warning it prints nothing on a healthy start, and nothing while an
        audio input hangs opening. Raising the level for a line to wait on
        would put every line it prints in the log; see _ffmpeg_level.

        - ffmpeg exits, prints a line in _FATAL_MARKERS, or is found dead by
          the writer, before its second frame lands: it will not record. An
          audio input that cannot be opened lands here, since ffmpeg opens it
          before reading a frame: with a stand-in for one, ffmpeg was gone
          26 ms after launch and the take refused 56 ms after start().
        - Its second frame lands: it is recording. ffmpeg reads the first as
          the last step of opening its inputs, and cannot read the second until
          it has found its encoder and opened the output file. Measured with
          the bundled 8.1.2 build: an unknown encoder, and an output it could
          not open, were both reported 1-4 ms after the first frame landed, and
          the second write failed. That does not cover the encoder
          initialising, which happens on its first frame inside ffmpeg: an
          NVENC driver refusing, or Media Foundation failing to write a header,
          still fails once the take is recording and restarts from there.
        - START_READY_WAIT passes first: announced anyway, for the reasons
          given there.
        """
        process = self._process
        writer = self._writer
        if process is None or writer is None:
            return False, self.detail or "ffmpeg did not start."
        while True:
            # Cleared before looking, so news arriving while this looks is not
            # slept through.
            self._launch_news.clear()
            if self.state is not RecorderState.STARTING:
                return False, self.detail or "ffmpeg stopped while it was starting."
            if (
                self._died_while_starting
                or process.poll() is not None
                or self._fatal_error()
            ):
                return False, self._why_ffmpeg_will_not_start(process)
            if self._landed_writes >= 2:
                return True, ""
            if not writer.is_alive():
                return False, (
                    "The recorder stopped writing frames while ffmpeg was "
                    "starting. See the log."
                )
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return True, self._say_ffmpeg_is_still_starting(process)
            self._launch_news.wait(min(remaining, 0.05))

    def _why_ffmpeg_will_not_start(self, process: subprocess.Popen[bytes]) -> str:
        """ffmpeg's reason for not starting, once it has finished giving it."""
        # A fatal line can come before ffmpeg exits; it is not waited out.
        self._kill_process(process)
        for reader in (self._stderr_thread, self._stdout_thread):
            if reader is not None:
                reader.join(timeout=1.0)
        return self._explain_early_exit()

    def _say_ffmpeg_is_still_starting(self, process: subprocess.Popen[bytes]) -> str:
        """Log why a take is announced before ffmpeg has shown it is recording,
        and return the detail to announce it with."""
        landed = self._landed_writes
        if not self._write_began:
            if self._launched_without_a_frame:
                # The launch already waited the whole START_READY_WAIT for a
                # frame, and said so. ffmpeg was started a moment ago: saying
                # it had run that long with nothing fed to it was wrong.
                log.info(
                    "Announcing the take with ffmpeg just launched and still no "
                    "frame on its way in (%d landed); the writer says so if none "
                    "arrives.",
                    landed,
                )
            else:
                log.info(
                    "No frame was on its way into ffmpeg %.1fs after it was "
                    "launched (%d landed); announcing the take, and the writer "
                    "says so if none arrives.",
                    START_READY_WAIT, landed,
                )
            return ""
        if landed == 0 and self._process_has_audio:
            settings = self.settings
            name = settings.audio_device if settings is not None else None
            log.warning(
                "ffmpeg has not read its first frame %.1fs after it was "
                "launched: it opens its audio input %r before reading one, and "
                "that is still opening. Announcing the take; nothing is in the "
                "file until the input opens, and it is said if that takes more "
                "than %.0fs.",
                START_READY_WAIT, name, FIRST_WRITE_WARN_AFTER,
            )
            self._opening_announced_for = process
            return f'Waiting for the audio input "{name}" to open; nothing is in the file yet.'
        if landed == 0:
            log.warning(
                "ffmpeg has not read its first frame %.1fs after it was "
                "launched; announcing the take, and the watchdog says so if it "
                "stays stuck.",
                START_READY_WAIT,
            )
        else:
            log.warning(
                "ffmpeg read its first frame but has not taken its second %.1fs "
                "after it was launched, so its output is still opening; "
                "announcing the take, and the watchdog says so if it stays stuck.",
                START_READY_WAIT,
            )
        return ""

    def _abandon_start(self) -> None:
        """Undo a start that failed once ffmpeg had been launched.

        Frames were being taken by then, and a failed start never reaches
        stop(), so whatever was queued stayed referenced until the next take
        -- a full queue is 30 frames, 186 MB at 1080p. The flag goes first,
        under the lock submit() reads it under, so nothing is queued behind the
        discard.
        """
        with self._lock:
            self._start_armed = False
            self._take_not_begun = False
        self._kill_process()
        self._cleanup()
        self._held.clear()
        self.parts = []
        discarded = self._discard_queued_frames()
        if discarded:
            log.info(
                "Discarded %d frame(s) queued for the take that did not start",
                discarded,
            )

    def _say_what_the_backlog_cost(self) -> None:
        """Log the frames a full queue gave up before the take began, if any."""
        with self._lock:
            given_up, self._backlog_given_up = self._backlog_given_up, 0
        if given_up:
            log.info(
                "%d frame(s) were given up, oldest first, while ffmpeg was "
                "starting and the queue was full. Not counted as dropped: the "
                "take had not begun, and -fps_mode cfr collapses a backlog read "
                "in one burst anyway.",
                given_up,
            )

    #: Lines that mean the recording will never work, regardless of whether
    #: ffmpeg has got around to exiting yet.
    _FATAL_MARKERS = (
        "error opening input",
        "error opening output",
        "unknown encoder",
        "no space left",
        "permission denied",
        "invalid argument",
        "conversion failed",
        "could not open",
    )

    def _fatal_error(self) -> bool:
        return any(
            marker in line.lower()
            for line in self._stderr_tail
            for marker in self._FATAL_MARKERS
        )

    def _kill_process(self, process: subprocess.Popen[bytes] | None = None) -> None:
        """Stop an ffmpeg, the current one unless told which.

        The watchdog names the process it judged stuck, so that a restart
        landing between its decision and the kill cannot turn it on the new one.
        """
        process = self._process if process is None else process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def _last_words(self) -> str:
        """ffmpeg's most recent line, for a log message."""
        lines = [line for line in list(self._stderr_tail) if line.strip()]
        return lines[-1][:200] if lines else "no output"

    def _audio_input_blamed(self) -> bool:
        """Whether ffmpeg's recent words point at the audio input.

        The first two tests are the phrases _explain_early_exit has always
        matched. The third rests on an assumption, stated so it can be checked:
        ffmpeg prefixes a component's messages with its name ("[dshow @ ...]",
        or "[in#0/dshow @ ...]" from its command-line layer, the audio input
        being ffmpeg's first), and the only DirectShow input in the command Wer
        builds is the audio one -- the picture arrives down a pipe -- so a
        DirectShow line reporting a failure is about the audio input. It rests
        on DirectShow's own line: when the input cannot be opened at all, the
        command-line layer says "[in#0 @ ...] Error opening input" with no
        format name in it (measured on the bundled 8.1.2 build), so without a
        "[dshow @" line only the device listing can blame the input. Nothing
        here has watched a real audio input fail mid-take.
        """
        lines = [line.lower() for line in list(self._stderr_tail) if line.strip()]
        joined = " ".join(lines)
        if "could not find audio only device" in joined:
            return True
        if "i/o error" in joined and "audio" in joined:
            return True
        return any(
            "dshow" in line
            and any(word in line for word in ("error", "could not", "failed"))
            for line in lines
        )

    def _error_kinds(self) -> frozenset[str]:
        """The kinds of error in ffmpeg's recent words, to tell failures apart.

        Its error-level lines, with their numbers and addresses set aside the
        way _RepeatGate tells one kind of line from another. The closing lines
        in _FFMPEG_EPITAPHS are left out, because they end failures of every
        kind alike. The tail is emptied at each launch, so this is what one
        ffmpeg said.
        """
        return frozenset(
            _RepeatGate.kind(line)
            for line in list(self._stderr_tail)
            if _ffmpeg_level(line) >= logging.ERROR
            and not any(epitaph in line for epitaph in _FFMPEG_EPITAPHS)
        )

    def _explain_early_exit(self) -> str:
        """Turn ffmpeg's parting words into something actionable."""
        lines = [line for line in self._stderr_tail if line.strip()]
        joined = " ".join(lines).lower()

        if self._audio_input_blamed():
            return (
                "ffmpeg could not open the audio device. Check the Audio setting, "
                "or set it to None to record video only."
            )
        if "permission denied" in joined:
            return "Permission denied writing the output file. Try another folder."
        if "no space left" in joined:
            return "The disk is full."
        if "unknown encoder" in joined or "cannot load" in joined:
            return (
                "The selected encoder is not available on this machine. "
                "Switch to Software (x264) in Recording settings."
            )
        if lines:
            return f"ffmpeg exited immediately: {lines[-1][:200]}"
        return "ffmpeg exited immediately without saying why."

    # ------------------------------------------------------------------ frames

    def submit(self, frame: Frame) -> bool:
        """Offer a frame. False if it had to be dropped, or was not wanted.

        Never blocks: the caller is the capture or compositing path and must not
        be stalled by a slow encoder.

        Taken while STARTING too, from the moment start() has checked the audio
        input, because ffmpeg must find a frame already waiting once its audio
        input has opened (see build_ffmpeg_command): a take with sound launches
        ffmpeg only once one is queued. Refused, and not counted, before that:
        start() can spend up to 15 s asking DirectShow which audio inputs exist,
        and frames queued then would only be thrown away.

        Refused, and not counted as dropped, once the disk floor has stopped
        the take: the writer is finishing the frames that were queued at that
        moment, and nothing after them will be written. Taking them anyway
        filled a queue nobody was reading, held up to 90 frames in memory until
        the next take, and made stop() wait out its sentinel timeout.
        """
        state = self.state
        if self._disk_stop_requested or state not in (
            RecorderState.RECORDING, RecorderState.STARTING
        ):
            return False
        if state is RecorderState.STARTING or self._take_not_begun:
            return self._queue_before_the_take_begins(frame)
        try:
            self._queue.put_nowait(frame)
            return True
        except queue.Full:
            self._note_drop()
            return False

    def _queue_before_the_take_begins(self, frame: Frame) -> bool:
        """Queue a frame offered before the take has begun.

        A full queue gives up its OLDEST frame here, and that is not counted as
        dropped. The queue fills only while ffmpeg is opening its inputs -- at
        30 fps an audio input taking a second fills all of it -- and what is
        queued then is read in one burst once it has: stamped within a
        millisecond of each other, -fps_mode cfr collapses them, and the
        writer drops the ones older than the file's first picture anyway
        (_drop_stale_backlog). Counted, they would put DROPPED on a healthy
        take whose audio input was slow to open, put down to an encoder that
        had not started. Giving up the oldest keeps the newest, so the frame
        waiting when ffmpeg is ready is live.
        """
        with self._lock:
            # Under the lock _abandon_start clears the flag under, so a frame
            # cannot slip into the queue after a failed start has emptied it.
            if not self._start_armed or self.state not in (
                RecorderState.STARTING, RecorderState.RECORDING
            ):
                return False
            while True:
                try:
                    self._queue.put_nowait(frame)
                except queue.Full:
                    pass
                else:
                    self._launch_news.set()     # start() may be waiting for one
                    return True
                try:
                    oldest = self._queue.get_nowait()
                except queue.Empty:
                    continue                # the writer took one meanwhile
                if oldest is None:
                    # A stop's sentinel, which nothing is queued behind. It
                    # cannot be here while the state holds, and must not be
                    # lost if it somehow is.
                    try:
                        self._queue.put_nowait(None)
                    except queue.Full:
                        pass
                    return False
                self._backlog_given_up += 1

    def _note_drop(self) -> None:
        """Count a frame the full queue turned away, against what cost it.

        The total goes up at once whatever the reason: the Recording tab shows
        it live. What the frame is put down to decides what the log and the
        finish message say, and they used to put every one down to the
        encoder. The overnight soak on h264_mf, whose ffmpeg could not write a
        header at any start, logged "The encoder is not keeping up with
        capture" after a restarted ffmpeg had taken its two frames and before
        its death was logged, and the take was reported as 2,638 frames lost
        "because the encoder could not keep up". Its encoder never produced a
        picture.

        - With no ffmpeg taking frames -- one has been found dead and no
          replacement has shown it is encoding -- the frame is lost to ffmpeg
          failing, and logged as that.
        - With one running, a full queue does not yet say which. An encoder
          that is slow goes on encoding, and one that is failing does not. So
          the frame waits: behind once ffmpeg's own frame count moves, lost to
          ffmpeg failing if it dies first. See _judge_pending_drops.
        """
        stats = self.stats
        # Locked: this is not the only thread that moves these, or the flag.
        # The writer settles and adds frames, and ffmpeg's reader settles them.
        with self._lock:
            stats.frames_dropped += 1
            if self._failed_ffmpeg is None:
                self._drops_pending += 1
                return
            stats.frames_dropped_ffmpeg_failing += 1
            failing = stats.frames_dropped_ffmpeg_failing
        self._report_drops(failing=True, before=failing - 1, after=failing)

    def _judge_pending_drops(
        self,
        *,
        ffmpeg_failed: bool,
        counted_by: subprocess.Popen[bytes] | None = None,
    ) -> None:
        """Settle what the frames turned away while ffmpeg ran were lost to.

        On the evidence there is. ffmpeg's own frame count moving
        (``ffmpeg_failed=False``) shows it encoding, so the frames it could not
        take meanwhile were an encoder falling behind, and a replacement's
        first movement is where the cost of the failure before it ends. ffmpeg
        dying, or having to be killed for taking nothing
        (``ffmpeg_failed=True``), shows they were lost to it failing, and
        nothing takes frames from then until a replacement's count moves.

        Not a write landing: ffmpeg reads ahead of its encoder, so a failing
        one still takes a frame or two first. The soak's took two every time.

        ``counted_by`` is the ffmpeg whose count moved, when that is the
        evidence. It counts only while it is the current one, still running,
        and not one the writer has already found dead, and that is checked
        under the lock the writer's judgement takes. A reader can hand over
        its process's last lines after the writer has dealt with the death,
        because the writer waits only so long for them (see
        _let_ffmpeg_finish_talking). Nor is "still running" enough on its own.
        Measured on Windows 11 with a Python child standing in for ffmpeg: in
        200 tries out of 200, a write into its pipe failed as it exited while
        poll() still said it was running.
        """
        stats = self.stats
        with self._lock:
            if counted_by is not None and (
                counted_by is not self._process
                or counted_by is self._failed_ffmpeg
                or counted_by.poll() is not None
            ):
                return
            self._failed_ffmpeg = self._process if ffmpeg_failed else None
            pending, self._drops_pending = self._drops_pending, 0
            if not pending:
                return
            if ffmpeg_failed:
                stats.frames_dropped_ffmpeg_failing += pending
                after = stats.frames_dropped_ffmpeg_failing
            else:
                after = stats.frames_dropped - stats.frames_dropped_ffmpeg_failing
        self._report_drops(
            failing=ffmpeg_failed, before=after - pending, after=after
        )

    def _report_drops(self, *, failing: bool, before: int, after: int) -> None:
        """Log the count of frames dropped for one reason, when it is due.

        Each reason is logged at DROP_MILESTONES, then summed into one line at
        most every DROP_REPORT_INTERVAL while it keeps rising, so an encoder
        that falls behind for an hour leaves a total in the log and not a
        flood. The two are kept apart so that the first frame an encoder really
        cannot take is logged as the first, however many a failing ffmpeg cost
        before it. Frames can be settled several at once, so a milestone is
        due when a count passes it, not only when it lands on it.
        """
        now = time.perf_counter()
        with self._lock:
            first = failing not in self._drops_reported
            reported, reported_at = self._drops_reported.get(failing, (0, now))
            milestone = first or any(
                before < mark <= after for mark in DROP_MILESTONES
            )
            if not milestone and now - reported_at < DROP_REPORT_INTERVAL:
                return
            self._drops_reported[failing] = (after, now)
            total, percent = self.stats.frames_dropped, self.stats.drop_percent
        if failing and milestone:
            log.warning(
                "ffmpeg failed and nothing is taking frames: %d frame(s) "
                "dropped for that so far this take. This is not the encoder "
                "falling behind.",
                after,
            )
        elif failing:
            log.warning(
                "ffmpeg failed and nothing is taking frames: %d more frame(s) "
                "dropped since the last report %.0fs ago; %d dropped so far "
                "this take (%.1f%% of the frames offered).",
                after - reported, now - reported_at, total, percent,
            )
        elif milestone:
            log.error(
                "Encoder queue full; %d frame(s) dropped so far with ffmpeg "
                "encoding too slowly. The encoder is not keeping up with "
                "capture.",
                after,
            )
        else:
            log.error(
                "Encoder queue full: %d more frame(s) dropped since the last "
                "report %.0fs ago; %d dropped so far this take (%.1f%% of the "
                "frames offered).",
                after - reported, now - reported_at, total, percent,
            )

    def _write_frames(self) -> None:
        me = threading.current_thread()
        # The second condition retires a writer that has been let go. A start
        # whose ffmpeg failed its launch check used to leave its writer
        # polling an empty queue for good -- nothing ever set _abort for it --
        # and the next take that did start then had two writers taking frames
        # off one queue and writing them into one pipe, in whatever order the
        # two threads happened to run.
        while not self._abort.is_set() and self._writer is me:
            if self._disk_stop_requested and self._disk_backlog <= 0:
                log.info("Ending the recording early: the disk is nearly full")
                break
            try:
                frame = self._held.popleft()
                from_queue = False
            except IndexError:
                from_queue = True
            if from_queue:
                try:
                    frame = self._queue.get(timeout=0.5)
                except queue.Empty:
                    frame = _NOTHING
            if frame is _NOTHING:
                if self._writer is not me:
                    break
                if self._disk_stop_requested:
                    log.info("Ending the recording early: the disk is nearly full")
                    break
                process = self._process
                if process is not None and process.poll() is not None:
                    self._handle_ffmpeg_death()
                    if self.state is not RecorderState.RECORDING:
                        return
                self._check_frames_still_arriving()
                continue

            # The sentinel arrives BEHIND every frame already queued, so by the
            # time it is read the backlog has been written. Checking a stop
            # flag at the top of the loop instead would abandon that backlog --
            # which it did, silently losing the last seconds of every take.
            if frame is None:
                break

            if self._disk_stop_requested and from_queue:
                # One of the frames already queued when the floor was reached.
                # The disk stop broke out at the top of the loop, which is the
                # mistake described just above, and abandoned every one of
                # them while its own comment said they were being written.
                #
                # Counting, not a syscall: the disk itself is looked at by the
                # watchdog now, for the reason _check_disk_if_due gives.
                self._disk_backlog -= 1

            # Re-read each time: a restart replaces the process underneath us.
            process = self._process
            if process is None or process.stdin is None:
                continue
            if self._writer is not me:
                # Let go, by a start that failed, while it waited for this
                # frame. The process current now can already be the next
                # take's, and a frame from this one must not go into it.
                break

            image = frame.image
            if not image.flags["C_CONTIGUOUS"]:
                # ffmpeg reads a flat byte stream; a non-contiguous array would
                # be written with its padding and shear the picture.
                image = np.ascontiguousarray(image)

            # The array goes to the pipe as it stands. ffmpeg is launched with
            # bufsize=0, so stdin is a raw FileIO, which takes any C-contiguous
            # buffer -- and .tobytes() here was copying every frame for nothing:
            # 6 MB at 1080p, 25 MB at 4K, on the thread that must not fall
            # behind. Checked byte-for-byte against the old path before the
            # change.
            #
            # The count is checked because a short write does not raise: it
            # would shift every frame boundary after it and shear the rest of
            # the take, which is exactly the kind of quiet ruin worth making
            # loud. Windows anonymous pipes block until all bytes are taken, so
            # this has never been seen; it is a guard, not a fix.
            began = time.perf_counter()
            self._write_began = began
            try:
                written = process.stdin.write(image)
                if written is not None and written != image.nbytes:
                    # Against the frame in hand, not self._frame_bytes: that is
                    # set by _launch, and comparing against it would make this
                    # depend on setup order rather than on the write.
                    raise OSError(
                        f"short write to ffmpeg: {written} of "
                        f"{image.nbytes} bytes"
                    )
            except (BrokenPipeError, OSError):
                # Only the write is inside the try. The bookkeeping below calls
                # a listener, and an OSError raised in someone else's callback
                # must not be read here as ffmpeg dying.
                self._write_began = 0.0
                self._handle_ffmpeg_death()
                if self.state is not RecorderState.RECORDING:
                    return
                # Continued into a new part; this frame is the one lost.
                continue
            self._write_began = 0.0

            self.stats.note_frame()
            self.stats.bytes_written += self._frame_bytes
            landed_at = self.stats.last_frame_at
            first_in_part = not self._part_first_write
            if first_in_part:
                self._part_first_write = landed_at
                # Before the count below, which start() reads it by.
                self._first_write_waited = landed_at - began
                self._anchor_current_part()
            if process is self._process:
                self._landed_writes += 1
                if self._landed_writes <= 2:
                    self._launch_news.set()     # start() may be waiting on it
            if first_in_part:
                self._note_first_frame_landed(process, began, landed_at)
            if self._stall_reported_for is not None:
                self._note_writes_resumed(process)
            if self._stalled:
                self._note_frames_resumed()
            if (
                self._consecutive_failures
                and self.stats.last_frame_at - self._part_first_write
                >= HEALTHY_PART_SECONDS
            ):
                log.info(
                    "%s has recorded for %.0fs; the ffmpeg failure(s) before it "
                    "no longer count towards giving up",
                    self.parts[-1].path.name if self.parts else "This part",
                    HEALTHY_PART_SECONDS,
                )
                if self._sound_trial_after is not None:
                    # A trial without sound that records is the one outcome
                    # that does point at the input. It used to end here
                    # unsaid, and the log's last word on the sound stayed "in
                    # case the audio input is why".
                    log.warning(
                        "ffmpeg has recorded for %.0fs since the audio input was "
                        "taken out at %s, after failing %d times in a row with "
                        "it, so the audio input may have been the cause. The "
                        "rest of the take stays without sound.",
                        HEALTHY_PART_SECONDS, _clock(self.audio_dropped_at or 0.0),
                        self._consecutive_failures,
                    )
                    self._sound_trial_after = None
                self._consecutive_failures = 0

        if self._writer is not me:
            # Let go -- by a start whose ffmpeg failed, or a stop that gave up
            # on this thread. Whatever process is current now belongs to the
            # take that let it go, or to the next one: closing its stdin here
            # ended the next take's ffmpeg the moment it launched.
            return
        # Re-read rather than trusting a name from inside the loop: the loop
        # can exit on the very first pass (an abort, or the disk stop) without
        # ever having bound one, and the NameError that produced killed this
        # thread silently -- leaving stdin open, so ffmpeg never saw EOF and
        # stop() waited out the whole shutdown timeout before terminating it.
        process = self._process
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass

    def _note_first_frame_landed(
        self, process: subprocess.Popen[bytes], began: float, landed_at: float
    ) -> None:
        """What follows the first frame a part's ffmpeg has taken.

        ``began`` and ``landed_at`` bound that write, which for an ffmpeg with
        an audio input lasted as long as the input took to open.
        """
        waited = landed_at - began
        with self._lock:
            began_the_take = self._take_not_begun
            self._take_not_begun = False
        if self._process_has_audio and not self._note_if_the_sound_is_late(
            process, began, waited
        ):
            self._say_the_audio_input_opened(process)
        _, _, fps = self._capture_geometry
        interval = 1.0 / fps if fps > 0 else 1.0 / 30
        if waited > interval and not self._disk_stop_requested:
            self._drop_stale_backlog(landed_at, waited)
        if began_the_take and self.state is RecorderState.RECORDING:
            # Announced before this frame landed; otherwise start() says it.
            self._say_what_the_backlog_cost()

    def _note_if_the_sound_is_late(
        self, process: subprocess.Popen[bytes], began: float, waited: float
    ) -> bool:
        """Say so when a part's sound came out late against its picture.

        True if it did. ``began`` and ``waited`` are the part's first write, to
        an ffmpeg with an audio input. One that set off FIRST_FRAME_IN_STEP_WITHIN
        or more after launch and landed within it found ffmpeg already reading
        its pipe, which ffmpeg does only once the audio input has opened: the
        input opened with no frame to read, the sound is timed from its first
        sample and the picture from this frame, and the part's sound is late by
        the gap for as long as it runs. Only the gap's limit is known, the time
        from launch to this write.

        start() and a restart with sound both launch ffmpeg on a frame in hand
        so that this does not happen; it is for a take whose camera delivered
        nothing before START_READY_WAIT ran out. Said on screen and not only in
        the log, because sound out of step is found in the edit, weeks later.
        """
        set_off = began - self._part_started_at
        if waited >= FIRST_FRAME_IN_STEP_WITHIN or set_off < FIRST_FRAME_IN_STEP_WITHIN:
            return False
        settings = self.settings
        name = settings.audio_device if settings is not None else None
        where = self.parts[-1].path.name if self.parts else "the recording"
        message = (
            f"The first frame reached ffmpeg {set_off:.1f}s after it was "
            f'launched, after the audio input "{name}" had opened, so the sound '
            f"in {where} may be up to {set_off:.1f}s late against the picture. "
            f"Stop and start the take again to put them back in step."
        )
        log.warning("%s", message)
        # Said instead of what would otherwise follow this frame and put
        # something else on screen over it: that the input has opened, that
        # ffmpeg is taking frames again, or that the camera's frames resumed --
        # the camera not delivering being how this came about.
        self._stalled = False
        with self._lock:
            self._opening_announced_for = None
            if self._stall_reported_for is process:
                self._stall_reported_for = None
            self._sound_late_said = message
        self._say_the_sound_is_late()
        return True

    def _say_the_sound_is_late(self) -> None:
        """Put _note_if_the_sound_is_late's message on screen, once the take is
        RECORDING. Called by the writer as it finds the sound late and by
        start() once it has announced the take, for the reason
        _say_the_audio_input_opened gives; whichever comes second finds nothing
        to say."""
        with self._lock:
            message = self._sound_late_said
            if message is None or self.state is not RecorderState.RECORDING:
                return
            self._sound_late_said = None
        self._set_state(
            RecorderState.RECORDING, message, only_if=RecorderState.RECORDING
        )

    def _say_the_audio_input_opened(self, process: subprocess.Popen[bytes]) -> None:
        """Say the audio input has opened, once, if the take was announced or
        warned about while it was still opening.

        "ffmpeg is taking frames again" would be the wrong thing to say then,
        and so would nothing: the file begins now, not when Record was pressed.
        Called by the writer as the first frame lands, and by start() once it
        has announced the take, because the frame can land between start()
        deciding what to announce and announcing it, and a writer that finds
        the take not yet RECORDING leaves this to start(). Left to the writer
        alone, "Waiting for the audio input to open" stood over a take that was
        recording. Whichever of the two comes second finds nothing to say.
        """
        with self._lock:
            if self.state is not RecorderState.RECORDING or not (
                self._stall_reported_for is process
                or self._opening_announced_for is process
            ):
                return
            self._stall_reported_for = None
            self._opening_announced_for = None
        settings = self.settings
        name = settings.audio_device if settings is not None else None
        where = self.parts[-1].path.name if self.parts else "the recording"
        message = (
            f'The audio input "{name}" took {self._first_write_waited:.1f}s to '
            f"open; {where} begins from then, and nothing before it is in it."
        )
        log.warning("%s", message)
        self._set_state(
            RecorderState.RECORDING, message, only_if=RecorderState.RECORDING
        )

    def _drop_stale_backlog(self, landed_at: float, waited: float) -> None:
        """Drop the frames queued behind a part's first frame while it waited.

        ffmpeg stamps each frame as it reads it, and reads a part's first only
        once its audio input has opened. Every frame queued meanwhile was
        captured before the file's first picture and is read straight after it
        in a burst: measured 13 Sep 2026, three frames of 0.1-0.3 s old picture
        opened every take, and none once they were dropped. Only after a write
        that waited longer than a frame lasts: one that did not has nothing
        behind it but the next frame, and frames offered faster than ffmpeg
        reads them are not a backlog.

        Not counted as dropped: nothing is missing from the file, which begins
        at its first frame. The first frame found newer than that is kept and
        written next, and everything behind it with it.
        """
        stale = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None and item.timestamp < landed_at:
                stale += 1
                continue
            self._held.append(item)
            break
        if stale:
            log.info(
                "Dropped %d frame(s) captured before ffmpeg read the first frame "
                "of %s, %.2fs after it was written: older than the file's first "
                "picture, they would have opened it on a burst of stale ones. "
                "Not counted as dropped.",
                stale, self.parts[-1].path.name if self.parts else "the take",
                waited,
            )

    def _anchor_current_part(self) -> None:
        """Pin the part that just received its first frame to the marker clock.

        session_offset is what main_window subtracts from every marker time to
        place it inside this particular file, so it has to be measured from the
        SAME origin the marker times are: stats.elapsed, which runs from the
        session's first frame. It used to be measured from stats.started_at,
        which was 0.6s earlier then -- a launch check that let no frame through
        until it was over -- and the two disagreeing put every marker in part 2
        onwards 0.62s out and dropped any marker taken between part 1's last
        frame and part 2's offset from the chapters altogether.

        And measured from the part's first WRITE, not from when its ffmpeg was
        launched: the file's own t=0 is the first frame that reaches it
        (-use_wallclock_as_timestamps), which ffmpeg reads only once its audio
        input has opened, a few hundred milliseconds after launch and different
        every time.
        """
        if not self.parts:
            return
        origin = self.stats.first_frame_at or self._part_first_write
        self.parts[-1].session_offset = max(0.0, self._part_first_write - origin)

    def _check_frames_still_arriving(self) -> None:
        """Say so, once, when the camera stops delivering.

        Nothing else tells the recorder. CameraCapture spots a stall after 30
        failed reads and reports it to the preview, which paints a message on
        the picture; the recorder is not a listener. Down here the only symptom
        is an empty queue, which is indistinguishable from a slow moment unless
        somebody times it -- so a take whose camera was unplugged at 02:40 sat
        in RECORDING for the rest of the afternoon with wallclock elapsed still
        climbing, and its chapters and markers were written against that.

        Deliberately does NOT end the take. Measured: a gap that recovers is
        filled correctly by -fps_mode cfr -- a six-second dropout inside a
        fourteen-second take produced a fourteen-second file, still in sync --
        so stopping here would turn a survivable hiccup into the loss of the
        rest of the act. What was wrong was the silence, and the durations
        derived from the wallclock; those are fixed instead.
        """
        if self.state is not RecorderState.RECORDING or self._stalled:
            return
        since = self.stats.last_frame_at or self.stats.started_at
        if not since:
            return
        idle = time.perf_counter() - since
        if idle < STALL_TIMEOUT:
            return

        self._stalled = True
        if self.stats.frames_written:
            message = (
                f"No frames from the camera for {idle:.0f}s. The recording is "
                f"still open but nothing is being written to it."
            )
        else:
            message = (
                f"No frames have reached the recorder in {idle:.0f}s. Nothing "
                f"is being recorded."
            )
        log.error("%s", message)
        self._set_state(RecorderState.RECORDING, message)

    def _note_frames_resumed(self) -> None:
        self._stalled = False
        if self.state is not RecorderState.RECORDING:
            # Same guard as _check_frames_still_arriving, and for a sharper
            # reason: this runs from the writer thread on any successful write,
            # including the frames drained during stop(). Announcing RECORDING
            # there would flip the recorder out of STOPPING -- which re-opens
            # submit() and repaints the panel as "Recording" -- for the whole
            # length of the drain, reporting the opposite of what it is doing.
            return
        log.warning("Frames from the camera resumed")
        self._set_state(
            RecorderState.RECORDING,
            "Frames resumed after a gap; the gap is in the recording.",
        )

    def _note_writes_resumed(self, process: subprocess.Popen[bytes]) -> None:
        reported, self._stall_reported_for = self._stall_reported_for, None
        if reported is not process:
            # The stall was said of an ffmpeg that has since died or been
            # killed, and this frame went to the one that replaced it. Nothing
            # recovered, and the restart has already said where the take went.
            return
        log.warning("ffmpeg is taking frames again")
        # Guarded for the reason _note_frames_resumed gives: this also runs on
        # the frames drained during stop(), and must not announce RECORDING
        # over STOPPING.
        self._set_state(
            RecorderState.RECORDING,
            "ffmpeg is taking frames again after a stall; the frames dropped "
            "meanwhile are not in the recording.",
            only_if=RecorderState.RECORDING,
        )

    # --------------------------------------------------------------- watchdog

    def _watch_writes(self, writer: threading.Thread, stop: threading.Event) -> None:
        """Watch the writer from outside, for the one failure it cannot see.

        A write into ffmpeg's pipe that never comes back. ffmpeg can stay alive
        and stop reading -- a wedged hardware encoder, a stalled USB or network
        volume, an SMR disk pausing, as stop() puts it -- and the writer is
        then stuck inside that call, where none of its own checks run. This
        thread does nothing but time the write in flight, and goes when the
        writer does.
        """
        while writer.is_alive() and not stop.wait(WATCHDOG_INTERVAL):
            for check in (
                self._check_write_in_flight,
                self._check_the_sound_is_arriving,
                self._check_disk_if_due,
                self._sample_size_if_due,
            ):
                try:
                    check()
                except Exception:  # noqa: BLE001 - it has to outlive its own bugs
                    log.exception("The ffmpeg watchdog raised")

    def _check_write_in_flight(self) -> None:
        began = self._write_began
        process = self._process
        if not began or process is None or self.state is not RecorderState.RECORDING:
            return
        stuck = time.perf_counter() - began
        # The first write to an ffmpeg with an audio input waits for that input
        # to open, since ffmpeg opens it before reading a frame. That is not
        # ffmpeg failing to take frames: it has its own limits and its own
        # words, and nothing is inside ffmpeg yet to be lost by killing it. See
        # FIRST_WRITE_WARN_AFTER.
        opening = self._process_has_audio and self._landed_writes == 0
        if opening:
            warn_after, kill_after = FIRST_WRITE_WARN_AFTER, FIRST_WRITE_RESTART_AFTER
        else:
            warn_after, kill_after = WRITE_STALL_WARN_AFTER, WRITE_STALL_RESTART_AFTER
        if stuck < warn_after:
            return

        # ffmpeg's own frame count is a second opinion, not a precondition: if
        # it has moved since this write began, ffmpeg is still doing something
        # and is left to it. Its progress lines arrive about twice a second on
        # the bundled build (measured); if none ever arrive, the count never
        # moves and the time alone decides, which is the safe way round.
        idle = self._progress_moved_at <= began
        # Said as it will actually happen: with continuing after a failure
        # switched off, the kill ends the take, and promising a new file there
        # would send someone looking for one.
        afterwards = (
            "the take will continue in a new file"
            if self.restart_on_failure
            else "the take will end, because continuing after a failure is off"
        )
        settings = self.settings
        name = settings.audio_device if settings is not None else None
        if stuck >= kill_after and idle:
            if self._write_began != began:
                return                  # it landed while this was deciding
            if opening:
                log.error(
                    "The audio input %r has not opened after %.0fs, and ffmpeg "
                    "reads no frame until it has; killing it, and %s. Its last "
                    "words: %s",
                    name, stuck, afterwards, self._last_words(),
                )
                self._killed_for_not_opening = stuck
            else:
                log.error(
                    "ffmpeg has taken nothing from its pipe for %.0fs and its own "
                    "frame count has not moved from %d; killing it, and %s. Its "
                    "last words: %s",
                    stuck, self._progress[0], afterwards, self._last_words(),
                )
                self._killed_for_stalling = stuck
            # This process, not whatever _process is by the time it runs.
            self._kill_process(process)
            return

        if self._stall_reported_for is process or self._write_began != began:
            return
        if opening:
            message = (
                f'The audio input "{name}" has not opened after {stuck:.0f}s, so '
                f"nothing is being recorded. If it has not opened after "
                f"{kill_after:.0f}s, ffmpeg will be stopped and {afterwards}."
            )
        else:
            message = (
                f"ffmpeg has not taken a frame for {stuck:.0f}s, so frames are "
                f"being dropped. If it has not recovered after "
                f"{kill_after:.0f}s it will be stopped and {afterwards}."
            )
        if self._set_state(
            RecorderState.RECORDING, message, only_if=RecorderState.RECORDING
        ):
            self._stall_reported_for = process
            log.error("%s Its last words: %s", message, self._last_words())

    # ------------------------------------------------------------------- disk

    def _sample_size_if_due(self, *, force: bool = False) -> None:
        """Measure what the take has put on disk, for the panel and the estimate.

        On the watchdog thread -- never on the GUI thread (see
        RecordingStats.file_size_mb) and, since 12 Sep 2026, never on the
        writer either. See _check_disk_if_due for why.
        """
        now = time.perf_counter()
        if not force and now < self._next_size_sample:
            return
        self._next_size_sample = now + SIZE_SAMPLE_INTERVAL
        total = self._session_bytes_on_disk()
        if total is not None:
            self.stats.note_size(now, total)

    def _session_bytes_on_disk(self) -> int | None:
        """Every part's bytes: finished parts as last measured, this one now.

        None when the current part cannot be measured, so a drive that has
        stopped answering leaves the last figure standing -- and going stale,
        which the Recording tab says -- rather than recording a smaller number.
        """
        current = self.stats.output_path
        finished = sum(part.size_bytes for part in self.parts if part.path != current)
        if current is None:
            return finished
        try:
            size = current.stat().st_size
        except FileNotFoundError:
            # ffmpeg creates the file once its inputs are open -- with sound, a
            # few hundred milliseconds after launch. Until the file has been
            # seen, not being there is expected.
            return None if self._part_seen_on_disk else finished
        except OSError:
            return None
        self._part_seen_on_disk = True
        return finished + size

    def _check_disk_if_due(self) -> None:
        """Look at free space every few seconds while recording.

        On the WATCHDOG thread, not the writer. It ran between frames on the
        writer, on the reasoning that a stat call costs nothing next to writing
        6 MB of frame. That holds for a disk that answers. It does not hold for
        one that has stopped: check_disk walks the path, and the record panel
        measured that same walk at 28.7 s on a dropped share -- which is why
        the panel moved it off the GUI thread (see record_panel). Held up that
        long between frames, the writer stops writing, the queue fills in about
        five seconds, and the drops are then reported as the encoder not
        keeping up, while the watchdog sees no write in flight to blame. The
        one thing that would tell you the truth is the thing that cannot run.

        The watchdog already runs every second and already wraps each check so
        that one of them failing cannot take the thread down. Both checks gate
        themselves on their own intervals, so their cadence is unchanged.
        """
        if self._disk_stop_requested:
            # Already stopping. Running again would re-take _disk_backlog from
            # a queue that has moved on, and the writer counts down from that
            # number to know when the last queued frame has been written.
            return
        now = time.perf_counter()
        if now < self._next_disk_check:
            return
        self._next_disk_check = now + DISK_CHECK_INTERVAL

        output = self.stats.output_path
        if output is None:
            return
        space = check_disk(output.parent)
        if space is None:
            return

        floor = max(self.stop_below_bytes, CRITICAL_FREE_BYTES)

        # Minutes until the recording STOPS, not minutes until the disk hits
        # zero. The recorder holds back the floor deliberately, so counting it
        # as recordable time overstated the answer by exactly 2x at the shipped
        # defaults: 19.9 GB free at 4 GB/hour was announced as "about 299
        # minutes" and the take ended 149 minutes later.
        #
        # And at the faster of two rates: every part over the whole take, and
        # the last few minutes. The whole-take average alone hides a change of
        # pace -- the house dark for two hours, then a lit act -- and the
        # recent rate alone forgets a busy stretch the moment the stage goes
        # quiet. The faster errs towards less time than there really is, which
        # is the direction someone in the booth can do something about.
        self._sample_size_if_due(force=True)
        rate = max(self.stats.bytes_per_second, self.stats.recent_bytes_per_second)
        usable = max(0.0, space.free_bytes - floor)
        minutes = (usable / rate / 60) if rate > 0 else None

        critical = space.free_bytes <= floor
        warning = DiskWarning(space.free_bytes, minutes, critical)
        self.disk = warning

        if critical:
            log.error(
                "Only %.1f GB free, at or below the %.1f GB floor; stopping the "
                "recording so the file closes properly.",
                warning.free_gb, floor / 1_000_000_000,
            )
            self._notify_disk(warning)
            # Not an abort: the frames queued at this moment are still written
            # -- the writer takes exactly that many more and then closes stdin
            # -- and ffmpeg still gets a clean shutdown. A file that closes
            # properly is the whole point of stopping early. The flag goes
            # first, so submit() stops adding frames behind the count.
            self._disk_stop_requested = True
            self._disk_backlog = self._queue.qsize()
            return

        if space.free_bytes >= self.low_disk_bytes:
            return
        # Said on first crossing the warning level, and again each time the
        # estimate falls past a LOW_DISK_REANNOUNCE_MINUTES step -- not every
        # check, which over four hours would be 1,440 warnings. It used to
        # latch after the first, so however wrong that estimate became, it was
        # the only one anyone got.
        step = next(
            (
                limit for limit in sorted(LOW_DISK_REANNOUNCE_MINUTES)
                if minutes is not None and minutes <= limit
            ),
            None,
        )
        closer = step is not None and (
            self._warned_step is None or step < self._warned_step
        )
        if self._warned_low and not closer:
            return
        self._warned_low = True
        if closer:
            self._warned_step = step
        log.warning("Low disk space while recording: %s", warning.describe())
        self._notify_disk(warning)

    def _notify_disk(self, warning: DiskWarning) -> None:
        if self._on_disk_warning is None:
            return
        try:
            self._on_disk_warning(warning)
        except Exception:  # noqa: BLE001
            log.exception("Disk warning handler raised")

    def _handle_ffmpeg_death(self) -> None:
        """ffmpeg went away while we were still recording.

        If continuation is enabled, a fresh ffmpeg is started writing the next
        numbered part and recording carries on. What is lost is the frames
        buffered in the dead process -- a second or so -- rather than the rest
        of the session.
        """
        if self._abort.is_set():
            # We are the ones who killed it, in stop(), to unblock a wedged
            # write. Nothing to report and nothing to restart.
            log.info("ffmpeg went away during shutdown, as expected")
            return
        if self.state is RecorderState.STARTING and self._fail_the_start():
            return
        if self.state not in (RecorderState.RECORDING, RecorderState.STOPPING):
            # Error or idle: the take is already over.
            return

        self._let_ffmpeg_finish_talking()
        self._bank_ffmpeg_progress()
        last = self._last_words()
        stalled_for, self._killed_for_stalling = self._killed_for_stalling, 0.0
        opening_for, self._killed_for_not_opening = self._killed_for_not_opening, 0.0
        # Evidence against the audio input once it happens twice running; see
        # _reason_to_drop_audio.
        self._opening_kills_in_a_row = (
            self._opening_kills_in_a_row + 1 if opening_for else 0
        )
        if opening_for:
            log.error(
                "ffmpeg was killed after its audio input had not opened for "
                "%.0fs, %d frames into the take: %s",
                opening_for, self.stats.frames_written, last,
            )
        elif stalled_for:
            log.error(
                "ffmpeg was killed after taking no frames for %.0fs, %d frames "
                "into the take: %s",
                stalled_for, self.stats.frames_written, last,
            )
        else:
            log.error(
                "ffmpeg stopped unexpectedly after %d frames: %s",
                self.stats.frames_written, last,
            )
        # The frames turned away while the writer waited on it were lost to it
        # failing, and nothing takes any more until a replacement is encoding.
        self._judge_pending_drops(ffmpeg_failed=True)
        # Even when the take's first frame never landed: frames turned away
        # from here on are lost to ffmpeg failing, and counted as that.
        with self._lock:
            self._take_not_begun = False
        self._opening_announced_for = None

        if self.state is RecorderState.STOPPING:
            # A death during the drain, that we did not cause. Do NOT restart:
            # a new part here reopens a session that is already being closed,
            # which is exactly what the pre-fix code did while stop() was
            # wedged. But do not go quiet about it either -- this branch also
            # catches ffmpeg failing to write its trailer ("No space left on
            # device" at the last moment), and the line above is the only place
            # that reason is ever recorded. The part accounting is left to
            # stop(), which does it after process.wait(), when the file on disk
            # is final; doing it here would judge a file still being flushed.
            return

        self._close_current_part()
        self._verify_parts()

        if not (self.restart_on_failure and self._base_path is not None):
            if opening_for:
                reason = (
                    f"The audio input had not opened after {opening_for:.0f}s, "
                    f"so ffmpeg was stopped, {self.stats.frames_written} frames "
                    f"in: {last}"
                )
            elif stalled_for:
                reason = (
                    f"ffmpeg stopped taking frames for {stalled_for:.0f}s and was "
                    f"stopped, {self.stats.frames_written} frames in: {last}"
                )
            else:
                reason = (
                    f"ffmpeg stopped unexpectedly after "
                    f"{self.stats.frames_written} frames: {last}"
                )
            self._set_state(RecorderState.ERROR, reason)
            return

        self._consecutive_failures += 1
        failures = self._consecutive_failures
        self.stats.most_ffmpeg_failures_in_a_row = max(
            self.stats.most_ffmpeg_failures_in_a_row, failures
        )
        trial_after, self._sound_trial_after = self._sound_trial_after, None
        audio_problem, listed = self._reason_to_drop_audio()
        if audio_problem is not None:
            there = self._camera_still_there(audio_problem)
            if there is None:
                return
            if not there:
                audio_problem = None
        on_trial = False
        if audio_problem is not None:
            device = self._drop_audio()
            log.error(
                "Continuing the take WITHOUT SOUND from %s: %s. The audio input "
                "was %r; the picture is kept rather than lost with it.",
                _clock(self.audio_dropped_at or 0.0), audio_problem, device,
            )
        elif failures > MAX_RESTARTS:
            if self.settings is None or not self.settings.audio_device:
                self._set_state(
                    RecorderState.ERROR,
                    f"ffmpeg failed {failures} times in a row"
                    f"{self._verdict_on_the_sound(trial_after)}; giving up "
                    f"rather than restarting again. Last error: {last}",
                )
                return
            # The last resort before giving up. Nothing points at the input,
            # so this is a trial of it, and said to be one.
            on_trial = True
            self._sound_trial_after = self._error_kinds()
            device = self._drop_audio()
            log.error(
                "Trying the take WITHOUT SOUND from %s, in case the audio input "
                "%r is why ffmpeg has failed %d times in a row. Nothing points "
                "at it: ffmpeg's words did not name it, and %s. If ffmpeg fails "
                "with the same errors without it, the sound was not the cause.",
                _clock(self.audio_dropped_at or 0.0), device, failures,
                "DirectShow still lists it" if listed
                else "the device list could not be read",
            )

        if RESTART_DELAYS:
            delay = RESTART_DELAYS[min(failures, len(RESTART_DELAYS)) - 1]
            if delay > 0 and not self._wait_before_restarting(delay, failures):
                return

        if self.settings is not None and self.settings.audio_device:
            frame = self._frame_to_restart_on()
            if frame is None:
                return
            self._held.append(frame)

        self._restarts += 1
        next_path = self._next_part_path()
        log.warning(
            "Continuing the recording into %s (restart %d, %d in a row)",
            next_path.name, self._restarts, failures,
        )

        if self._launch(next_path):
            self.parts.append(
                RecordingPart(
                    next_path,
                    # Provisional, and on the marker clock (the session's first
                    # frame), not the wallclock. _anchor_current_part() replaces
                    # it with the exact figure as soon as a frame lands; this
                    # value only survives for a part that never gets one, which
                    # _verify_parts marks unusable anyway.
                    session_offset=max(
                        0.0,
                        self._part_started_at
                        - (self.stats.first_frame_at or self.stats.started_at),
                    ),
                )
            )
            if opening_for:
                detail = (
                    f"The audio input had not opened after {opening_for:.0f}s, so "
                    f"ffmpeg was restarted; continuing in {next_path.name}"
                )
            elif stalled_for:
                detail = (
                    f"ffmpeg stopped taking frames for {stalled_for:.0f}s and was "
                    f"restarted; continuing in {next_path.name}"
                )
            else:
                detail = (
                    f"ffmpeg restarted after a failure; continuing in "
                    f"{next_path.name}"
                )
            if audio_problem is not None:
                detail += f" WITHOUT SOUND: {audio_problem}"
            elif on_trial:
                detail += (
                    " WITHOUT SOUND, in case the audio input is the cause, "
                    "though nothing points at it"
                )
            elif self.audio_dropped_at is not None:
                detail += " without sound"
            self._set_state(RecorderState.RECORDING, detail)
        else:
            self._set_state(
                RecorderState.ERROR,
                f"ffmpeg stopped and could not be restarted: {self.detail or last}",
            )

    def _fail_the_start(self) -> bool:
        """The writer has found ffmpeg dead while start() is waiting on it.

        True if the take has not started; False if start() announced it
        meanwhile, in which case this is a death mid-take after all. Frames
        reach ffmpeg while the take is STARTING, so the writer can be the first
        to know -- an unknown encoder fails the write of the second frame 1-4 ms
        after the first has landed. This was once ignored, on the grounds that
        the launch check was judging that very process; start() now waits on
        the pipe instead, so it is told at once, and whichever of the two says
        why first ends the start.
        """
        self._died_while_starting = True
        self._launch_news.set()
        self._let_ffmpeg_finish_talking()
        self._set_state(
            RecorderState.ERROR, self._explain_early_exit(),
            only_if=RecorderState.STARTING,
        )
        return self.state is not RecorderState.RECORDING

    def _frame_to_restart_on(self) -> Frame | None:
        """Wait for a frame to launch the next part on; None if the take ended.

        For a restart that still has sound. ffmpeg opens its audio input before
        it reads a frame, and one whose input is missing exits within tens of
        milliseconds of launch. A restart launched with no frame to give it
        used to wait harmlessly on its pipe until the camera came back; now it
        would die at once, and again a second later, and that second failure
        finds DirectShow blaming the input or no longer listing it -- so an
        unplugged Blackmagic, whose Line In goes and comes back with its
        picture, would lose its sound for the rest of the take within a second
        of being pulled out. With a frame in hand first, nothing is launched
        until the camera delivers again, which for that unit is when the sound
        is back too.

        Only a frame captured after the death has been dealt with. The queue is
        emptied first at every failure, and a frame captured before then that
        arrives later is thrown away too. The pause before a restart empties it
        as well (_wait_before_restarting), but the first failure has no pause,
        and this used to take whatever was at the head of the queue: a frame
        the camera delivered before it was pulled, queued behind the write that
        found ffmpeg dead. The part was launched on it at once, died at once
        with DirectShow naming the input, and that second failure dropped the
        sound for the rest of the take. Frames thrown away are counted as lost
        to ffmpeg failing, since there was no ffmpeg to take them. While nothing
        arrives the take says so after STALL_TIMEOUT, because the writer is
        waiting here and not where it notices a camera that has stopped.

        A frame the camera hands over after that -- captured after its Line In
        failed, if a pulled unit's picture outlasts its sound -- still starts a
        part that dies at once. That death is not what costs the sound: before
        sound is given up on evidence, _camera_still_there watches for the
        camera stopping too.
        """
        # perf_counter, the clock Frame.timestamp is taken on as a frame is
        # pulled from the device.
        cutoff = time.perf_counter()
        thrown_away = self._discard_queued_frames()
        said = False
        try:
            while True:
                # Not given up for the disk floor: with no ffmpeg there is
                # nothing to finish, and the stop that follows the floor ends
                # this wait with its sentinel, as it ends a take with no frames.
                # A sentinel emptied out above is no loss: stop() leaves
                # RECORDING before it queues one.
                if self._abort.is_set() or self.state is not RecorderState.RECORDING:
                    return None
                try:
                    item = self._queue.get(timeout=0.1)
                except queue.Empty:
                    waited = time.perf_counter() - cutoff
                    if not said and waited >= STALL_TIMEOUT:
                        said = True
                        message = (
                            f"ffmpeg stopped, and no frames have come from the "
                            f"camera for {waited:.0f}s to start the next part on. "
                            f"Nothing is being recorded; it restarts as soon as "
                            f"they do."
                        )
                        log.error("%s", message)
                        self._set_state(
                            RecorderState.RECORDING, message,
                            only_if=RecorderState.RECORDING,
                        )
                    continue
                if item is None:
                    return None             # stop()'s sentinel: nothing follows
                if item.timestamp < cutoff:
                    thrown_away += 1        # captured before the death was dealt with
                    continue
                if said:
                    log.warning(
                        "Frames from the camera are arriving again; restarting ffmpeg"
                    )
                return item
        finally:
            self._book_frames_lost_to_the_restart(thrown_away)

    def _camera_still_there(self, evidence: str) -> bool | None:
        """Whether the camera goes on delivering, before sound is given up on
        ``evidence``.

        Sound given up stays given up for the take, and where sound and picture
        come from one unit, a Blackmagic's Line In, both go when it is pulled
        out. The recorder need not see them go at the same moment: ffmpeg can
        find the input gone before the last frames have come through, a restart
        launched on one of them dies at launch with DirectShow naming the input,
        and that is evidence gathered while the unit was going. Measured with
        stand-ins: an ffmpeg that failed at once, and a camera that
        delivered one or three more frames after it, lost the sound for the
        rest of the take though the unit was back three seconds later. Nothing
        here has timed a real Blackmagic being pulled, so nothing is assumed
        about how long that moment lasts.

        So the camera is watched first. True once it delivers a frame captured
        STALL_TIMEOUT after this began, never having gone that long without
        one. False the moment it goes STALL_TIMEOUT without a frame -- the
        recorder's measure of a camera that has stopped -- and the evidence is
        then not taken against the input: the take restarts with sound once
        the camera is back. None if the take ended meanwhile. It costs a take
        whose audio input really has gone STALL_TIMEOUT more picture, once,
        which is the price of not losing an act's sound to a cable pulled and
        put back. Frames taken meanwhile are counted as lost to ffmpeg failing:
        there is no ffmpeg to take them.
        """
        settings = self.settings
        name = settings.audio_device if settings is not None else None
        message = (
            f"The audio input looks to be the problem ({evidence}). Checking for "
            f"{STALL_TIMEOUT:.0f}s that the camera is still delivering before the "
            f"take goes on without sound; nothing is being recorded meanwhile."
        )
        log.warning("%s", message)
        if not self._set_state(
            RecorderState.RECORDING, message, only_if=RecorderState.RECORDING
        ):
            return None
        began = time.perf_counter()
        # Frames captured before now are no sign of the camera after the
        # failure, so they move this on no further.
        seen = began
        thrown_away = 0
        try:
            while True:
                if self._abort.is_set() or self.state is not RecorderState.RECORDING:
                    return None
                gone_for = time.perf_counter() - seen
                if gone_for >= STALL_TIMEOUT:
                    log.warning(
                        "Not taken against the audio input %r: the camera stopped "
                        "delivering too, for %.1fs from %.1fs after ffmpeg failed, "
                        "so sound and picture may have gone together, as they do "
                        "from a Blackmagic pulled out. The take keeps its sound, and "
                        "restarts with it once the camera is back.",
                        name, gone_for, seen - began,
                    )
                    return False
                try:
                    item = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    return None             # stop()'s sentinel: nothing follows
                thrown_away += 1
                seen = max(seen, item.timestamp)
                if item.timestamp >= began + STALL_TIMEOUT:
                    return True
        finally:
            self._book_frames_lost_to_the_restart(thrown_away)

    def _let_ffmpeg_finish_talking(self) -> None:
        """Give a dying ffmpeg a moment to exit and its last words to arrive.

        The write fails the instant the pipe breaks, which can be before the
        reader thread has been handed what ffmpeg printed on its way out -- so
        the reason logged, and whether the audio input is to blame, could be
        judged from a tail that did not hold it yet. Short, because frames are
        being dropped for as long as this waits.
        """
        process = self._process
        if process is not None:
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
        for reader in (self._stderr_thread, self._stdout_thread):
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=1.0)

    def _bank_ffmpeg_progress(self) -> None:
        """Add a finished ffmpeg's duplicated and dropped counts to the take's,
        and how much of its sound arrived."""
        _, duplicated, dropped = self._progress
        self._progress = (0, 0, 0)
        self.stats.ffmpeg_duplicated += duplicated
        self.stats.ffmpeg_dropped += dropped
        with self._sound_lock:
            sound, self._sound = self._sound, _SoundDelivery()
            # The stretch of silence is this ffmpeg's, not the next one's: a
            # restart opens the input again, and its first seconds are the
            # settle all over again. Whether the take has SAID so is the
            # take's, not the file's, and is not reset here.
            self._sound_silence = _SoundSilence()
            self._sound_heard_at = None
        self.stats.sound_delivered_seconds += sound.delivered
        self.stats.sound_timeline_seconds += sound.timeline

    def _reason_to_drop_audio(self) -> tuple[str | None, bool | None]:
        """What points at the audio input as why ffmpeg keeps failing, or None,
        and whether DirectShow still lists it.

        The picture is the part that cannot be had again, so a failing audio
        input must not take it down with it. But sound given up stays given up
        for the take, so it is not given up lightly:

        - Never on the first failure. A restart with sound is launched only
          once the camera has a frame to give it (_frame_to_restart_on),
          because ffmpeg opens its audio input before reading one and dies
          within tens of milliseconds when it cannot -- so while the camera is
          not delivering either, nothing is tried and no evidence against the
          input piles up, and where sound and picture come from one unit, a
          Blackmagic's Line In, both come back when it is plugged in again.
          Dropping the sound on the first failure would lose it for nothing
          there.
        - After that, when ffmpeg's own words point at the audio input; when
          ffmpeg has been killed for its audio input not opening in
          FIRST_WRITE_RESTART_AFTER twice running; or when DirectShow no longer
          lists the device. The kills count because nothing else can: an open
          that hangs prints nothing and leaves the device listed, and each kill
          used to be a failure with nothing pointing at the input, so a wedged
          input cost six kills and every pause between them before the take was
          even tried without it -- about 164 s of picture at the shipped values.
          ffmpeg reads no frame until the input has opened (see
          build_ffmpeg_command), and the kill comes only with a frame waiting
          in the pipe, so what it waited on was the input.
        - And even then only once the camera has been seen to go on delivering
          (_camera_still_there, called by _handle_ffmpeg_death with what this
          returns). Evidence gathered while the camera was going too is what a
          unit pulled out of its socket leaves, and is not held against the
          input.

        With neither, the take is still tried without sound rather than given
        up altogether -- a take that carries on without sound beats one that
        stops -- but that is a trial, not a finding, and _handle_ffmpeg_death
        says so. It used to come back from here as a reason, "ffmpeg failed 6
        times in a row with it", and was announced as though the input had been
        found at fault, on the soak whose microphone array turned out not to be.

        The listing comes back too, so that the trial's announcement says what
        the probe gave. None means it was not asked, or could not answer. The
        announcement said "the device list did not show it gone" whatever came
        back, including from a probe that raised, timed out or found no
        devices, when no list had been read at all. By the time of a trial it
        has been asked: a trial comes only after more than MAX_RESTARTS
        failures in a row, and these checks start at the second.
        """
        settings = self.settings
        if settings is None or not settings.audio_device:
            return None, None
        if self._consecutive_failures < 2:
            return None, None
        if self._audio_input_blamed():
            return "ffmpeg reported a problem with the audio input", None
        kills = self._opening_kills_in_a_row
        if kills >= 2:
            # Before the listing, which can take DirectShow fifteen seconds and
            # would not change the answer.
            return (
                f'the audio input "{settings.audio_device}" did not open in '
                f"{FIRST_WRITE_RESTART_AFTER:.0f}s, {kills} times in a row",
                None,
            )
        listed = _audio_device_listed(settings.audio_device)
        if listed is False:
            return (
                f'"{settings.audio_device}" is no longer listed as an audio input',
                listed,
            )
        return None, listed

    def _drop_audio(self) -> str | None:
        """Take the audio input out of the rest of the take. Returns its name."""
        settings = self.settings
        if settings is None:
            return None
        self.settings = replace(
            settings,
            audio_device=None,
            audio_sample_rate=None,
            audio_sample_bits=None,
            audio_channels=None,
        )
        self.audio_dropped_at = self.stats.elapsed
        self._opening_kills_in_a_row = 0
        return settings.audio_device

    def _verdict_on_the_sound(self, trial_after: frozenset[str] | None) -> str:
        """What a failed attempt without sound shows about the sound.

        For the message that ends the take; "" when the failure that ends it
        was not that attempt. The overnight soak's h264_mf ffmpeg failed six
        times in a row with the laptop's microphone array, was tried without
        it, and failed a seventh time. The same failure with no audio input at
        all would show the input did not cause it.

        "The same failure" used to be ffmpeg's last line, and there that line
        was "Nothing was written into output file", which ends any failure
        before a header is written (see _FFMPEG_EPITAPHS). An audio chain
        failing with sound and an encoder failing without it both end on it,
        and were called one failure. So the kinds of error ffmpeg gave are
        compared instead, its closing lines left out (_error_kinds), and the
        sound is cleared only when there were some and they match. That still
        clears it where it should be cleared: a header the muxer refused
        (rawvideo into MP4, from lavfi inputs) gave the same error lines on the
        bundled 8.1.2 build with an audio stream as without one.

        Failing with other errors shows only that something fails without the
        input too, and failing with no errors either time leaves nothing to
        compare. Neither clears the sound or blames it.
        """
        if trial_after is None:
            return ""
        errors = self._error_kinds()
        if trial_after and errors == trial_after:
            return (
                ", the last time without sound and with the same errors as with "
                "sound, so the sound was not the cause"
            )
        if not trial_after and not errors:
            return (
                ", the last time without sound; ffmpeg gave no errors to compare, "
                "with sound or without, so this does not show whether the sound "
                "was the cause"
            )
        return (
            ", the last time without sound and not with the same errors as with "
            "sound, so this does not show whether the sound was the cause"
        )

    def _wait_before_restarting(self, delay: float, failures: int) -> bool:
        """Pause before the next attempt. False if the take was stopped meanwhile.

        The camera goes on delivering while this waits, and with no ffmpeg
        there is nothing to write its frames to, so they are taken off the
        queue as they arrive and counted as dropped. They used to be left
        where they were, and the queue was full within seconds. The part after
        the pause then began with up to 90 frames from before and during it --
        as much as half a minute old -- handed to the new ffmpeg in a burst as
        though they were live, and the file is timed by when frames reach it.
        The frames that did not fit were counted, and logged, as the encoder
        not keeping up, with no encoder running. And a Stop pressed meanwhile
        found a full queue that nothing was reading.
        """
        message = (
            f"ffmpeg has failed {failures} times in a row; trying again in "
            f"{delay:.0f}s. Nothing is being recorded until it starts."
        )
        log.warning("%s", message)
        if not self._set_state(
            RecorderState.RECORDING, message, only_if=RecorderState.RECORDING
        ):
            return False
        deadline = time.perf_counter() + delay
        thrown_away = 0
        try:
            while True:
                # Emptied first on every pass, the last one included, so the
                # new part is given only frames that arrive while it launches.
                thrown_away += self._discard_queued_frames()
                if self._abort.is_set() or self.state is not RecorderState.RECORDING:
                    return False
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return True
                self._abort.wait(min(0.1, remaining))
        finally:
            self._book_frames_lost_to_the_restart(thrown_away)

    def _book_frames_lost_to_the_restart(self, count: int) -> None:
        """Count frames thrown away while ffmpeg was being replaced, and log it.

        As dropped, and as lost to ffmpeg failing: there was no ffmpeg to take
        them. For _wait_before_restarting and _frame_to_restart_on.
        """
        if not count:
            return
        stats = self.stats
        with self._lock:                    # see _note_drop
            stats.frames_dropped += count
            stats.frames_dropped_ffmpeg_failing += count
            # The line below is this reason's report; see _report_drops.
            self._drops_reported[True] = (
                stats.frames_dropped_ffmpeg_failing, time.perf_counter()
            )
            total = stats.frames_dropped
        log.warning(
            "%d frame(s) arrived while there was no ffmpeg to write them to, "
            "and were dropped; %d dropped so far this take",
            count, total,
        )

    def _next_part_path(self) -> Path:
        base = self._base_path
        assert base is not None
        number = len(self.parts) + 1
        candidate = base.with_name(f"{base.stem}-part{number}{base.suffix}")
        while candidate.exists():
            number += 1
            candidate = base.with_name(f"{base.stem}-part{number}{base.suffix}")
        return candidate

    def _close_current_part(self) -> None:
        """Record how much footage the part that just ended actually contains.

        Measured from the frames written, not from the wallclock. The two are
        not the same thing and the difference is not academic: the file's t=0
        is the first frame reaching ffmpeg, which it reads only once its audio
        input has opened, and if the camera dies mid-take the wallclock keeps
        running while the file does not. A camera unplugged four seconds into a fifteen-second take
        gave a 14.6s part duration over a 4.0s file, and this number is what
        write_chapters is handed as the total duration -- so cues were written
        at 8s and 12s into a video that ended at 4s.
        """
        if not self.parts:
            return
        part = self.parts[-1]
        if self._part_first_write:
            part.duration = max(0.0, self.stats.last_frame_at - self._part_first_write)
        else:
            part.duration = 0.0
        part.frames = self.stats.frames_written - sum(
            other.frames for other in self.parts[:-1]
        )
        # Set, never added to: this can run twice for the same part (a death,
        # then stop()), and the take's size is totalled from these.
        try:
            part.size_bytes = part.path.stat().st_size
        except OSError:
            pass

    def _verify_parts(self) -> None:
        """Mark any part that did not end up holding a picture.

        This is the only thing standing between a dead file and a status bar
        that says "Saved", so it may not be a proxy for the question. It used
        to test `st_size == 0`, which stopped meaning anything the moment
        `-flush_packets 1` was added to the command: ffmpeg now puts the
        container header on disk within milliseconds of launching, before a
        single frame has been encoded. Measured, with ffmpeg frozen mid-take
        and the recording then stopped: a 593-byte MKV that ffmpeg itself
        answers "Error opening input: End of file" for, reported as
        "Saved suspend.mkv". Zero bytes is now only the MP4 case (delay_moov
        holds everything back until the first fragment).

        So: ask ffmpeg. A byte count cannot tell a header from a picture, and
        the next header that grows past whatever number we picked would put the
        silence straight back. The probe is skipped for anything big enough to
        be beyond doubt, which is every normal take, so a stop costs no extra
        process; only a suspiciously small file is opened, and that costs about
        a tenth of a second.

        Runs again in stop() once ffmpeg has actually exited, and the verdict
        is recomputed rather than latched: a file is still short while the
        process is flushing, and an earlier "empty" that is no longer true must
        not stick to a recording that is in fact fine.
        """
        for part in self.parts:
            usable, reason = self._part_holds_video(part)
            if part.usable and not usable:
                log.error(
                    "%s holds no video: %s. Those %d frames are lost.",
                    part.path.name, reason, part.frames,
                )
            part.usable = usable

    def _part_holds_video(self, part: RecordingPart) -> tuple[bool, str]:
        if not part.path.is_file():
            return False, "the file is not there"
        try:
            size = part.path.stat().st_size
        except OSError as exc:
            return False, f"it cannot be read ({exc})"
        if size == 0:
            return False, "the file is empty"
        if size >= PROBE_BELOW_BYTES:
            return True, ""

        opened = _ffmpeg_can_open(part.path)
        if opened is None:
            # No answer available (no ffmpeg to ask, or it hung). Fall back to
            # the header-only size, which is the thing actually being caught.
            if size < HEADER_ONLY_BYTES:
                return False, f"it is {size} bytes -- a header and nothing else"
            return True, ""
        if not opened:
            return False, f"ffmpeg cannot open the {size}-byte file"
        return True, ""

    def _drain_stderr(self) -> None:
        """Read ffmpeg's stderr continuously.

        Not optional. A subprocess whose stderr is never read blocks once the
        pipe buffer fills, and ffmpeg with -stats writes a progress line
        constantly. Without this the recording would freeze a few seconds in.

        Read in chunks and split on CARRIAGE RETURNS as well as newlines,
        because -stats separates its progress lines with \\r and nothing else.
        readline() therefore returned nothing at all until ffmpeg exited, and
        then handed over the entire session's stderr as one string: measured,
        zero lines in eight seconds of recording, then a single 1,884-character
        blob at exit. So the recent-output deque held one entry, the tail the
        UI is meant to show was empty for the whole recording, and when ffmpeg
        died the reason reported to the operator was `lines[-1][:200]` -- the
        first 200 characters of that blob, which is the progress counter from
        the first second. The real message was at the far end and never seen.
        """
        process = self._process
        if process is None or process.stderr is None:
            return
        stream = process.stderr
        pending = ""
        while True:
            try:
                chunk = stream.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            pending += chunk.decode("utf-8", errors="replace")
            pieces = re.split(r"[\r\n]", pending)
            # The last piece has no terminator yet; it is the line ffmpeg is
            # still writing. Hold it until the rest of it arrives.
            pending = pieces.pop()
            for piece in pieces:
                self._note_stderr(piece, process)
        self._note_stderr(pending, process)
        # Its stderr has closed, so ffmpeg has exited or is about to, which
        # start() may be waiting to hear.
        self._launch_news.set()

    #: The start of a -stats progress line, and the counters read from it.
    #: Measured on the bundled 8.1 build: "frame=   63 fps= 31 q=-1.0
    #: Lsize=  6KiB time=00:00:02.03 bitrate=  26.0kbits/s dup=3 drop=0 ...".
    _PROGRESS = re.compile(r"^frame=\s*(\d+)")
    _PROGRESS_COUNTER = re.compile(r"\b(dup|drop)=\s*(\d+)")

    def _note_stderr(
        self, line: str, process: subprocess.Popen[bytes] | None
    ) -> None:
        """Keep one line of ffmpeg's stderr, unless it is just the progress counter.

        ``process`` is the ffmpeg that printed it, which is not always the
        current one by the time the line is read; see _note_progress.

        Progress lines are read for their counters and not stored, so the
        40-entry tail holds forty things ffmpeg actually said. Storing them
        meant the tail was forty copies of the frame counter and the one line
        explaining the failure had long since fallen off the end.

        Everything else is logged at WARNING or worse; see _ffmpeg_level. It
        was logged at DEBUG unless it said "error", "failed" or "invalid", and
        the app logs at INFO, so every other thing ffmpeg complained about in
        a take -- however long it went on complaining -- reached no log at
        all. It goes through _RepeatGate, so a warning printed once a frame
        costs the log a line a minute rather than thirty a second.
        """
        line = line.strip()
        if not line:
            return
        if line.startswith("frame="):
            self._note_progress(line, process)
            return
        self._stderr_tail.append(line)
        self._launch_news.set()             # start() may be waiting on a word
        said = self._stderr_gate.offer(line)
        if said is not None:
            log.log(_ffmpeg_level(line), "ffmpeg: %s", said)

    def _note_progress(
        self, line: str, process: subprocess.Popen[bytes] | None
    ) -> None:
        match = self._PROGRESS.match(line)
        if match is None:
            return
        frame = int(match.group(1))
        counters = dict(self._PROGRESS_COUNTER.findall(line))
        moved = frame != self._progress[0]
        self._progress = (
            frame, int(counters.get("dup", 0)), int(counters.get("drop", 0))
        )
        if moved:
            self._progress_moved_at = time.perf_counter()
            # The ffmpeg that printed this, encoding: the frames it could not
            # take meanwhile were it being slow, not failing -- provided it is
            # the one running now and not one already found dead, which
            # _judge_pending_drops decides under the lock. Looked at here
            # without it first, so a healthy take does not take the lock twice
            # a second. See _note_drop.
            if process is not None and (
                self._failed_ffmpeg is not None or self._drops_pending
            ):
                self._judge_pending_drops(ffmpeg_failed=False, counted_by=process)

    #: One SOUND_STATS_FORMAT line, as the bundled 8.1.2 build prints it:
    #: "wer-sound tb=1/48000 pts=2048 samp=1024". A zero denominator cannot
    #: match, so a torn or malformed line is read as unreadable rather than
    #: dividing by nothing in the one thread that drains ffmpeg's stdout.
    _SOUND_STATS = re.compile(r"^wer-sound tb=(\d+)/([1-9]\d*) pts=(-?\d+) samp=(\d+)$")

    #: ametadata's two lines for one second of sound, on the same pipe: the
    #: frame it measured, then the measurement. As the bundled 8.1.2 build
    #: prints them, verbatim:
    #:
    #:     frame:3    pts:144000   pts_time:3
    #:     lavfi.astats.Overall.Peak_level=-inf
    #:
    #: The time is where in the sound the second begins, in the same timeline
    #: as the delivery statistics' pts. It is "N/A" for a frame with no
    #: timestamp, which matches here and is read as no time at all rather than
    #: as a line nobody can read.
    _SOUND_FRAME = re.compile(r"^frame:\d+\s+pts:\S+\s+pts_time:(\S+)$")
    #: The peak in dBFS, or "-inf" when every sample in the second was zero.
    _SOUND_LEVEL = re.compile(
        rf"^{re.escape(SOUND_LEVEL_KEY)}=(-?(?:inf|\d+(?:\.\d+)?))$"
    )

    def _read_sound_stats(self, process: subprocess.Popen[bytes]) -> None:
        """Read one ffmpeg's sound statistics from its stdout, to the end.

        Not optional either. A pipe nobody reads fills up, and ffmpeg then
        waits to write the next line, which at 48 kHz comes 47 times a second.
        Every line ends in a newline and nothing outside ffmpeg writes here, so
        unlike stderr there is nothing to split them from. Two of ffmpeg's own
        writers share this pipe -- the delivery statistics and the peak levels
        -- and measured on the bundled build, over ten seconds of both, not one
        line of either was torn.

        Reading survives anything the parsing does, the way the watchdog does:
        an exception out of the handler would end this thread, and an ffmpeg
        whose stdout nobody drains stops taking frames within seconds, which
        reaches the operator as a stalled encoder with no hint of the cause.
        """
        stream = process.stdout
        if stream is None:
            return
        pending = b""
        while True:
            try:
                chunk = stream.read(4096)
            except (OSError, ValueError) as exc:
                # Not silently: a drain that stopped early is why an ffmpeg
                # would go on to wedge, and the log is where that is worked out.
                log.warning("Reading ffmpeg's sound statistics stopped: %s", exc)
                break
            if not chunk:
                break
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                self._note_sound_stats(line.decode("utf-8", errors="replace"), process)
        self._note_sound_stats(pending.decode("utf-8", errors="replace"), process)

    def _note_sound_stats(
        self, line: str, process: subprocess.Popen[bytes] | None
    ) -> None:
        """Read one line of them, and say once a take what it shows.

        Three kinds of line arrive here, all on the one pipe: a frame of sound
        reaching the encoder, and ametadata's pair naming a second of sound and
        giving its peak level. The first answers how much of the input's sound
        is arriving, the other two whether what arrives is anything but digital
        silence, and neither question answers the other.

        ``process`` is the ffmpeg that printed the line. A line from any other,
        one already replaced after a failure, is not the current file's sound.
        """
        share: float | None = None
        silent = False
        try:
            line = line.strip()
            if not line or process is not self._process:
                return
            sound = self._sound
            match = self._SOUND_STATS.match(line)
            if match is not None:
                num, den, pts, samples = (int(value) for value in match.groups())
                # tb is the audio encoder's time base, which ffmpeg sets to one
                # over the sample rate, so a sample lasts one tick of it.
                with self._sound_lock:
                    self._sound_heard_at = time.perf_counter()
                    share = sound.note(pts * num / den, samples * num / den)
            elif (frame := self._SOUND_FRAME.match(line)) is not None:
                with self._sound_lock:
                    self._sound_silence.note_frame(_seconds_or_none(frame.group(1)))
            elif (level := self._SOUND_LEVEL.match(line)) is not None:
                # float() reads "-inf", which is what a second of exact zeros
                # comes out as -- the very thing being looked for.
                with self._sound_lock:
                    silent = self._sound_silence.note_peak(float(level.group(1)))
            else:
                if not sound.unreadable_said:
                    sound.unreadable_said = True
                    log.warning(
                        "Could not read a line of ffmpeg's sound statistics: %r. "
                        "Lines like it are left out of the check that this "
                        "file's sound is arriving whole.", line[:120],
                    )
                return
        except Exception:  # noqa: BLE001 - the drain must outlive this
            log.exception("Reading a line of ffmpeg's sound statistics raised")
            return
        if share is not None:
            self._say_the_sound_is_short(share)
        if silent:
            self._say_the_sound_is_silence()

    def _check_the_sound_is_arriving(self) -> None:
        """Notice an audio input that has stopped sending anything at all.

        On the watchdog, because there is nothing else to notice it by: the
        statistics simply stop, and a check driven by them alone scored a dead
        input as having delivered all of its sound. Only for an input that was
        delivering -- until the first line arrives there is nothing to say the
        device was ever opened, and ffmpeg may still be opening it.
        """
        if self.state is not RecorderState.RECORDING:
            return
        settings = self.settings
        process = self._process
        if settings is None or not settings.audio_device:
            return
        if process is None or process.poll() is not None:
            return
        with self._sound_lock:
            heard_at = self._sound_heard_at
            if heard_at is None:
                return
            quiet = time.perf_counter() - heard_at
            if quiet < SOUND_SILENCE_SECONDS:
                return
            self._sound_heard_at = time.perf_counter()
            share = self._sound.note_silence(quiet)
        if share is not None:
            self._say_the_sound_is_short(share, silent_for=quiet)

    def _say_the_sound_is_short(
        self, share: float, *, silent_for: float | None = None
    ) -> None:
        """Tell the operator, once a take, that the sound is not all arriving.

        ``silent_for`` is how long nothing at all has arrived, when that is
        what raised this. The share on its own would not say so: it is taken
        over the last half minute, so a microphone that dies after an hour of
        perfect sound first reads as "only 40%" -- which sends whoever is in
        the booth looking for a fault in the sound rather than for a
        microphone that has stopped.
        """
        if self._sound_short_said:
            return
        settings = self.settings
        name = settings.audio_device if settings is not None else None
        output = self.stats.output_path
        # Named, because this is pinned to the status bar and a pin outlives
        # the take it was raised in: "this take's sound has gaps" was still
        # there, in the present tense, over the next take's perfectly good
        # sound.
        where = output.name if output is not None else "the recording"
        if silent_for is not None:
            message = (
                f'No sound at all has arrived from "{name}" for {silent_for:.0f} '
                f"seconds, so {where} has none from there on. The picture is "
                f"unaffected. Check the input, or record sound from another one."
            )
        else:
            message = (
                f'Only {share:.0%} of the sound from "{name}" is reaching '
                f"{where}, so its sound has gaps in it. The picture is "
                f"unaffected. If this input keeps doing it, record sound from "
                f"another one."
            )
        if self._say_about_the_sound(message):
            self._sound_short_said = True

    def _say_the_sound_is_silence(self) -> None:
        """Tell the operator, once a take, that the input is delivering zeros.

        Its own message and its own flag, because it is its own fault and its
        own remedy: the sound is arriving, all of it, and there is nothing in
        it. Saying "only 0% of the sound is reaching" instead would send the
        booth looking for the gaps that the delivery check reports, and there
        are none to find.
        """
        if self._sound_silent_said:
            return
        settings = self.settings
        name = settings.audio_device if settings is not None else None
        output = self.stats.output_path
        # Named, for the reason the message above is: this is pinned to the
        # status bar, and a take started from the console leaves the pin up
        # (main_window._start_recording). "The take carries on", left standing
        # over the next act, would be read as the next act's.
        where = output.name if output is not None else "the recording"
        # The two causes worth naming, because between them they are what it
        # has been every time: a laptop microphone whose effects gate it shut,
        # and an HDMI feed with no sound embedded in it. Both are fixed away
        # from Wer -- in Windows Sound settings, or at the desk -- so the
        # message says what to look at rather than offering a button.
        message = (
            f"No sound from {name}: only digital silence for "
            f"{SOUND_SILENT_SECONDS:.0f} s, so {where} has none in it. A "
            f"laptop microphone with voice effects, or a capture device whose "
            f"HDMI carries no sound, does this. The picture is unaffected and "
            f"the take was not stopped."
        )
        if self._say_about_the_sound(message):
            self._sound_silent_said = True

    def _say_about_the_sound(self, message: str) -> bool:
        """Put one warning about the sound on the status bar and in the log.

        Returns whether it was said. A take that is not RECORDING is told
        nothing -- ffmpeg's statistics start arriving while it is still
        STARTING -- and the caller then leaves its flag down, so the next
        judgement can say it instead of the take losing the warning outright.
        """
        if not self._set_state(
            RecorderState.RECORDING, message, only_if=RecorderState.RECORDING
        ):
            return False
        log.warning("%s", message)
        if self._on_sound_warning is not None:
            try:
                self._on_sound_warning(message)
            except Exception:  # noqa: BLE001
                log.exception("Sound warning handler raised")
        return True

    # ------------------------------------------------------------------- stop

    def stop(self) -> Path | None:
        """Finish the recording cleanly. Returns the output path.

        Closes stdin and waits for ffmpeg to flush and write its trailer. That
        wait matters: killing ffmpeg here is what produces a file that will not
        seek, and the whole point is a file that drops into an edit timeline.
        """
        if self.state is RecorderState.IDLE:
            return None

        self._set_state(RecorderState.STOPPING, "flushing buffered frames")
        output = self.stats.output_path

        # Waits for room, deliberately: the sentinel must get into the queue
        # even when it is full, and it must sit behind the frames already
        # there so they are written first.
        #
        # But only while something is still reading the queue. Once the disk
        # floor or a failure has ended the writer, a full queue made this wait
        # out the whole SENTINEL_TIMEOUT for nobody, then log that the sentinel
        # could not be queued as though that were the problem. The writer can
        # also end WHILE this waits -- one whose ffmpeg dies during the drain
        # returns without reading on -- and a single blocking put cannot
        # notice that. So it asks for room a tenth of a second at a time, and
        # stops asking once the writer has gone.
        writer = self._writer
        if writer is not None and writer.is_alive():
            deadline = time.perf_counter() + SENTINEL_TIMEOUT
            while True:
                try:
                    self._queue.put(None, timeout=0.1)
                    break
                except queue.Full:
                    if not writer.is_alive():
                        break
                    if time.perf_counter() >= deadline:
                        log.error(
                            "Could not queue the stop sentinel; aborting the writer"
                        )
                        self._abort.set()
                        break

        writer_stuck = False
        ffmpeg_killed = False
        if self._writer is not None:
            self._writer.join(timeout=WRITER_JOIN_TIMEOUT)
            if self._writer.is_alive():
                log.error(
                    "Writer did not drain in %gs; some frames may be lost",
                    WRITER_JOIN_TIMEOUT,
                )
                self._abort.set()
                # Kill ffmpeg NOW, before going anywhere near stdin.
                #
                # A writer still alive at this point is blocked inside
                # stdin.write() -- there is no timeout on that call -- which
                # happens when ffmpeg stops draining the pipe but stays alive:
                # a wedged hardware encoder, a stalled USB or network volume,
                # an SMR disk pausing. _abort does not reach it, because it is
                # only tested at the top of the loop. Closing the handle does
                # not either: close() blocks against the concurrent write, so
                # stop() never reached process.wait() and never reached the
                # terminate/kill fallback written for exactly this case.
                # Measured: stop() still running after 60s, released only by
                # killing ffmpeg from outside. Killing it here breaks the pipe,
                # which is the one thing that unblocks the write.
                self._kill_process()
                ffmpeg_killed = True
                self._writer.join(timeout=5.0)
                writer_stuck = self._writer.is_alive()

        # Frames still queued at the top of stop() are written during the drain
        # above, so the part's real length is only known now.
        self._close_current_part()

        process = self._process
        if process is not None:
            if process.stdin is not None and not writer_stuck:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                log.error(
                    "ffmpeg did not exit within %gs; terminating. The file may "
                    "be incomplete.", SHUTDOWN_TIMEOUT,
                )
                ffmpeg_killed = True
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()

        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2.0)
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=2.0)
        self._bank_ffmpeg_progress()
        # Whatever was turned away and not yet settled, now that ffmpeg's last
        # words are in. One that had to be killed to end the take was not
        # taking frames. See _note_drop.
        self._judge_pending_drops(ffmpeg_failed=ffmpeg_killed)
        for line in self._stderr_gate.unreported():
            log.log(_ffmpeg_level(line), "ffmpeg: %s", line)

        # Now that ffmpeg has exited and flushed, the files on disk are final.
        self._verify_parts()
        self._cleanup()
        # Nothing will write these now, and at 1080p a full queue is half a
        # gigabyte that used to stay referenced until the next take.
        self._discard_queued_frames()

        usable = [part for part in self.parts if part.usable and part.exists]
        self._measure_parts()
        self._log_take_summary(usable)
        footnote = self._take_footnote()
        if usable:
            for part in usable:
                log.info(
                    "Recorded %s: %d frames, %.1f MB, %.1fs",
                    part.path.name, part.frames,
                    part.size_bytes / 1_000_000, part.duration,
                )
            if len(usable) == 1:
                self._set_state(
                    RecorderState.IDLE, f"Saved {usable[0].path.name}{footnote}"
                )
            else:
                # Say it plainly: more than one file is not what was asked for,
                # and the user needs to know before they go looking for one.
                self._set_state(
                    RecorderState.IDLE,
                    f"Saved in {len(usable)} parts after an encoder failure: "
                    + ", ".join(part.path.name for part in usable)
                    + footnote,
                )
        else:
            # Not "was not written": with -flush_packets there is very often a
            # file sitting there, a container header of a few hundred bytes
            # with no picture in it, and sending someone to look for a file
            # that exists and is useless wastes the minutes this message is
            # meant to save.
            self._set_state(
                RecorderState.ERROR,
                "No video was written. Nothing in this take can be recovered.",
            )
        return usable[-1].path if usable else output

    def _measure_parts(self) -> None:
        """Final sizes, for the summary and for the panel once the take is over."""
        for part in self.parts:
            try:
                part.size_bytes = part.path.stat().st_size
            except OSError:
                pass
        self.stats.note_size(
            time.perf_counter(), sum(part.size_bytes for part in self.parts)
        )

    def _log_take_summary(self, usable: list[RecordingPart]) -> None:
        """One line that says how the whole take went, drops included.

        The per-part lines gave frames, megabytes and seconds and nothing
        else, and the running drop count in the log stopped at 1,000, so a
        take that lost a fifth of its frames finished with the same lines as a
        perfect one. At WARNING when anything was dropped, so it is found by
        whoever searches the log for trouble rather than only by whoever
        reads it end to end. The frames ffmpeg failing cost are said apart
        whenever it cost any, so a take like the soak's is not read as an
        encoder too slow for its picture. How much of the sound arrived is given
        whenever it was measured, so a take whose sound had gaps is found the
        same way, and a take whose figure is missing shows the check did not
        run.
        """
        stats = self.stats
        failing = stats.frames_dropped_ffmpeg_failing
        behind = stats.frames_dropped - failing
        if not failing:
            why = ""
        elif not behind:
            why = ", all because ffmpeg failed"
        else:
            why = (
                f", {failing} because ffmpeg failed and {behind} because the "
                f"encoder fell behind"
            )
        share = stats.sound_delivered_share
        sound_short = share is not None and share < SOUND_SHORT_BELOW
        log.log(
            logging.WARNING if stats.frames_dropped or sound_short else logging.INFO,
            "Take finished: %d frames written, %d dropped by the recorder "
            "(%.2f%% of the frames offered)%s; ffmpeg duplicated %d and "
            "dropped %d to hold the frame rate; %d of %d part(s) usable, %.1f "
            "MB, %d restart(s)%s%s",
            stats.frames_written, stats.frames_dropped, stats.drop_percent, why,
            stats.ffmpeg_duplicated, stats.ffmpeg_dropped,
            len(usable), len(self.parts), stats.file_size_mb, self._restarts,
            "" if self.audio_dropped_at is None
            else f"; no sound from {_clock(self.audio_dropped_at)} on",
            "" if share is None else f"; {share:.1%} of the sound arrived",
        )

    def _take_footnote(self) -> str:
        """What "Saved" must not be left to imply: the take is not all there."""
        notes = []
        if self.stats.frames_dropped:
            notes.append(
                f"{self.stats.frames_dropped:,} frames dropped "
                f"({self.stats.drop_percent:.1f}%)"
            )
        if self.audio_dropped_at is not None:
            notes.append(f"no sound from {_clock(self.audio_dropped_at)} on")
        share = self.stats.sound_delivered_share
        if share is not None and share < SOUND_SHORT_BELOW:
            notes.append(f"only {share:.0%} of the sound arrived")
        return f" — {'; '.join(notes)}" if notes else ""

    def _discard_queued_frames(self) -> int:
        """Empty the queue. Returns how many frames it held, a sentinel aside."""
        frames = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return frames
            if item is not None:
                frames += 1

    def _cleanup(self) -> None:
        self._watchdog_stop.set()
        self._process = None
        self._writer = None
        self._watchdog = None
        self._stderr_thread = None
        self._stdout_thread = None

    @property
    def queue_depth(self) -> int:
        """How far behind the encoder is, which the UI shows live."""
        return self._queue.qsize()

    @property
    def queue_capacity(self) -> int:
        return self._queue.maxsize
