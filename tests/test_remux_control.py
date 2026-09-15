"""Stopping a chapter rewrite from outside it.

No Qt, and no ffmpeg. The process embed_chapters launches is swapped for a
stand-in that looks like a long rewrite from the outside: it opens the
temporary copy, writes into it, reports progress on stderr the way -stats
does, and then keeps going. A real rewrite of a clip short enough for a test
is over in milliseconds, before anything could stop it -- and what is under
test here is the kill, the wait and the delete, which are the same whatever
the process is.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from wer.video import remux

#: Writes a megabyte into the temporary copy (the last argument ffmpeg is
#: given), reports it on stderr, then carries on for a minute.
STAND_IN = """\
import sys, time
with open(sys.argv[-1], "wb") as copy:
    copy.write(b"\\0" * 1_000_000)
    copy.flush()
    sys.stderr.write("frame=  100 fps=0.0 size=1024KiB time=00:00:03.33\\n")
    sys.stderr.flush()
    time.sleep(60)
"""


@pytest.fixture
def take(tmp_path: Path) -> tuple[Path, Path]:
    video = tmp_path / "take012.mkv"
    video.write_bytes(b"the recording itself " * 5000)
    chapters = tmp_path / "take012.chapters.txt"
    chapters.write_text(";FFMETADATA1\n", encoding="utf-8")
    return video, chapters


@pytest.fixture
def stand_in(tmp_path: Path, monkeypatch) -> list[subprocess.Popen]:
    """Swap ffmpeg for the stand-in, and keep every process it launches.

    Only in subprocess as remux sees it, and only for a rewrite into this
    test's own folder. Replacing subprocess.Popen itself replaced it for the
    whole process, and a run of the suite reaches these tests with finishing
    threads and probes left behind by the window tests still going: anything
    one of them launched meanwhile would have run the stand-in instead, and
    been counted here as the rewrite under test.
    """
    script = tmp_path / "stand_in_ffmpeg.py"
    script.write_text(STAND_IN, encoding="utf-8")
    launched: list[subprocess.Popen] = []

    def launch(command, **kwargs):
        if Path(command[-1]).parent != tmp_path:
            return subprocess.Popen(command, **kwargs)
        process = subprocess.Popen([sys.executable, str(script), command[-1]], **kwargs)
        launched.append(process)
        return process

    class SubprocessAsRemuxSeesIt:
        def __getattr__(self, name: str):
            return getattr(subprocess, name)

    view = SubprocessAsRemuxSeesIt()
    view.Popen = launch
    monkeypatch.setattr(remux, "ffmpeg_path", lambda: Path(sys.executable))
    monkeypatch.setattr(remux, "subprocess", view)
    yield launched
    for process in launched:
        if process.poll() is None:
            process.kill()
            process.wait(10.0)


def _copy_of(video: Path) -> Path:
    return video.with_name(f"{video.stem}.chapters-tmp{video.suffix}")


def test_a_stopped_rewrite_leaves_no_ffmpeg_and_no_copy_behind(take, stand_in) -> None:
    """Closing Wer mid-rewrite used to leave both.

    Shutdown gave up on the finishing thread once its wait ran out, and exited.
    The ffmpeg it had launched was a plain Popen in a local variable nothing
    else could reach, so it carried on with nobody reading it; and the
    full-size .chapters-tmp copy stayed beside the video for good, because only
    embed_chapters' own failure branches ever delete it and a thread that dies
    with the interpreter reaches none of them. Stopping a rewrite has to kill
    the process, delete the copy and leave the recording exactly as it was.
    """
    video, chapters = take
    original = video.read_bytes()
    temporary = _copy_of(video)
    control = remux.EmbedControl()
    outcome: dict = {}
    worker = threading.Thread(
        target=lambda: outcome.update(
            result=remux.embed_chapters(video, chapters, control=control)
        ),
        daemon=True,
    )
    worker.start()

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and control.temporary_bytes() < 1_000_000:
        time.sleep(0.02)
    assert control.temporary_bytes() >= 1_000_000, (
        "precondition: the stand-in never started writing its copy"
    )
    assert control.running

    control.cancel("Wer was closed before the rewrite finished")
    worker.join(15.0)

    assert not worker.is_alive(), "embed_chapters never came back after being stopped"
    result = outcome["result"]
    assert not result.ok and result.cancelled
    assert result.detail == "Wer was closed before the rewrite finished"
    assert stand_in[0].wait(5.0) is not None, "the ffmpeg was left running"
    assert not temporary.exists(), "the temporary copy was left beside the video"
    assert video.read_bytes() == original, "the recording was touched"
    assert not control.running


def test_a_stop_outlasts_the_rewrite_it_landed_on(take, stand_in) -> None:
    """A take in two parts is embedded one part at a time, with one control.

    A stop that landed on the first part's rewrite must not let the second
    launch a fresh ffmpeg a moment later -- into a process about to exit, which
    is the orphan this exists to prevent.
    """
    video, chapters = take
    original = video.read_bytes()
    control = remux.EmbedControl()
    control.cancel("Wer was closed before the rewrite finished")

    result = remux.embed_chapters(video, chapters, control=control)

    assert not result.ok and result.cancelled
    assert stand_in == [], "a rewrite was launched after being told to stop"
    assert video.read_bytes() == original
    assert not _copy_of(video).exists()


def test_a_rewrite_whose_reader_fails_still_stops_its_ffmpeg(take, stand_in) -> None:
    """Whatever ends the read loop early, ffmpeg must not outlive it.

    An exception out of the stderr loop left the process running and its copy
    on disk, exactly as an abandoned rewrite did: nothing else held the Popen,
    so nothing else could stop it.
    """
    video, chapters = take

    def broken_progress(seconds: float) -> None:
        raise RuntimeError("the progress handler fell over")

    with pytest.raises(RuntimeError):
        remux.embed_chapters(video, chapters, on_progress=broken_progress)

    assert stand_in, "precondition: the stand-in never ran"
    try:
        stand_in[0].wait(10.0)
    except subprocess.TimeoutExpired:
        pytest.fail("the ffmpeg was left running after the reader failed")
    assert not _copy_of(video).exists(), "the temporary copy was left behind"


def test_a_stop_that_lands_as_ffmpeg_starts_still_stops_it(
    take, stand_in, monkeypatch
) -> None:
    """embed_chapters looks for a stop before it starts ffmpeg, and the control
    only holds the process once it has started.

    A stop landing in between has no process to kill, and a control only stops
    once: the next stop is ignored. Unless the launch looks again and kills
    what it has just started, that ffmpeg runs the whole rewrite with nothing
    left able to stop it, while shutdown, believing it stopped, gives it a few
    seconds and exits over it -- the orphan and the leftover copy EmbedControl
    exists to prevent.
    """
    video, chapters = take
    original = video.read_bytes()
    control = remux.EmbedControl()
    launch = remux.subprocess.Popen

    def launch_then_stop(command, **kwargs):
        process = launch(command, **kwargs)
        control.cancel("Wer was closed before the rewrite finished")
        return process

    monkeypatch.setattr(remux.subprocess, "Popen", launch_then_stop)
    outcome: dict = {}
    worker = threading.Thread(
        target=lambda: outcome.update(
            result=remux.embed_chapters(video, chapters, control=control)
        ),
        daemon=True,
    )
    worker.start()
    worker.join(10.0)

    assert stand_in, "precondition: the stand-in never ran"
    assert not worker.is_alive(), (
        "the rewrite ran on after a stop that landed as its ffmpeg started"
    )
    result = outcome["result"]
    assert not result.ok and result.cancelled
    try:
        stand_in[0].wait(5.0)
    except subprocess.TimeoutExpired:
        pytest.fail("the ffmpeg was left running")
    assert not _copy_of(video).exists(), "the temporary copy was left beside the video"
    assert video.read_bytes() == original, "the recording was touched"
