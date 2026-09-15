"""A/V offsets kept for each camera and audio input, and where each one came from.

No Qt. The offset that lines a take's sound up with its picture belongs to the
devices -- how late this camera's picture and this input's sound reach the
file -- so it is kept for each camera and audio input, by name, and follows the
operator from one rig to the next (Hudson, 12 Sep 2026). A pairing has one of:

- an offset the operator typed on the Recording tab, which sticks;
- an offset a clap test measured and the operator applied, which also sticks,
  and remembers what it measured; or
- nothing set, in which case the automatic value is used.

Names, not indexes: the Blackmagic's DirectShow index moved when a USB camera
was plugged in (10-12 Sep 2026); its name did not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from wer.core.showfile import AvOffset, RecordingConfig

__all__ = [
    "AUTOMATIC",
    "AUTOMATIC_OFFSET_MS",
    "CLAP",
    "NO_SOUND",
    "OFFSET_LIMIT_MS",
    "TYPED",
    "Offset",
    "describe_offset",
    "offset_for",
    "set_offset",
    "sync_legacy_offset",
    "use_automatic",
]

AUTOMATIC = "automatic"
TYPED = "typed"
CLAP = "clap"
NO_SOUND = "no sound"

#: The Recording tab's range, in ms either way.
OFFSET_LIMIT_MS = 2000

#: What a pairing uses until the operator or a clap test sets something.
#:
#: 0 for now, deliberately. With the recording start of 13 Sep 2026 -- audio
#: input opened first, frames written from launch -- the clocks put the sound
#: about 60 ms behind the picture on the development laptop's own camera and
#: microphone. A clock reading is not lip sync, though: the camera's own delay
#: before a frame reaches Wer would cancel some or all of that, and nothing
#: inside the program can see it. This number is to come from clap tests on
#: Hudson's rigs, not from the clocks.
AUTOMATIC_OFFSET_MS = 0


@dataclass(frozen=True)
class Offset:
    """The offset a take would be armed with now, and why."""

    value_ms: int
    #: AUTOMATIC, TYPED, CLAP or NO_SOUND.
    source: str
    entry: AvOffset | None = None


def offset_for(config: RecordingConfig, camera: str) -> Offset:
    """The offset for ``camera`` and the audio input the config names."""
    if not config.audio_device:
        return Offset(0, NO_SOUND)
    entry = _find(config, camera, config.audio_device)
    if entry is None:
        return Offset(AUTOMATIC_OFFSET_MS, AUTOMATIC)
    return Offset(_clamp(entry.offset_ms), CLAP if entry.source == CLAP else TYPED, entry)


def set_offset(
    config: RecordingConfig,
    camera: str,
    value_ms: int,
    *,
    source: str = TYPED,
    measured_lag_ms: float | None = None,
    recorded_offset_ms: int | None = None,
    claps: int | None = None,
    frame_period_ms: float | None = None,
    audio: str | None = None,
    now: datetime | None = None,
) -> AvOffset:
    """Set the offset for ``camera`` and an audio input. It sticks.

    The audio input is the config's own unless ``audio`` names another: a clap
    test applies to the input its take was recorded with, which the operator
    may have changed since.
    """
    audio = audio if audio is not None else (config.audio_device or "")
    entry = _find(config, camera, audio)
    if entry is None:
        entry = AvOffset(camera=camera, audio=audio)
        config.av_offsets.append(entry)
    measured = source == CLAP
    entry.offset_ms = _clamp(value_ms)
    entry.source = CLAP if measured else TYPED
    entry.set_at = (now or datetime.now()).isoformat(timespec="seconds")
    entry.measured_lag_ms = measured_lag_ms if measured else None
    entry.recorded_offset_ms = recorded_offset_ms if measured else None
    entry.claps = claps if measured else None
    entry.frame_period_ms = frame_period_ms if measured else None
    sync_legacy_offset(config, camera)
    return entry


def use_automatic(config: RecordingConfig, camera: str) -> bool:
    """Forget what was set for ``camera`` and the config's audio input.

    True if there was something to forget.
    """
    audio = config.audio_device or ""
    kept = [entry for entry in config.av_offsets if not (entry.camera == camera and entry.audio == audio)]
    changed = len(kept) != len(config.av_offsets)
    config.av_offsets[:] = kept
    sync_legacy_offset(config, camera)
    return changed


def sync_legacy_offset(config: RecordingConfig, camera: str) -> None:
    """Keep ``av_offset_ms`` to what an older Wer should record with.

    That field is all an older Wer reads, and it saves it back. So it holds only
    a value the operator or a clap test set for the pairing in use, and 0 when
    that pairing is on the automatic value: the next upgrade of a file an older
    Wer saved reads a non-zero value as the operator's own, and an automatic
    estimate must never come back as that.
    """
    offset = offset_for(config, camera)
    config.av_offset_ms = offset.value_ms if offset.source in (TYPED, CLAP) else 0


def describe_offset(offset: Offset) -> str:
    """Where the offset came from, in words for the Recording tab."""
    if offset.source == NO_SOUND:
        return "No sound is recorded, so there is nothing to line up."
    if offset.source == AUTOMATIC:
        return (
            f"Automatic ({offset.value_ms} ms): not set yet for this camera and audio "
            "input. A clap test measures it."
        )
    entry = offset.entry
    day = _day(entry.set_at) if entry is not None else ""
    when = f" on {day}" if day else ""
    if offset.source == CLAP:
        from_claps = f", from {entry.claps} claps" if entry is not None and entry.claps else ""
        return f"Measured by a clap test{when}{from_claps}."
    return f"Set by you for this camera and audio input{when}."


def _find(config: RecordingConfig, camera: str, audio: str) -> AvOffset | None:
    for entry in config.av_offsets:
        if entry.camera == camera and entry.audio == audio:
            return entry
    return None


def _clamp(value_ms: int) -> int:
    return max(-OFFSET_LIMIT_MS, min(OFFSET_LIMIT_MS, int(value_ms)))


def _day(set_at: str) -> str:
    try:
        moment = datetime.fromisoformat(set_at)
    except (TypeError, ValueError):
        return ""
    return f"{moment.day} {moment:%b %Y}"
