"""What encoder detection found is remembered as one value, never as two halves.

"auto" is resolved on whichever thread builds an ffmpeg command, and the
recorder builds one for every part it restarts. The encoders detection found,
and the chip Media Foundation reached, used to be two module globals assigned
one after the other, so a reader landing between the two could pair one
detection's encoders with another's vendor.

Nothing here touches a GPU or runs ffmpeg: the detection results are made up.
tests/test_encoder.py covers what "auto" resolves to; it test-encodes on real
hardware, so these stay apart from it.
"""

from __future__ import annotations

import dataclasses

import pytest

from wer.video import encoder as encoder_module
from wer.video.encoder import (
    HARDWARE_ENCODERS,
    SOFTWARE_ENCODER,
    EncoderAvailability,
    forget_detected_encoders,
    remember_detected_encoders,
)

MEDIA_FOUNDATION = next(encoder for encoder in HARDWARE_ENCODERS if encoder.name == "h264_mf")


@pytest.fixture(autouse=True)
def nothing_remembered():
    forget_detected_encoders()
    yield
    forget_detected_encoders()


def test_a_detection_is_remembered_as_one_frozen_value_with_its_encoders_and_vendor() -> None:
    """Both halves live in one object that cannot be changed in place, so the
    only way to change what "auto" means is to replace that object whole."""
    remember_detected_encoders([
        EncoderAvailability(SOFTWARE_ENCODER, True),
        EncoderAvailability(MEDIA_FOUNDATION, True, "", "NVIDIA"),
    ])
    detected = encoder_module._DETECTED
    assert detected.encoders == (SOFTWARE_ENCODER, MEDIA_FOUNDATION)
    assert detected.mf_vendor == "NVIDIA"
    with pytest.raises(dataclasses.FrozenInstanceError):
        detected.mf_vendor = "Intel"
    for old_half in ("_AVAILABLE_CACHE", "_MF_VENDOR"):
        assert not hasattr(encoder_module, old_half), f"{old_half} is back as a global of its own"


def test_a_second_detection_replaces_the_first_whole_and_forgetting_clears_both_halves() -> None:
    """A reader still holding the first detection keeps a pair that belongs
    together, whatever has been remembered since."""
    remember_detected_encoders([EncoderAvailability(MEDIA_FOUNDATION, True, "", "NVIDIA")])
    first = encoder_module._DETECTED
    remember_detected_encoders([EncoderAvailability(SOFTWARE_ENCODER, True)])
    second = encoder_module._DETECTED

    assert second is not first
    assert (first.encoders, first.mf_vendor) == ((MEDIA_FOUNDATION,), "NVIDIA")
    assert (second.encoders, second.mf_vendor) == ((SOFTWARE_ENCODER,), "")

    forget_detected_encoders()
    assert encoder_module._DETECTED is None
