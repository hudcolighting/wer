"""Embed chapters into a finished recording.

No Qt.

Why this exists
---------------
A recording gets a ``.chapters.txt`` sidecar in ffmetadata form. That is the
right file to hand an editor, but nothing reads it on its own -- it is an *input*
to ffmpeg, not something a player picks up. Double-clicking the video in VLC
shows no chapters at all.

Embedding them into the video file is what makes the recording actually
navigable: VLC lists them under Playback → Chapter, mpv shows them, and Resolve
and Premiere import them with the file. For a four-hour tech that is the
difference between "here is a list of cue times" and "press a key to jump to
cue 412".

What goes in depends on the container
-------------------------------------
Chapters and tags go into MKV and MP4 alike. The marker list and the bus log go
in as attachments only in MKV, because MP4 cannot hold attachments: asked to,
ffmpeg fails the whole rewrite, chapters and all. An MP4 take is rewritten
without them, and they stay beside it. See _HOLDS_ATTACHMENTS.

The cost, stated plainly
------------------------
ffmpeg cannot add chapters to an existing file in place; it has to write a new
one. The video and audio are stream-copied, so nothing is re-encoded and no
quality is lost, but every byte is still rewritten. A 20 GB tech takes a couple
of minutes on an SSD and needs 20 GB of free space while it runs.

So the original is never touched until the new file is complete and verified.
If anything goes wrong the recording is exactly as it was, and the sidecars are
still there to fall back on.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from wer.paths import ffmpeg_path

log = logging.getLogger(__name__)

# Every subprocess call in this module reads ffmpeg's output as UTF-8 with
# errors="replace", never text=True. This is not style.
#
# text=True decodes with the locale codec -- cp1252 on a UK or US Windows --
# and ffmpeg echoes the file path back in UTF-8. A path containing any
# character whose UTF-8 uses one of the five bytes cp1252 leaves undefined
# (0x81, 0x8D, 0x8F, 0x90, 0x9D) kills subprocess's reader thread with a
# UnicodeDecodeError; the call then returns with stderr set to None, and the
# first thing that touches it raises a TypeError that no except clause here is
# looking for. Latvian a-macron, Polish L-stroke and Czech c-caron all fall in
# that set, and {show} goes straight into the filename template -- so it takes
# one accented show name to reach it. Reproduced with a file named
# show_a<U+0101>.mkv, which raised under text=True and reads fine under this.
#
# It matters more here than anywhere else: chapter embedding runs at the end of
# every single take.

__all__ = [
    "embed_chapters",
    "EmbedControl",
    "count_chapters",
    "list_attachments",
    "extract_attachments",
    "RemuxResult",
]

_PROGRESS = re.compile(r"time=(\d+):(\d+):(\d+)\.(\d+)")

#: How long to keep trying to delete a temporary copy whose ffmpeg has just
#: been killed. See _discard.
DISCARD_RETRY_SECONDS = 2.0


class RemuxResult:
    """Outcome of an embed attempt."""

    def __init__(
        self,
        ok: bool,
        path: Path,
        detail: str = "",
        elapsed: float = 0.0,
        *,
        cancelled: bool = False,
        attached: Sequence[Path] = (),
        left_out: Sequence[Path] = (),
    ):
        self.ok = ok
        self.path = path
        self.detail = detail
        self.elapsed = elapsed
        #: True when the rewrite was stopped through its EmbedControl rather
        #: than failing by itself. ``detail`` is then the reason it was given.
        self.cancelled = cancelled
        #: The attachments the finished file now holds. A caller deletes a
        #: sidecar on the strength of this, so it names only what went into the
        #: file, never merely what was asked for. Empty unless ``ok``.
        self.attached = tuple(attached)
        #: Attachments that were asked for but that this kind of file cannot
        #: hold, so were never put in: they exist only beside the video. See
        #: _HOLDS_ATTACHMENTS. Empty unless ``ok``.
        self.left_out = tuple(left_out)

    def __bool__(self) -> bool:
        return self.ok


class EmbedControl:
    """A way into a chapter rewrite from another thread: which file it is
    writing, and a way to stop it.

    embed_chapters holds its caller in a readline on ffmpeg's stderr for the
    whole rewrite -- minutes for a long take on a slow disk -- and the process
    lived in a local variable nothing else could reach. Two things need to.
    Shutdown gave up on a rewrite still running once its bounded wait ran out
    and exited: the ffmpeg carried on with nobody reading it, and the full-size
    ``.chapters-tmp`` copy it was writing stayed beside the video for good,
    because only embed_chapters' own failure branches ever delete it. And a
    take recording on the same drive can be stopped at its disk floor by that
    copy, seconds before the rewrite would have given the space back.

    ``cancel`` kills ffmpeg and returns. embed_chapters, still running on its
    own thread, sees the pipe close, deletes the temporary copy and returns a
    result with ``cancelled`` set; the original is never touched on that path.
    A cancel that lands after ffmpeg has already exited cleanly changes
    nothing: the finished file is swapped in, which frees the same space.

    One control can serve several rewrites in turn (one per part of a take),
    and a cancel outlasts the rewrite it landed on: a later embed_chapters
    given the same control returns at once without starting ffmpeg. Safe to
    use from any thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._reason = ""
        #: The file being rewritten and its temporary copy, from the moment a
        #: rewrite begins. Left set afterwards, so a caller can still say which
        #: file it was.
        self.video: Path | None = None
        self.temporary: Path | None = None

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return bool(self._reason)

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @property
    def running(self) -> bool:
        """True while an ffmpeg started through this control has not exited."""
        with self._lock:
            process = self._process
        return process is not None and process.poll() is None

    def temporary_bytes(self) -> int:
        """What the temporary copy holds right now; 0 when there is none."""
        temporary = self.temporary
        if temporary is None:
            return 0
        try:
            return temporary.stat().st_size
        except OSError:
            return 0

    def cancel(self, reason: str) -> None:
        """Stop the rewrite in progress, and any later one given this control.

        Does not wait for ffmpeg to exit or for the copy to be deleted: the
        thread inside embed_chapters does both, and a caller may be one that
        must not block.
        """
        reason = reason or "the rewrite was stopped"
        with self._lock:
            if self._reason:
                return
            self._reason = reason
            process = self._process
        if process is not None:
            log.warning("Stopping the chapter rewrite of %s: %s",
                        self.video.name if self.video else "a take", reason)
            _kill(process)

    def _attach(self, process: subprocess.Popen[bytes]) -> bool:
        """Called once ffmpeg is running. False when a cancel got there first,
        in which case the caller kills the process itself."""
        with self._lock:
            if self._reason:
                return False
            self._process = process
            return True

    def _detach(self) -> None:
        with self._lock:
            self._process = None


