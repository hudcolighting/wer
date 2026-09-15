"""Rotating file logging, plus a crash handler that cannot be missed.

The rule: no silent failures. Every failure path either shows in the UI or
writes to a log file. The frozen app has no console, so the log file is the
only record of anything that goes wrong before the UI exists.

No Qt here - logging is configured before QApplication is constructed, so that
a failure during Qt startup still lands in the file.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
from pathlib import Path
from types import TracebackType

from wer import APP_DISPLAY_NAME, __version__
from wer.paths import app_dir, ffmpeg_path, is_frozen, log_dir

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 5 MB x 5 files. A long tech generates a lot of bus traffic at debug level;
#: this keeps a session's worth without unbounded growth.
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

_log_file: Path | None = None


def log_file_path() -> Path | None:
    """Path of the active log file, or None if logging is not configured yet."""
    return _log_file


def setup_logging(level: int = logging.INFO) -> Path:
    """Configure root logging to a rotating file (and stderr when available).

    Returns the log file path. Safe to call once at startup; calling twice
    replaces the handlers rather than duplicating them.
    """
    global _log_file

    directory = log_dir()
    _log_file = directory / "wer.log"

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        _log_file,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
        delay=False,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # A windowed PyInstaller build has sys.stderr set to None. Guard, or every
    # log call raises.
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    _install_excepthook()
    _log_banner()
    return _log_file


def _log_banner() -> None:
    """Record the environment. This is the first thing to read in a bug report."""
    log = logging.getLogger(__name__)
    ffmpeg = ffmpeg_path()
    log.info("=" * 72)
    log.info("%s %s starting", APP_DISPLAY_NAME, __version__)
    log.info("Python   %s", sys.version.replace("\n", " "))
    log.info("Platform %s", _platform_text())
    log.info("Frozen   %s", is_frozen())
    log.info("App dir  %s", app_dir())
    log.info("Log file %s", _log_file)
    log.info("ffmpeg   %s", ffmpeg if ffmpeg else "NOT FOUND - recording unavailable")
    log.info("=" * 72)


def _platform_text() -> str:
    """The Windows version and processor architecture, without asking WMI.

    platform.platform() and platform.machine() both go through
    platform.uname(), which on Python 3.12 answers from WMI queries. That cost
    109-186 ms on every launch -- three fresh processes on the development
    laptop -- before the window could appear, to write one line of the log
    banner. sys.getwindowsversion() and the environment carry the same facts
    in about a third of a millisecond.
    """
    if sys.platform != "win32":
        return f"{platform.system()} {platform.release()} {platform.machine()}"
    version = sys.getwindowsversion()
    architecture = (
        os.environ.get("PROCESSOR_ARCHITEW6432")
        or os.environ.get("PROCESSOR_ARCHITECTURE", "")
    )
    # Windows 11 still reports itself as 10.0; the build number tells them apart.
    name = "11" if version.major == 10 and version.build >= 22000 else f"{version.major}.{version.minor}"
    return f"Windows {name} (10.0.{version.build}) {architecture}".strip()


def _install_excepthook() -> None:
    """Route unhandled exceptions to the log instead of a silent death."""

    def handle(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("wer.crash").critical(
            "Unhandled exception", exc_info=(exc_type, exc_value, exc_tb)
        )

    sys.excepthook = handle

    # Exceptions raised inside threads bypass sys.excepthook entirely. With
    # capture, encode and one thread per connection, that is most of the app.
    import threading

    def handle_thread(args: threading.ExceptHookArgs) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        logging.getLogger("wer.crash").critical(
            "Unhandled exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = handle_thread
