"""Recorder.is_writing: what Wer's sACN status follows.

Narrower than is_recording on purpose: a take that is starting, stalled,
waiting to restart or draining for a full disk is not putting picture into the
file, and a status light that pulses through that is lying.

No Qt, no ffmpeg: the recorder is never started.
"""

from __future__ import annotations

import time

import pytest

from wer.video.recorder import STALL_TIMEOUT, Recorder, RecorderState


def recorder(state: RecorderState, *, frame_ago: float | None = 0.1,
             disk_stop: bool = False) -> Recorder:
    made = Recorder()
    made.state = state
    made.stats.last_frame_at = 0.0 if frame_ago is None else time.perf_counter() - frame_ago
    made._disk_stop_requested = disk_stop
    return made


def test_recording_with_a_frame_just_written_is_writing() -> None:
    assert recorder(RecorderState.RECORDING).is_writing


def test_starting_is_not_writing_yet() -> None:
    assert not recorder(RecorderState.STARTING).is_writing


def test_recording_with_no_frame_for_longer_than_a_stall_is_not_writing() -> None:
    assert not recorder(RecorderState.RECORDING, frame_ago=STALL_TIMEOUT + 0.5).is_writing


def test_recording_before_the_first_frame_is_not_writing() -> None:
    assert not recorder(RecorderState.RECORDING, frame_ago=None).is_writing


def test_draining_for_a_full_disk_is_not_writing() -> None:
    assert not recorder(RecorderState.RECORDING, disk_stop=True).is_writing


@pytest.mark.parametrize(
    "state", [s for s in RecorderState if s is not RecorderState.RECORDING]
)
def test_every_other_state_is_not_writing(state: RecorderState) -> None:
    assert not recorder(state).is_writing
