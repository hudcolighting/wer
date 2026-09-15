"""Hold the camera's automatic start at launch until the start-up queries answer.

Measured on the rig, in seconds from process creation, with the webcam saved
(the Blackmagic run has the same shape):

    0.34-0.41  interface thread: DirectShow enumeration of the cameras
    0.41-1.09  format-probe thread: ffmpeg -list_options for each camera
    0.50       window shown; the console finder's probe answers at 0.557
    0.50-1.87  interface thread: encoder detection (ffmpeg -encoders, then a
               test encode on each GPU)
    1.87-2.17  interface thread: ffmpeg -list_devices for the audio inputs
    2.17-2.64  wer-audio-probe: -list_options on each audio input
    2.19-5.42  interface thread: opening the camera (2.4 s) and measuring the
               rate it delivers (0.83 s)
    5.42       the console reports Live; its connection could not even start
               until the interface thread came free
    5.46       first camera frame

Encoder detection and the audio list now run on worker threads, so the
interface thread is free from the moment the window is shown and the console's
result is handled as soon as it arrives. That leaves the question of when the
camera may open. Opening is still done on the interface thread
(PreviewPanel.start_capture), and two kinds of work must not run alongside it:

- A device query. In the run above the audio probe was asking each input for
  its formats during the open, and on the Blackmagic rig one of those inputs is
  the UltraStudio whose video was being opened.
- A test encode. open() times 25 reads on DirectShow and keeps whichever
  backend delivers more. A DirectShow rate read low because a GPU test encode
  had the machine busy sends it on to try Media Foundation as well, with
  DirectShow still holding the device -- and Media Foundation is the backend
  measured at half the rate on this laptop's camera in poor light (capture.py).

So the automatic start waits for the two device queries: the camera format
probe and the audio input check. The main window starts the audio check only
once the format probe is over, so the Blackmagic's audio and video are never
probed at the same moment. A camera the operator picks or restarts later is not
held.

Encoder detection is kept out of the open a different way: it is not started
until the open is over (MainWindow._detect_encoders_after_the_camera). It used
to be waited for as well, and on the first machine measured that was not the
development laptop -- a Surface Pro 8, freshly installed, 12 Sep 2026 -- it
was the last to answer, holding the camera until 8.59 s:

    0.00-4.59  the camera format probe
    1.11-8.59  encoder detection, its last test encode across the whole of
    4.59-8.43  the audio input check

Nothing about opening a camera needs detection's answer; only a take on
Automatic does. Waiting for it saved nothing and cost the picture.

A query that never answers must not keep the stage off the screen, so the wait
is bounded. Running out is logged at WARNING and pinned on the status bar,
naming what had not answered and what the camera is starting without. A query
that answers after that is logged, and announced, so the main window can say
that what it started without has arrived.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable

from PySide6.QtCore import QObject, QTimer, Signal

log = logging.getLogger(__name__)

__all__ = [
    "AUDIO_INPUTS",
    "CAMERA_FORMATS",
    "FALLBACK_SECONDS",
    "StartupGate",
]

CAMERA_FORMATS = "the camera format probe"
AUDIO_INPUTS = "the audio input check"

#: What the camera is starting without, for each query that has not answered.
_WITHOUT = {
    CAMERA_FORMATS: "The camera opens as soon as its formats are known.",
    # Worded for either stage the check can be stuck at. The inputs are
    # listed first and asked for their formats afterwards, so when this is
    # pinned the Audio list may well be filled already; what is certainly
    # missing is the format each input will be asked for.
    AUDIO_INPUTS: (
        "Until the audio input check answers, a take leaves its audio input on "
        "the first format DirectShow lists, which is 44.1 kHz on both inputs "
        "measured here, so the Blackmagic's 48 kHz audio is resampled."
    ),
}

#: How long the automatic start waits before starting the camera anyway.
#: Between them the queries took under two seconds on the rig, and under nine
#: on a freshly installed Surface Pro 8, so thirty is well clear of a normal
#: wait. It also covers any single ffmpeg device query that runs to its own
#: 15 s timeout and then answers with nothing, which is the kind of failure
#: this exists for.
FALLBACK_SECONDS = 30.0


class StartupGate(QObject):
    """Waits for named start-up queries, then lets the camera start, once.

    Main thread only: begin and finish are called from queued slots, and the
    wait is bounded by a QTimer.
    """

    #: Sent once, when the camera may start: every query has answered, or the
    #: wait ran out. Carries the names still unanswered, in the order they were
    #: given -- empty when every one answered.
    released = Signal(object)
    #: A query answered after the wait ran out without it: its name, and how
    #: many seconds after the release it came. What was pinned when the wait
    #: ran out stays until Record is pressed at the machine, so without this
    #: the booth would go on reading about a start that has since caught up.
    answered_late = Signal(str, float)

    def __init__(self, names: Iterable[str], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._names = tuple(names)
        self._created = time.monotonic()
        self._began: dict[str, float] = {}
        self._took: dict[str, float] = {}
        self._released_at: float | None = None
        self._cancelled = False
        #: Read when the gate is built, not at import, so a test can shorten it.
        self._waited = FALLBACK_SECONDS
        self._fallback = QTimer(self)
        self._fallback.setSingleShot(True)
        self._fallback.setInterval(int(self._waited * 1000))
        self._fallback.timeout.connect(self._give_up)
        self._fallback.start()

    def begin(self, name: str) -> None:
        """Note when a query started. Its time is counted from here."""
        self._began.setdefault(name, time.monotonic())

    def finish(self, name: str) -> bool:
        """Note that a query has answered. False if it had already."""
        if name in self._took:
            return False
        now = time.monotonic()
        self._took[name] = now - self._began.get(name, self._created)
        if self._released_at is not None:
            late = now - self._released_at
            log.info(
                "%s answered %.2f s after the camera stopped waiting for it",
                _capitalised(name), late,
            )
            if not self._cancelled:
                self.answered_late.emit(name, late)
        elif all(each in self._took for each in self._names):
            self._release([])
        return True

    def cancel(self) -> None:
        """Never let the camera start from here: the window is closing."""
        self._cancelled = True
        self._fallback.stop()

    def message_for(self, unanswered: list[str]) -> str:
        """What the status bar and the log say when the wait ran out."""
        consequences = " ".join(_WITHOUT.get(name, "") for name in unanswered)
        return (
            f"Stopped waiting for {_joined(unanswered)} after {self._waited:.0f} s "
            f"and let the camera start. {consequences}"
        ).strip()

    def _give_up(self) -> None:
        if self._released_at is None and not self._cancelled:
            self._release([name for name in self._names if name not in self._took])

    def _release(self, unanswered: list[str]) -> None:
        if self._cancelled:
            return
        self._fallback.stop()
        self._released_at = time.monotonic()
        # One line for the next measurement to read, whichever way it went.
        log.info(
            "Camera autostart released %.2f s after the panels began building: %s",
            self._released_at - self._created, self._timings(),
        )
        if unanswered:
            log.warning("%s", self.message_for(unanswered))
        self.released.emit(list(unanswered))

    def _timings(self) -> str:
        now = time.monotonic()
        parts = []
        for name in self._names:
            began = self._began.get(name)
            if name in self._took:
                text = f"{name} took {self._took[name]:.2f} s"
            elif began is not None:
                text = f"{name} still running after {now - began:.2f} s"
            else:
                text = f"{name} never started"
            if began is not None:
                text += f" from {began - self._created:.2f} s"
            parts.append(text)
        return "; ".join(parts)


def _capitalised(text: str) -> str:
    return text[:1].upper() + text[1:]


def _joined(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]
