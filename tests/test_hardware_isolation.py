"""Windows built inside tests must not touch the machine's real capture hardware.

A MainWindow starts what the app starts: the preview enumerates cameras and
autostarts one, and the Recording panel test-encodes on every GPU and probes
every audio input. A test that simply lets its window go stops none of that.
With a Blackmagic UltraStudio Recorder 3G attached to the development laptop,
eleven real capture threads leaked that way -- ten from test_console_profiles,
one from test_osc_control -- and the whole run died with "Fatal Python error:
Aborted", inside a test that used no camera at all. The leak predated the
crash; adding an audio format probe to the Recording panel is what tipped it
over -- 2 of 2 aborts with it and 0 of 2 on the commit before, same machine.

conftest.windows_stay_off_real_hardware is what prevents it. This is the
tripwire for that fixture being removed or narrowed: the failure it guards
against is intermittent, native, and only reproduces on the machines that
have real capture hardware, so it must be caught here instead.
"""

from __future__ import annotations

import threading
import time


def _count(name: str) -> int:
    return sum(1 for thread in threading.enumerate() if thread.name == name)


def test_a_window_built_in_a_test_starts_no_real_hardware(qt_app) -> None:
    from wer.ui.main_window import MainWindow

    capture_before = _count("capture")
    format_probes_before = _count("format-probe")
    audio_probes_before = _count("wer-audio-probe")

    window = MainWindow()
    try:
        # Long enough for the start-up steps that begin at once: encoder
        # detection starts on a worker as the window is built, and the audio
        # check as soon as the (stubbed, empty) camera list is in. Both answer
        # by queued signal, so what they put in the Recording panel is the
        # reliable tripwire below. Camera autostart waits on those and then on
        # a DirectShow open that can take several seconds, so the capture
        # checks only catch a camera that has started by the end of this wait.
        # Measured with the fixture removed, before detection and the audio
        # list moved to workers: the audio check failed and the capture checks
        # passed -- the camera had not opened yet.
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.02)

        assert window.preview.capture is None, (
            "the preview autostarted a real camera inside a test"
        )
        assert _count("capture") == capture_before, (
            "a real CameraCapture thread was started inside a test"
        )
        assert _count("format-probe") == format_probes_before
        assert _count("wer-audio-probe") == audio_probes_before, (
            "a real audio input was probed inside a test"
        )
        # A thread count alone no longer proves it: the real audio check can
        # list and probe two inputs well inside the wait above and be gone.
        # The stub lists none, and any machine running the suite with the stub
        # removed lists at least what ffmpeg finds, so this tells them apart.
        assert window.record_panel._audio_devices == [], (
            "real audio inputs were listed inside a test "
            f"({window.record_panel._audio_devices!r})"
        )
        # Real detection lists every encoder, available or not, on any
        # machine; the stub lists software alone. So this tells a real GPU
        # test-encode from none, whatever hardware runs the suite.
        assert len(window.record_panel._encoders) == 1, (
            "real encoder detection ran inside a test "
            f"({len(window.record_panel._encoders)} encoders listed)"
        )
    finally:
        window.close()
