"""The application starts and exits cleanly.

These run the whole app in a subprocess and check the exit code, which is the
only way to catch crashes that happen during interpreter teardown -- after every
assertion in an in-process test has already passed.

That is not theoretical. Moving DirectShow enumeration onto a worker thread to
keep it off the UI thread made the process segfault on exit, reliably, while
every in-process check still looked fine.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

LIFECYCLE = """
import sys, logging
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from wer.logging_setup import setup_logging
setup_logging(logging.ERROR)
app = QApplication(sys.argv)
from wer.ui.main_window import MainWindow
window = MainWindow()
window.show()
QTimer.singleShot({dwell}, window.close)
QTimer.singleShot({dwell} + 200, app.quit)
app.exec()
print("reached end of main")
"""


def run_app(dwell_ms: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", LIFECYCLE.format(dwell=dwell_ms)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )


def test_app_opens_and_exits_cleanly() -> None:
    """Exit code 0, not 139. A segfault after main() still fails the user."""
    result = run_app(2500)
    assert "reached end of main" in result.stdout, result.stderr[-2000:]
    assert result.returncode == 0, (
        f"exited {result.returncode} "
        f"({'SEGFAULT' if result.returncode in (139, -11) else 'error'})\n"
        f"{result.stderr[-2000:]}"
    )


def test_app_exits_cleanly_even_if_closed_immediately() -> None:
    """Closing before the format probe finishes must not crash either."""
    result = run_app(100)
    assert result.returncode == 0, (
        f"exited {result.returncode}\n{result.stderr[-2000:]}"
    )


def test_startup_is_not_blocked_by_device_probing() -> None:
    """Window construction must not wait on ffmpeg.

    Probing a camera costs ~1 s and has a 15 s timeout; doing it inline during
    construction froze startup and would freeze it far longer on a camera that
    failed to answer.
    """
    script = """
import sys, time, logging
from PySide6.QtWidgets import QApplication
from wer.logging_setup import setup_logging
setup_logging(logging.ERROR)
app = QApplication(sys.argv)
from wer.ui.main_window import MainWindow
start = time.perf_counter()
window = MainWindow()
print(f"{(time.perf_counter() - start) * 1000:.0f}")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    elapsed_ms = float(result.stdout.strip().splitlines()[-1])
    assert elapsed_ms < 500, (
        f"MainWindow() took {elapsed_ms:.0f} ms; something slow moved back onto "
        "the main thread"
    )


def test_format_probe_delivers_integer_keys_intact() -> None:
    """Qt's Signal(dict) marshals through QVariantMap, which needs STRING keys.

    Declared as Signal(dict), the integer device indices were silently dropped
    and the receiver got an empty dict -- no error, no warning, just a format
    picker permanently showing unverified guesses. Signal(object) passes the
    Python value through untouched.

    This runs in a subprocess because it needs a QApplication and a real camera.
    """
    script = """
import sys, logging
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from wer.logging_setup import setup_logging
setup_logging(logging.ERROR)
from wer.ui.preview import FormatProbe
from wer.video.devices import enumerate_video_devices

app = QApplication(sys.argv[:1])
devices = enumerate_video_devices()
if not devices:
    print("SKIP no camera")
    raise SystemExit(0)

received = {}
probe = FormatProbe()
def done(formats):
    received.update(formats)
    app.quit()
probe.finished.connect(done)
probe.probe(devices)
QTimer.singleShot(30000, app.quit)
app.exec()

keys = sorted(received)
print("KEYS", keys)
print("TYPES", sorted({type(k).__name__ for k in keys}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    if "SKIP" in result.stdout:
        pytest.skip("no camera present")

    assert "KEYS []" not in result.stdout, (
        "the probe delivered an empty dict; integer keys were dropped in transit"
    )
    assert "TYPES ['int']" in result.stdout, (
        f"device indices did not survive the signal: {result.stdout}"
    )


# ------------------------------------------------------- taskbar identity


def test_the_app_claims_its_own_taskbar_identity() -> None:
    """Without an explicit AppUserModelID the shell hands the taskbar button
    the host process's identity and icon -- while the window's own title bar
    still shows the right one. That half-working split is the symptom."""
    import sys as _sys

    from wer.app import APP_USER_MODEL_ID, _claim_taskbar_identity

    assert "." in APP_USER_MODEL_ID, "an AppUserModelID is dotted, like a CLSID"
    assert len(APP_USER_MODEL_ID) <= 128, "Windows caps this at 128 characters"
    assert " " not in APP_USER_MODEL_ID

    _claim_taskbar_identity()          # must not raise, on any platform

    if _sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        buf = ctypes.c_wchar_p()
        hresult = ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(
            ctypes.byref(buf)
        )
        assert hresult == 0, f"shell rejected the id (hresult {hresult:#x})"
        assert buf.value == APP_USER_MODEL_ID
