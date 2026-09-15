"""Keeping Windows awake while a take is recording.

A laptop left recording a tech sleeps on its power plan's schedule -- on the
development laptop's Balanced plan, after 30 minutes idle on battery -- and a
sleeping machine records nothing: the camera stops delivering, ffmpeg is
suspended mid-file, and the take is whatever survives the wake-up. Nothing in
Wer asked Windows to stay up, so an unattended take on battery was a
30-minute take.

SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) is the documented
way to hold off idle sleep for as long as the caller asks. Two properties of it
shape this module:

* The request belongs to the THREAD that makes it, and lapses when that thread
  exits. It is therefore made and cleared on the Qt main thread, which lives as
  long as the app does -- never from a capture or finishing thread, whose
  request would quietly end when the thread did.
* It holds off IDLE sleep only. Closing the lid or pressing the power button
  still sleeps the machine, as it should.

The display is left to its own timeout on purpose: recording does not need a
lit screen, and a booth laptop dimming during the show is wanted.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

log = logging.getLogger(__name__)

__all__ = ["ES_CONTINUOUS", "ES_SYSTEM_REQUIRED", "KeepAwake"]

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _windows_setter() -> Callable[[int], int] | None:
    """kernel32.SetThreadExecutionState, or None off Windows."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    function = ctypes.WinDLL("kernel32", use_last_error=True).SetThreadExecutionState
    # Declared, not left to ctypes' int default: EXECUTION_STATE is a DWORD,
    # and ES_CONTINUOUS has the top bit set.
    function.argtypes = [wintypes.DWORD]
    function.restype = wintypes.DWORD
    return function


class KeepAwake:
    """Holds off idle sleep between hold() and release(). Main thread only."""

    def __init__(self, setter: Callable[[int], int] | None = None) -> None:
        self._setter = setter if setter is not None else _windows_setter()
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def hold(self) -> bool:
        """Ask Windows not to idle-sleep. Returns whether it agreed."""
        if self._held:
            return True
        if self._setter is None:
            return False
        if not self._setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
            # Zero means refused. The take is still worth recording, so this
            # is said in the log -- which travels with the recording -- rather
            # than stopping anything.
            log.warning(
                "Windows refused to stay awake for this take; it may sleep on "
                "the power plan's schedule"
            )
            return False
        self._held = True
        log.info("Keeping Windows awake while recording")
        return True

    def release(self) -> None:
        """Let Windows sleep on its own schedule again."""
        if not self._held:
            return
        self._held = False
        if self._setter is not None:
            self._setter(ES_CONTINUOUS)
        log.info("Windows may sleep again")