def _kill(process: subprocess.Popen[bytes]) -> None:
    """Kill an ffmpeg that is still running. Never raises."""
    if process.poll() is not None:
        return
    try:
        process.kill()
    except OSError:
        # Popen.kill already ignores a process that exited in between. Anything
        # else is worth a line, and the wait that follows says the rest.
        log.exception("Could not stop ffmpeg (pid %s)", process.pid)


def _discard(temporary: Path) -> None:
    """Delete a temporary copy, and say so if it cannot be deleted.

    Retried, because the likeliest caller has just killed the ffmpeg writing
    it. Assumed rather than measured: Windows can report a killed process as
    gone a moment before every handle it held is closed, and deleting a file
    that is still open fails with a sharing violation. A short retry covers
    that without a wait on the process that could itself hang.

    Never raises. Every caller is already reporting something -- a failure, a
    timeout, a stop -- and an OSError out of here would escape embed_chapters
    and replace that report with "finishing the take failed".
    """
    deadline = time.monotonic() + DISCARD_RETRY_SECONDS
    while True:
        try:
            temporary.unlink(missing_ok=True)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                log.error(
                    "Could not delete the temporary copy %s: %s. The recording "
                    "beside it is untouched, and this file is safe to delete.",
                    temporary, exc,
                )
                return
            time.sleep(0.1)


#: MIME types for the sidecars we attach, by suffix. Matroska stores a
#: mimetype per attachment and tools use it to decide what to do with one.
_MIMETYPES = {
    ".csv": "text/csv",
    ".jsonl": "application/x-ndjson",
    ".json": "application/json",
    ".txt": "text/plain",
    ".wer": "application/json",
}

#: Suffixes of the files embed_chapters puts attachments into: Matroska only.
#:
#: MP4 cannot hold them. Measured with the bundled ffmpeg 8.1.2 on tiny
#: fragmented MP4s from libx264 and from h264_mf, with this module's own
#: command: any -attach made the mp4 muxer refuse the file ("Could not find tag
#: for codec none in stream #2, codec not currently supported in container",
#: then "Could not write header"), ffmpeg exited 127, and the chapters the
#: rewrite was for went with it. The same command without -attach embedded the
#: chapters and the tags. An hour-long MP4 soak lost all 1,685 of its chapters
#: that way, reported as a failed embed.
#:
#: Anything not listed is treated as unable to hold them. Wer writes only MKV
#: and MP4, so in practice that means MP4; for any other kind of file it is the
#: safe way to be wrong, because the sidecars then stay beside the video rather
#: than being deleted in the belief that they are inside it.
_HOLDS_ATTACHMENTS = frozenset({".mkv"})


