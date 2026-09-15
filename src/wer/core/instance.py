"""One writer for the settings file.

Wer autosaves continuously while it runs, and the whole file is rewritten each
time. Two copies open at once therefore do not merge -- whichever writes last
silently discards everything the other did. That is not hypothetical: a
layout built during a session was lost exactly this way, because a second
process rewrote the file from its own older in-memory state.

The fix is deliberately not a lock that refuses to start. A recorder that will
not open, in a booth, ten minutes before a tech, is worse than one with a
confusing settings file. So a second instance runs normally and simply does
not write settings: it is a passenger. The first instance keeps the pen.

The claim is scoped to the data directory, so instances pointed at different
WER_DATA_DIR values never contend -- which is what test harnesses and portable
installs want.

No Qt here: this has to work before the UI exists.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from wer.paths import user_data_dir

log = logging.getLogger(__name__)

__all__ = ["SettingsClaim", "claim_settings"]

LOCK_NAME = "settings.lock"


def _process_alive(pid: int) -> bool:
    """Is a process with this id running?

    Windows only in practice, but written so it degrades to "assume alive"
    rather than to a crash anywhere else. Assuming alive is the safe error:
    it costs a second instance its ability to save, which is recoverable,
    where assuming dead costs the first instance its work, which is not.
    """
    if pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            still_active = wintypes.DWORD()
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
            ]
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(still_active)):
                return True
            STILL_ACTIVE = 259
            return still_active.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - never let this stop the app opening
        log.debug("Could not check whether pid %d is alive", pid, exc_info=True)
        return True


@dataclass
class SettingsClaim:
    """Whether this process may write the settings file."""

    path: Path
    #: False when another live instance already holds the claim.
    granted: bool
    #: The pid holding it, when it is not us.
    held_by: int = 0

    @property
    def may_save(self) -> bool:
        return self.granted

    def release(self) -> None:
        """Give the claim up. Safe to call when it was never granted."""
        if not self.granted:
            return
        try:
            if self.path.is_file() and self._pid_in_file() == os.getpid():
                self.path.unlink()
        except OSError:
            log.debug("Could not remove %s", self.path, exc_info=True)

    def _pid_in_file(self) -> int:
        try:
            return int(self.path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return 0


def claim_settings(directory: Path | None = None) -> SettingsClaim:
    """Try to become the process that writes settings in this data directory.

    A stale claim -- one naming a process that is no longer running, which is
    what a crash or a force-kill leaves behind -- is taken over silently. There
    is nothing to protect at that point, and demanding the user delete a file
    is not a thing to ask of someone during a tech.
    """
    folder = directory or user_data_dir()
    path = folder / LOCK_NAME
    mine = os.getpid()

    try:
        folder.mkdir(parents=True, exist_ok=True)
        existing = 0
        if path.is_file():
            try:
                existing = int(path.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                existing = 0
        if existing and existing != mine and _process_alive(existing):
            log.warning(
                "Another copy of Wer (pid %d) is already using these settings, "
                "so this one will not save any changes to them. Recording and "
                "everything else works normally. Close the other copy and "
                "restart this one if you want to change settings here.",
                existing,
            )
            return SettingsClaim(path, granted=False, held_by=existing)
        path.write_text(str(mine), encoding="utf-8")
        return SettingsClaim(path, granted=True)
    except OSError:
        # An unwritable data directory is already reported elsewhere. Claiming
        # optimistically keeps single-instance behaviour working.
        log.debug("Could not claim %s", path, exc_info=True)
        return SettingsClaim(path, granted=True)
