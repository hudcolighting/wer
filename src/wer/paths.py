"""Filesystem locations, resolved for both source checkouts and frozen builds.

This module deliberately has no Qt and no third-party imports: it is the first
thing every other module needs, including the logger, and it must work before
anything else is initialised.

Three runtime shapes have to be handled, and they differ in ways that break
naive code:

    source checkout      <repo>/src/wer/paths.py, resources at <repo>/
    PyInstaller onedir   Wer.exe beside _internal/, resources in _internal/
    PyInstaller onefile   Wer.exe anywhere, resources unpacked to a temp dir
                          that is deleted on exit

`resource_dir()` answers "where are the files I shipped" and `app_dir()`
answers "where is the exe the user double-clicked". For onefile these are very
different places, and writing logs to the first one means writing into a temp
directory that vanishes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "is_frozen",
    "resource_dir",
    "app_dir",
    "repo_root",
    "build_id",
    "ffmpeg_path",
    "icon_path",
    "log_dir",
    "user_data_dir",
    "default_recording_dir",
]


def is_frozen() -> bool:
    """True when running from a PyInstaller build rather than a checkout."""
    return getattr(sys, "frozen", False)


def resource_dir() -> Path:
    """Directory containing bundled read-only resources (ffmpeg, icons).

    For onefile builds this is the temporary extraction directory, which is
    removed when the process exits. Never write here.
    """
    if is_frozen():
        # _MEIPASS is set for both onefile and onedir; for onedir it points at
        # the _internal folder.
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return repo_root()


def app_dir() -> Path:
    """Directory the user perceives the app as living in.

    Frozen: the folder holding Wer.exe. Source: the repo root. This is the
    anchor for "log next to the exe".
    """
    if is_frozen():
        return Path(sys.executable).parent
    return repo_root()


def repo_root() -> Path:
    """Repository root, for source checkouts. src/wer/paths.py -> up three."""
    return Path(__file__).resolve().parent.parent.parent


def build_id() -> str | None:
    """The commit a frozen build was made from, or None in a checkout.

    `build.ps1` runs `git describe --always --dirty` and stamps the answer
    into the payload as `build-id.txt`, because an exe has no repository to
    ask. The version number says what was intended; this says what was
    actually compiled, and whether the tree was dirty when it was. A source
    checkout has no stamp and needs none - git is right there, and a file
    written by some earlier build would be worse than nothing.
    """
    stamp = resource_dir() / "build-id.txt"
    try:
        # utf-8-sig, not utf-8: PowerShell 5.1 writes a BOM for -Encoding utf8,
        # and a leading ﻿ in the Environment tab is the sort of thing that
        # looks like a corrupted build to whoever is reading it.
        text = stamp.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        # Missing is the ordinary case (any source checkout). Unreadable is
        # not, but neither is worth an exception in a readout.
        return None
    return text.strip() or None


def ffmpeg_path() -> Path | None:
    """Locate the bundled ffmpeg.exe, or None if it is missing.

    Returning None rather than raising is deliberate: a missing ffmpeg must
    surface as a visible, explained state in the UI, not as a crash on startup
    (no silent failures). Recording is the only feature that needs it.
    """
    candidates = [
        resource_dir() / "vendor" / "ffmpeg" / "ffmpeg.exe",
        # onedir builds place add-binary payloads at the root of _internal.
        resource_dir() / "ffmpeg.exe",
        app_dir() / "ffmpeg.exe",
        app_dir() / "vendor" / "ffmpeg" / "ffmpeg.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def icon_path() -> Path | None:
    """Locate the application icon, or None if it was not shipped.

    None rather than an exception: an app that refuses to open because its
    icon is missing would be a poor trade. Qt simply falls back to its own.
    """
    candidates = [
        resource_dir() / "assets" / "wer.ico",
        resource_dir() / "wer.ico",
        app_dir() / "assets" / "wer.ico",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _is_writable(directory: Path) -> bool:
    """Probe a directory by actually creating a file in it.

    `os.access` lies on Windows often enough that it is not worth consulting -
    it ignores ACLs and virtualisation.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".wer-write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def log_dir() -> Path:
    """Where the rotating log file goes.

    The rule is "next to the exe", which is right for a portable app on a USB
    stick. But if the exe was dropped somewhere unwritable - Program Files, a
    read-only share - that would mean no log at all, exactly when a log matters
    most. So: next to the exe when possible, %LOCALAPPDATA% otherwise.
    """
    preferred = app_dir() / "logs"
    if _is_writable(preferred):
        return preferred
    fallback = user_data_dir() / "logs"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


#: Overrides where settings live. Set it to keep a whole installation on a
#: thumb drive, and set it in tests so a test run cannot overwrite the settings
#: of whoever is running it -- which it has done, once, for real.
DATA_DIR_ENV = "WER_DATA_DIR"


def user_data_dir() -> Path:
    """Per-user application data, for settings and recent-file lists."""
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "Wer"


def default_recording_dir() -> Path:
    r"""Default output folder for recordings.

    Videos\Wer, because a four-hour tech recording should not land next to the
    exe on whatever thumb drive it was launched from.
    """
    return Path.home() / "Videos" / "Wer"
