"""Application bootstrap.

Ordering matters here. Logging is configured before Qt is imported so that a
failure inside Qt startup - a missing platform plugin in a bad bundle, most
likely - still produces a log file rather than a window that never appears.
"""

from __future__ import annotations

import logging
import sys

from wer import APP_DISPLAY_NAME, APP_NAME, APP_ORG, __version__
from wer.logging_setup import setup_logging
from wer.paths import icon_path


#: What Windows uses to decide which taskbar button a window belongs to, and
#: therefore which icon that button wears.
APP_USER_MODEL_ID = "HudCo.Wer.WindowsEosRecorder"


def _claim_taskbar_identity() -> None:
    """Tell Windows this process is its own application.

    Without this the shell decides for us. It looks at the process, sees a
    generic host -- python.exe from a checkout, or a PyInstaller bootloader --
    and gives the taskbar button that host's identity and icon. The window
    itself still shows the right icon in its title bar, which is exactly the
    half-working symptom that makes this confusing to diagnose.

    Safe to call anywhere but Windows: it simply does nothing.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )
    except Exception:  # noqa: BLE001 - never block startup over a taskbar icon
        logging.getLogger(__name__).warning(
            "Could not set the AppUserModelID; the taskbar may show a generic "
            "icon", exc_info=True,
        )


def main(argv: list[str] | None = None) -> int:
    """Run the application. Returns a process exit code."""
    setup_logging()
    log = logging.getLogger(__name__)

    # Before any window exists: the shell reads this when the first one appears.
    _claim_taskbar_identity()

    # Imported after logging is live, deliberately - see module docstring.
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from wer.ui.main_window import MainWindow

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_ORG)

    # Set on the QApplication, not just the window: this is what Windows uses
    # for the taskbar button, Alt-Tab, and every dialog the app opens. The exe
    # carries the same icon for Explorer, but that one does not apply to a
    # source checkout, and running from source should look like the real thing.
    icon = icon_path()
    if icon is not None:
        app.setWindowIcon(QIcon(str(icon)))
    else:
        log.warning("No application icon found; Qt will use its default")

    window = MainWindow()
    window.show()
    log.info("Main window shown; entering event loop")

    code = app.exec()
    log.info("Event loop exited with code %s", code)
    return code
