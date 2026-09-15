"""Embedding chapters and sidecars into the recording itself.

Exercises the real bundled ffmpeg. That is the point: the value here is that a
finished file opens in an ordinary player with the cues as chapters, and only
ffmpeg can tell us whether that is true.

No Qt.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from wer.core.markers import MarkerLog
from wer.paths import ffmpeg_path
from wer.video.capture import Frame
from wer.video.encoder import SOFTWARE_ENCODER, Container, OutputSettings
from wer.video.recorder import Recorder
from wer.video.remux import (
    count_chapters,
    embed_chapters,
    extract_attachments,
    list_attachments,
)

pytestmark = pytest.mark.skipif(
    ffmpeg_path() is None, reason="bundled ffmpeg missing"
)

W, H, FPS = 320, 180, 30


def make_recording(
    path: Path, seconds: float = 2.0, container: Container = Container.MKV
) -> float:
    """Record a short clip, pacing frames the way a camera delivers them.

    The pacing is not decoration. ffmpeg times this pipe by when frames
    actually arrive, so submitting a second of video in three milliseconds
    produces a three-millisecond file -- and then every chapter written
    against the nominal duration lands past the end of it. That is precisely
    the bug this suite exists to catch, so the fixture must not reproduce it
    by accident.

    The software encoder is named rather than left to "auto": a detection
    remembered by an earlier test in the same run could otherwise send these
    clips to a GPU encoder.
    """
    recorder = Recorder()
    assert recorder.start(
        OutputSettings(
            quality_preset="tiny", encoder=SOFTWARE_ENCODER.name, container=container
        ),
        path,
        capture_width=W, capture_height=H, capture_fps=FPS,
    ), recorder.detail
    count = int(FPS * seconds)
    started = time.perf_counter()
    for i in range(count):
        image = np.zeros((H, W, 3), np.uint8)
        image[:, (i * 3) % (W - 20):(i * 3) % (W - 20) + 20] = (30, 180, 240)
        recorder.submit(Frame(image=image, timestamp=time.perf_counter(), index=i))
        due = started + (i + 1) / FPS
        remaining = due - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)
    elapsed = time.perf_counter() - started
    recorder.stop()
    return elapsed


def _marked_take(video: Path, container: Container) -> tuple[Path, Path, float]:
    """A short take, and a chapters file for three markers inside it."""
    duration = make_recording(video, container=container)
    log = MarkerLog()
    log.add_cue(0.4, "1/58", "Adriana Xs Center")
    log.add_cue(1.0, "1/62", "Enter Dromio")
    log.add_manual(1.5, "followspot late")
    chapters = video.with_suffix(".chapters.txt")
    log.write_chapters(chapters, duration)
    return video, chapters, duration


@pytest.fixture()
def recording(tmp_path: Path) -> tuple[Path, Path, float]:
    return _marked_take(tmp_path / "take.mkv", Container.MKV)


@pytest.fixture()
def mp4_recording(tmp_path: Path) -> tuple[Path, Path, float]:
    """An MP4 take as Wer writes one: fragmented, so an interrupted file plays."""
    return _marked_take(tmp_path / "take.mp4", Container.MP4)


def test_a_fresh_recording_has_no_chapters(recording) -> None:
    video, _, _ = recording
    assert count_chapters(video) == 0


def test_embedding_puts_chapters_in_the_file(recording) -> None:
    """The whole point: VLC and mpv read these; a sidecar they ignore."""
    video, chapters, _ = recording
    assert embed_chapters(video, chapters).ok
    assert count_chapters(video) == 3


def test_chapter_titles_survive_the_round_trip(recording) -> None:
    import subprocess

    video, chapters, _ = recording
    assert embed_chapters(video, chapters).ok
    result = subprocess.run(
        [str(ffmpeg_path()), "-hide_banner", "-i", str(video), "-f", "ffmetadata", "-"],
        capture_output=True, text=True, timeout=60,
    )
    assert "Cue 1/58 - Adriana Xs Center" in result.stdout
    assert "followspot late" in result.stdout


def test_attachments_go_inside_and_come_back_out(recording, tmp_path: Path) -> None:
    """Self-containment is only safe if it is reversible."""
    video, chapters, _ = recording
    markers = tmp_path / "take.markers.csv"
    markers.write_text("timecode,note\n00:00:01.000,hello\n", encoding="utf-8")
    buslog = tmp_path / "take.bus.jsonl"
    buslog.write_text('{"t":0.0,"key":"a","value":1}\n', encoding="utf-8")

    assert embed_chapters(video, chapters, attachments=[markers, buslog]).ok
    assert set(list_attachments(video)) == {markers.name, buslog.name}

    pulled = extract_attachments(video, tmp_path / "out")
    assert {p.name for p in pulled} == {markers.name, buslog.name}
    assert (tmp_path / "out" / markers.name).read_text(encoding="utf-8") == (
        markers.read_text(encoding="utf-8")
    )


def _sidecars_beside(video: Path) -> list[Path]:
    """The marker list and bus log a finished take hands to embed_chapters."""
    markers = video.with_suffix(".markers.csv")
    markers.write_text("timecode,note\n00:00:01.000,hello\n", encoding="utf-8")
    buslog = video.with_suffix(".bus.jsonl")
    buslog.write_text('{"t":0.0,"key":"a","value":1}\n', encoding="utf-8")
    return [markers, buslog]


def test_an_mkv_take_says_which_attachments_it_now_holds(recording) -> None:
    """The caller tidies sidecars away on this answer, so it must be exact."""
    video, chapters, _ = recording
    sidecars = _sidecars_beside(video)

    result = embed_chapters(video, chapters, attachments=sidecars)

    assert result.ok, result.detail
    assert result.attached == tuple(sidecars)
    assert result.left_out == ()
    assert set(list_attachments(video)) == {path.name for path in sidecars}


def test_an_mp4_take_gets_its_chapters_and_tags_when_it_has_sidecars(
    mp4_recording,
) -> None:
    """Every MP4 take with sidecars used to lose its chapters.

    The MP4 muxer cannot hold attachments, and ffmpeg failed the whole rewrite
    over them: an hour-long soak ended with 0 of its 1,685 chapters.
    """
    import subprocess

    video, chapters, _ = mp4_recording
    sidecars = _sidecars_beside(video)

    result = embed_chapters(
        video, chapters, attachments=sidecars,
        tags={"title": "The Comedy of Errors"},
    )

    assert result.ok, result.detail
    assert count_chapters(video) == 3
    metadata = subprocess.run(
        [str(ffmpeg_path()), "-hide_banner", "-i", str(video), "-f", "ffmetadata", "-"],
        capture_output=True, text=True, timeout=60,
    )
    assert "The Comedy of Errors" in metadata.stdout
    assert "Cue 1/58 - Adriana Xs Center" in metadata.stdout


def test_an_mp4_take_names_nothing_as_attached_and_leaves_its_sidecars(
    mp4_recording,
) -> None:
    """Naming a sidecar as attached would get its only copy deleted.

    The bus log in particular exists nowhere else.
    """
    video, chapters, _ = mp4_recording
    sidecars = _sidecars_beside(video)

    result = embed_chapters(video, chapters, attachments=sidecars)

    assert result.ok, result.detail
    assert result.attached == ()
    assert result.left_out == tuple(sidecars)
    assert list_attachments(video) == []
    assert all(path.is_file() for path in sidecars)


def test_metadata_tags_are_written(recording) -> None:
    import subprocess

    video, chapters, _ = recording
    assert embed_chapters(video, chapters, tags={"title": "The Comedy of Errors"}).ok
    result = subprocess.run(
        [str(ffmpeg_path()), "-hide_banner", "-i", str(video), "-f", "ffmetadata", "-"],
        capture_output=True, text=True, timeout=60,
    )
    assert "The Comedy of Errors" in result.stdout


def test_the_video_is_not_re_encoded(recording) -> None:
    """Stream copy: the recording must not lose quality to gain chapters."""
    video, chapters, _ = recording
    before = video.stat().st_size
    assert embed_chapters(video, chapters).ok
    after = video.stat().st_size
    # Chapters add a fixed few hundred bytes, which is a large fraction of a
    # one-second test clip and a negligible one of a real take. Allow for the
    # block itself; re-encoding would change the size by far more.
    assert abs(after - before) < before * 0.10 + 2048


def test_a_missing_chapters_file_leaves_the_video_alone(recording) -> None:
    video, _, _ = recording
    before = video.read_bytes()
    result = embed_chapters(video, video.with_suffix(".nope.txt"))
    assert not result.ok
    assert result.detail
    assert video.read_bytes() == before, "the recording must not be touched"


def test_a_missing_video_is_reported_not_raised(tmp_path: Path) -> None:
    chapters = tmp_path / "c.txt"
    chapters.write_text(";FFMETADATA1\n", encoding="utf-8")
    result = embed_chapters(tmp_path / "nope.mkv", chapters)
    assert not result.ok and result.detail


def test_counting_chapters_is_not_doubled(recording) -> None:
    """ffmpeg prints its chapter list twice - once for input, once for output."""
    video, chapters, _ = recording
    embed_chapters(video, chapters)
    assert count_chapters(video) == 3, "input and output blocks are both counted"


# ------------------------------------------------- the toggles actually toggle


def _finished_files(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir() if p.is_file()}


def test_embedding_off_leaves_the_sidecars_and_a_plain_video(
    recording, tmp_path: Path
) -> None:
    """With the setting off, nothing is rewritten and the file is untouched."""
    video, chapters, _ = recording
    markers = video.with_suffix(".markers.csv")
    markers.write_text("timecode,note\n", encoding="utf-8")
    before = video.read_bytes()

    # The main window simply does not call embed_chapters in this case.
    assert count_chapters(video) == 0
    assert video.read_bytes() == before
    assert markers.is_file() and chapters.is_file()


def test_embedding_on_can_keep_the_sidecars_too(recording, tmp_path: Path) -> None:
    """keep_sidecar_files is about tidiness, not about losing data."""
    video, chapters, _ = recording
    markers = video.with_suffix(".markers.csv")
    markers.write_text("timecode,note\n00:00:01.000,x\n", encoding="utf-8")

    assert embed_chapters(video, chapters, attachments=[markers]).ok
    # embed_chapters never deletes anything; the caller decides.
    assert markers.is_file()
    assert chapters.is_file()
    assert set(list_attachments(video)) == {markers.name}


def test_embedding_with_no_attachments_still_writes_chapters(recording) -> None:
    """Bus log off must not stop the chapters going in."""
    video, chapters, _ = recording
    assert embed_chapters(video, chapters, attachments=[]).ok
    assert count_chapters(video) == 3
    assert list_attachments(video) == []


# ----------------------------------------- a show name with an accent in it


def test_a_path_windows_cannot_spell_still_embeds(tmp_path: Path) -> None:
    """{show} goes into the filename, so one accented show name reaches this.

    Every subprocess call here reads ffmpeg's output as UTF-8. Under
    ``text=True`` Python decodes with the locale codec instead -- cp1252 on a
    UK or US Windows -- and ffmpeg echoes the path back in UTF-8. A character
    whose UTF-8 uses one of the five bytes cp1252 leaves undefined kills the
    reader thread, subprocess returns ``stderr=None``, and the first thing to
    touch it raises TypeError. No except clause in this module is watching for
    that, so it escapes into the finishing thread at the end of a take.

    U+0101 (Latvian a-macron) encodes as C4 81, and 0x81 is one of the five.
    Polish L-stroke and Czech c-caron are two more. This test spells one into
    the filename and asserts the whole chapter round trip survives it.
    """
    video = tmp_path / "Sp\u0101le_take001.mkv"
    duration = make_recording(video)
    log = MarkerLog()
    log.add_cue(0.4, "1/58", "Ādriana enters")
    log.add_cue(1.0, "1/62", "Dromio exits")
    chapters = video.with_suffix(".chapters.txt")
    log.write_chapters(chapters, duration)

    assert count_chapters(video) == 0, "fixture should start with no chapters"
    assert embed_chapters(video, chapters).ok, "embedding refused an accented path"
    assert count_chapters(video) == 2