def embed_chapters(
    video: Path,
    chapters: Path,
    *,
    attachments: list[Path] | None = None,
    tags: dict[str, str] | None = None,
    on_progress: Callable[[float], None] | None = None,
    timeout: float = 3600.0,
    control: EmbedControl | None = None,
) -> RemuxResult:
    """Write ``video`` again with chapters, tags and attachments inside it.

    Streams are copied, so this is I/O-bound rather than CPU-bound and loses no
    quality. ``on_progress`` receives seconds of media processed so far.

    ``attachments`` are embedded as Matroska attachments, which is what makes
    the result genuinely self-contained: the marker list and the bus log travel
    inside the video file rather than beside it. Copy one file to a USB stick
    and nothing is left behind. They can be pulled back out at any time with
    ``extract_attachments``.

    Only a Matroska file gets them (see _HOLDS_ATTACHMENTS). Any other file is
    rewritten with its chapters and tags alone, and the result's ``left_out``
    names what stayed out, rather than the rewrite failing and losing the
    chapters too. The result's ``attached`` names what the file really holds;
    it is the only thing a caller may delete a sidecar on.

    ``control`` lets another thread stop the rewrite part-way; see
    EmbedControl. Stopped or not, no way out of here leaves ffmpeg running or
    the temporary copy on disk, bar a delete that fails and is logged.
    """
    exe = ffmpeg_path()
    if exe is None:
        return RemuxResult(False, video, "ffmpeg not found")
    if not video.is_file():
        return RemuxResult(False, video, f"{video.name} does not exist")
    if not chapters.is_file():
        return RemuxResult(False, video, f"{chapters.name} does not exist")

    given = [p for p in (attachments or []) if p.is_file()]
    if video.suffix.lower() in _HOLDS_ATTACHMENTS:
        attachments, left_out = given, []
    else:
        attachments, left_out = [], given

    temporary = video.with_name(f"{video.stem}.chapters-tmp{video.suffix}")
    if control is not None:
        control.video = video
        control.temporary = temporary
        if control.cancelled:
            log.warning(
                "Not embedding chapters into %s: %s", video.name, control.reason
            )
            return RemuxResult(False, video, control.reason, cancelled=True)
    command = [
        str(exe), "-hide_banner", "-loglevel", "warning", "-stats",
        "-i", str(video),
        "-i", str(chapters),
        # Metadata comes from input 1 (the chapters file); everything else is
        # mapped from input 0 and copied untouched.
        "-map_metadata", "1",
        "-map", "0",
        "-c", "copy",
    ]

    for name, value in (tags or {}).items():
        if value:
            command += ["-metadata", f"{name}={value}"]

    for index, attachment in enumerate(attachments):
        mimetype = _MIMETYPES.get(attachment.suffix.lower(), "application/octet-stream")
        command += [
            "-attach", str(attachment),
            # ":t:<n>" addresses the n-th attachment stream specifically.
            # Without the mimetype Matroska stores an empty one and some tools
            # then refuse to extract it.
            f"-metadata:s:t:{index}", f"mimetype={mimetype}",
            f"-metadata:s:t:{index}", f"filename={attachment.name}",
        ]

    command += ["-y", str(temporary)]

    log.info("Embedding chapters into %s", video.name)
    started = time.perf_counter()
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        return RemuxResult(False, video, f"Could not run ffmpeg: {exc}")
    if control is not None and not control._attach(process):
        # A cancel landed between the check above and the launch.
        _kill(process)

    tail: list[str] = []
    exited = False
    try:
        assert process.stderr is not None
        for raw in iter(process.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            tail.append(line)
            del tail[:-20]
            if on_progress is not None:
                match = _PROGRESS.search(line)
                if match:
                    hours, minutes, seconds, fraction = match.groups()
                    on_progress(
                        int(hours) * 3600 + int(minutes) * 60 + int(seconds)
                        + float(f"0.{fraction}")
                    )

        try:
            process.wait(timeout=timeout)
            exited = True
        except subprocess.TimeoutExpired:
            pass
    finally:
        if control is not None:
            control._detach()
        if not exited:
            # Timed out, or something raised out of the loop above. Either way
            # nothing will read this ffmpeg again, so it is stopped here rather
            # than left writing a copy that nobody will ever swap in or delete.
            _kill(process)
            _discard(temporary)
    if not exited:
        return RemuxResult(False, video, "Embedding chapters timed out")

    elapsed = time.perf_counter() - started

    if process.returncode != 0 or not temporary.is_file():
        _discard(temporary)
        if control is not None and control.cancelled:
            log.warning(
                "Stopped embedding chapters into %s after %.1fs: %s. The "
                "original is untouched.", video.name, elapsed, control.reason,
            )
            return RemuxResult(False, video, control.reason, elapsed, cancelled=True)
        detail = tail[-1][:200] if tail else f"ffmpeg exited {process.returncode}"
        log.error("Chapter embedding failed: %s", detail)
        return RemuxResult(False, video, detail, elapsed)

    # Refuse to swap in a file that is obviously wrong. Losing a four-hour tech
    # to a botched remux would be unforgivable, and the check is nearly free.
    original_size = video.stat().st_size
    new_size = temporary.stat().st_size
    if new_size < original_size * 0.95:
        _discard(temporary)
        detail = (
            f"the rewritten file is {new_size / 1e6:.0f} MB against the "
            f"original's {original_size / 1e6:.0f} MB, so it was discarded"
        )
        log.error("Chapter embedding produced a suspicious file: %s", detail)
        return RemuxResult(False, video, detail, elapsed)

    try:
        temporary.replace(video)
    except OSError as exc:
        _discard(temporary)
        return RemuxResult(False, video, f"Could not replace the original: {exc}", elapsed)

    log.info(
        "Chapters%s embedded into %s in %.1fs",
        f" and {len(attachments)} attachment(s)" if attachments else "",
        video.name, elapsed,
    )
    # What stayed out is the caller's to report: it decides what is deleted,
    # so only it can say what stays beside the video.
    return RemuxResult(
        True, video, "", elapsed, attached=attachments, left_out=left_out
    )


def list_attachments(video: Path) -> list[str]:
    """Filenames of the attachments inside a media file."""
    text = _describe(video)
    if text is None:
        return []
    return re.findall(r"^\s*filename\s*:\s*(.+)$", text, flags=re.MULTILINE)


def extract_attachments(video: Path, destination: Path) -> list[Path]:
    """Pull every attachment back out of a file.

    The counterpart to embedding: it is what makes putting the sidecars inside
    the video safe rather than a one-way trip.
    """
    exe = ffmpeg_path()
    if exe is None or not video.is_file():
        return []
    names = list_attachments(video)
    if not names:
        return []

    destination.mkdir(parents=True, exist_ok=True)
    command = [str(exe), "-hide_banner", "-loglevel", "error", "-y"]
    for index, name in enumerate(names):
        command += ["-dump_attachment:t:" + str(index), str(destination / name)]
    command += ["-i", str(video), "-f", "null", "-"]

    try:
        subprocess.run(
            command, capture_output=True, encoding="utf-8", errors="replace", timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        log.exception("Could not extract attachments from %s", video.name)
        return []
    return [destination / name for name in names if (destination / name).is_file()]


def _describe(video: Path) -> str | None:
    exe = ffmpeg_path()
    if exe is None or not video.is_file():
        return None
    try:
        result = subprocess.run(
            [str(exe), "-hide_banner", "-i", str(video), "-f", "null", "-"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        log.exception("Could not inspect %s", video)
        return None
    text = result.stderr
    marker = text.find("Output #")
    return text[:marker] if marker != -1 else text


def count_chapters(video: Path) -> int:
    """How many chapters a media file actually contains.

    Used to verify an embed worked rather than assuming it did.
    """
    exe = ffmpeg_path()
    if exe is None or not video.is_file():
        return 0
    try:
        result = subprocess.run(
            [str(exe), "-hide_banner", "-i", str(video), "-f", "null", "-"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        log.exception("Could not inspect %s", video)
        return 0
    # ffmpeg lists chapters twice -- once for the input, once for the output
    # it is about to write -- so counting every "Chapter #" line reports double.
    # Only the input's block describes the file on disk.
    text = result.stderr
    output_marker = text.find("Output #")
    if output_marker != -1:
        text = text[:output_marker]
    return len(re.findall(r"^\s*Chapter #", text, flags=re.MULTILINE))
