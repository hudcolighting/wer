"""Shared test fixtures.

Two matter. The first keeps a test run out of the real settings: several tests
build a whole MainWindow, and a MainWindow autosaves. Without this, running the
suite silently rewrote the settings of whoever ran it -- which happened, and
turned three of their recording options off. Every test now gets a throwaway
data directory.

The second is the Qt application. There can only be one per process and
pytest runs every test file in the same process, so it has to be created once,
here, rather than per module.

It must be a **QApplication**, not a QGuiApplication. QGuiApplication is enough
for QImage and QPainter, which is all the overlay widgets need, but constructing
a real QWidget under one aborts the interpreter -- no exception, no traceback,
just a dead process partway through the run. That cost a confusing half hour, so:
one QApplication, session-scoped, for everything.
"""

from __future__ import annotations

import os

import pytest

from wer.paths import DATA_DIR_ENV


@pytest.fixture(scope="session", autouse=True)
def isolated_data_dir(tmp_path_factory):
    """Point settings and autosave at a throwaway directory for the whole run.

    Session-scoped and autouse: it has to be in place before the first import
    that reads a setting, and no test should have to remember to ask for it.
    """
    directory = tmp_path_factory.mktemp("wer-data")
    previous = os.environ.get(DATA_DIR_ENV)
    os.environ[DATA_DIR_ENV] = str(directory)
    yield directory
    if previous is None:
        os.environ.pop(DATA_DIR_ENV, None)
    else:
        os.environ[DATA_DIR_ENV] = previous


@pytest.fixture(scope="session")
def qt_app():
    """The single QApplication for the whole test session."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        # Offscreen would be tidier, but several tests grab real widget
        # geometry and the offscreen platform reports it differently.
        app = QApplication([])
    yield app
    # Deliberately not calling quit(): later tests in the session still need it,
    # and Python's own teardown handles the process exit.


@pytest.fixture(autouse=True)
def windows_stay_off_real_hardware(request, monkeypatch):
    """Keep windows built inside a test off the machine's real capture hardware.

    A MainWindow built in a test starts what the app starts: the preview
    enumerates cameras and autostarts one, and the Recording panel test-encodes
    on every GPU and probes every audio input. Nothing stops any of it when a
    test simply lets the window go. Measured on the development laptop with a
    Blackmagic UltraStudio Recorder 3G attached: by the time
    test_recording_lifecycle ran, eleven real OpenCV capture threads leaked
    from the ten MainWindows in test_console_profiles and the one in
    test_osc_control were still reading devices, and the process died with
    "Fatal Python error: Aborted" inside a test that used no camera at all.

    The leak was older than the crash. What tipped it over was the Recording
    panel starting to probe audio formats -- most likely the panels left
    behind by test_record_panel firing their deferred probes after their own
    stubs had been undone, so real ffmpeg probes joined that crowd. Run as
    test_console_profiles, test_osc_control, test_record_panel and
    test_recording_lifecycle in suite order on the same machine, the abort
    reproduced 2 of 2 on the code that added the probe and 0 of 2 on the
    commit before it.

    Scope, deliberately narrow:
      * only tests that use Qt (anything depending on qt_app) -- the pure tests
        never import a UI module, and this must not drag Qt into them;
      * only the names the UI modules imported. Tests that are ABOUT devices
        call wer.video.devices / wer.video.encoder directly and see the real
        functions.
    A test wanting a synthetic camera installs its own over these (see
    fake_camera in test_recording_lifecycle); monkeypatch applies it later, so
    it wins, and undoes everything in reverse order afterwards.
    """
    if "qt_app" not in request.fixturenames:
        yield
        return

    from wer.ui import preview, record_panel
    from wer.video.encoder import SOFTWARE_ENCODER, EncoderAvailability

    monkeypatch.setattr(preview, "enumerate_video_devices", lambda: [])
    monkeypatch.setattr(
        record_panel, "detect_encoders",
        lambda **_kwargs: [EncoderAvailability(SOFTWARE_ENCODER, True)],
    )
    monkeypatch.setattr(record_panel, "enumerate_audio_devices", lambda: [])
    monkeypatch.setattr(record_panel, "probe_audio_formats", lambda _device: [])
    # Asked on a first launch, and every test run is one: the data directory
    # above starts empty, so a MainWindow finds no autosave.
    monkeypatch.setattr(record_panel, "default_audio_input_name", lambda: None)
    yield
