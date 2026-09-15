"""Camera capture on its own thread.

No Qt, for threading discipline. Frames leave here as BGR numpy arrays stamped
with ``time.perf_counter()``, the same clock the DataBus uses, so bus changes
and frames can be correlated for the re-renderable sidecar.

Backend choice
--------------
MSMF is preferred, DSHOW the fallback. Both are tried, but the device *index*
is only meaningful under DSHOW, because that is the order `pygrabber`
enumerated (see ``devices.py``). On a machine with one camera this is moot; on
a machine with three it is not. So MSMF is attempted first and the opened frame
size is sanity-checked against what was asked for; if the result looks wrong,
DSHOW is used instead.

Queueing
--------
Two consumers, and they have opposite requirements.

- The **preview** may drop frames. It gets a bounded drop-oldest queue, so a
  slow UI can never apply backpressure to capture.
- The **encoder** may not. It gets a deep queue, and if that queue ever fills,
  the fact is counted and surfaced rather than silently discarded: silent frame
  drops are worse than a visible warning.

Long takes
----------
A camera that was fine at open() can stop being fine two hours into a take,
with nobody in the booth to see it. Two things are watched for as long as
capture runs. Reads that fail without a break are timed
(``CameraCapture.stalled_for``), and during a take the preview closes and
reopens a camera on that. A delivered rate that falls and stays down is logged
(``RateWatch``). Neither looks at the picture: the Blackmagic goes on
delivering flat black at full rate when its HDMI signal is lost, which is known
and accepted, and a theatre blackout is dark too.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import cv2
import numpy as np

from wer.paths import user_data_dir

log = logging.getLogger(__name__)

__all__ = [
    "Frame",
    "CaptureSettings",
    "CaptureStats",
    "CameraCapture",
    "remembered_backend",
    "remember_backend",
    "RateWatch",
    "CAPTURE_BACKENDS",
]

#: The backends a camera is tried on, in order, and what to call them in the
#: log. Both are kept: whichever delivers closest to the rate asked for is the
#: one used, so a camera that Media Foundation drives better than DirectShow
#: gets it without anyone editing this list. DirectShow is first because a
#: device index is a DirectShow index (wer.video.devices) and because of the
#: rates measured in CameraCapture.open; it is an order, not a preference.
#:
#: A list rather than a literal inside open() so that it can be pointed at one
#: backend from outside -- which is the only honest way to prove the fallback
#: carries an open, a measurement and the frames on its own, on a real camera.
CAPTURE_BACKENDS: tuple[tuple[int, str], ...] = (
    (cv2.CAP_DSHOW, "DSHOW"),
    (cv2.CAP_MSMF, "MSMF"),
)

#: Preview may lag; it must never stall capture. Two frames is enough to hand
#: one over while the next is being written.
PREVIEW_QUEUE_DEPTH = 2

#: The encoder queue absorbs momentary encoder stalls - a keyframe, a disk
#: hiccup. Half a second of slack at 30 fps.
#:
#: It was 60 -- two seconds. A 30-minute soak on the Blackmagic at 1080p30 on
#: 12 Sep 2026 (54,023 frames, none dropped, 7,188 samples at 4 Hz) never saw
#: this queue hold more than ONE frame: peak 1, p99 0, median 0. The depth was
#: not being used.
#:
#: And it could not have helped if it had been. ffmpeg times this pipe by when
#: frames arrive (-use_wallclock_as_timestamps, see build_ffmpeg_command) and
#: the output is -fps_mode cfr, so a backlog drained in a burst after a stall
#: arrives with near-identical timestamps and is collapsed to one frame per
#: output interval. A deep queue turns a gap into a gap plus memory; it cannot
#: buy the picture back.
ENCODER_QUEUE_DEPTH = 15

#: A backend delivering at least this fraction of the requested rate is good
#: enough to stop looking. Not 1.0: cameras run a shade under their nominal
#: rate normally, and 29.9 against 30 is not a reason to go and probe another
#: backend.
RATE_TOLERANCE = 0.9
#: Frames discarded before timing, so auto-exposure has settled -- and the most
#: time that may take. See _measure_rate for why there is a time limit at all.
RATE_SETTLE_FRAMES = 5
RATE_SETTLE_SECONDS = 0.2
#: Frames timed, and the most time the timing may take. At 30 fps the frame cap
#: is reached first (20 frames in 0.67 s), so a healthy camera is measured
#: exactly as it was before the time cap existed.
RATE_SAMPLE_FRAMES = 20
RATE_SAMPLE_SECONDS = 0.7
#: Under this rate, a slow open is far more likely to be a capture device with
#: no signal than a camera in poor light. Measured 12 Sep 2026: the Blackmagic
#: with nothing reaching its input sends a placeholder at exactly 1.0 fps, while
#: the slowest camera measured in poor light, a dim USB webcam, still managed
#: 10 fps and the laptop's own camera 15 to 22.
NO_SIGNAL_RATE = 3.0

#: Which backend last worked for a given camera, so the next launch does not
#: pay to discover it again. Keyed by DEVICE NAME, never by index: indices are
#: DirectShow enumeration order and they move the moment another camera is
#: plugged in -- on this machine, adding a USB camera pushed the Blackmagic
#: from 1 to 2. A remembered index would then point the preference at the wrong
#: camera, which is worse than having no preference at all.
_BACKEND_MEMORY = "camera-backends.json"
_memory_lock = threading.Lock()


def _memory_path():
    return user_data_dir() / _BACKEND_MEMORY


def remembered_backend(device_name: str) -> int | None:
    """The backend that last delivered for this camera, or None.

    Never raises. A preference is an optimisation, and a camera that will not
    open because its preference file is corrupt would be a poor trade.
    """
    if not device_name:
        return None
    try:
        with _memory_lock:
            data = json.loads(_memory_path().read_text(encoding="utf-8"))
        value = data.get(device_name)
        return int(value) if value is not None else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def remember_backend(device_name: str, backend: int) -> None:
    """Record the backend that delivered, for next time. Never raises."""
    if not device_name:
        return
    try:
        path = _memory_path()
        with _memory_lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except (OSError, ValueError):
                data = {}
            if data.get(device_name) == backend:
                return
            data[device_name] = int(backend)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        log.info("Remembered %s as the backend for %s", backend, device_name)
    except (OSError, ValueError, TypeError) as exc:
        log.debug("Could not remember the backend for %s: %s", device_name, exc)


#: No frame for this long and the camera counts as gone. Long enough that a
#: momentary hiccup does not flap the reading, short enough that an operator
#: glancing at the stats line learns the truth within a glance.
LIVE_WINDOW = 2.0

#: Whole seconds in a row that must agree before RateWatch says the delivered
#: rate has fallen, or has come back. Every one of them, not an average: a
#: two-second stall averages 24 fps over ten seconds, and it is not what anyone
#: reading the log the next morning needs to hear about.
RATE_WATCH_SECONDS = 10


@dataclass(frozen=True, slots=True)
class Frame:
    """One captured frame.

    ``image`` is BGR uint8, shaped (height, width, 3) - OpenCV's native layout,
    and the layout ffmpeg is told to expect on the pipe (``-pix_fmt bgr24``).
    """

    image: np.ndarray
    #: perf_counter at the moment the frame was pulled from the device.
    timestamp: float
    #: Monotonically increasing from the start of this capture session.
    index: int

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])


@dataclass(frozen=True, slots=True)
class CaptureSettings:
    """What to ask the device for.

    These are requests, not guarantees. Cameras routinely accept a mode and
    then deliver something else, which is why ``CameraCapture`` reports what it
    actually got rather than what it asked for.
    """

    device_index: int = 0
    #: What the device is called, used only to remember which backend worked
    #: for it. Optional: capture works without it, it just re-discovers the
    #: backend every time.
    device_name: str = ""
    #: How many video capture devices the machine has. Load-bearing: see
    #: CameraCapture.open. 1 unless told otherwise, which is the permissive
    #: value -- a caller that does not know cannot be worse off than before
    #: this existed, and every caller inside Wer does know.
    device_count: int = 1
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    #: Four-character code, e.g. "MJPG". None leaves the driver's default.
    #: Without this, many USB cameras negotiate an uncompressed format and
    #: quietly deliver 5 fps at 1080p.
    fourcc: str | None = "MJPG"
    #: 0, 90, 180 or 270, applied after capture. Not offered in the interface:
    #: a quarter turn swaps the frame's width and height, and a take is written
    #: at the size the camera delivers (actual_resolution), which a turned
    #: frame no longer is. Half a turn is both flips below.
    rotation: int = 0
    #: Mirror the picture left to right, and flip it top to bottom, for a
    #: camera seen in a mirror or mounted upside down; both together are the
    #: same picture as half a turn. Applied after the rotation, to the picture
    #: as turned, and before the overlay is drawn (see CameraCapture._orient).
    #: The preview changes them on a running capture by replacing
    #: CameraCapture.settings, without reopening the camera.
    flip_horizontal: bool = False
    flip_vertical: bool = False


@dataclass
class CaptureStats:
    """Live counters. These belong in the UI, in sight, not buried in a log."""

    frames_captured: int = 0
    frames_dropped_preview: int = 0
    frames_dropped_encoder: int = 0
    read_failures: int = 0
    started_at: float = 0.0
    last_frame_at: float = 0.0
    #: perf_counter of the first of the reads failing now, with no good read
    #: since it; 0.0 while the camera delivers. See CameraCapture.stalled_for.
    failing_since: float = 0.0
    _recent: list[float] = field(default_factory=list)

    def note_frame(self, timestamp: float) -> None:
        self.frames_captured += 1
        self.last_frame_at = timestamp
        self._recent.append(timestamp)
        if len(self._recent) > 60:
            del self._recent[:-60]

    @property
    def measured_fps(self) -> float:
        """Actual delivered rate over the last ~60 frames, or 0.0 if dead.

        This is the number that reveals a camera silently running at 5 fps
        because it negotiated an uncompressed format.

        The `is_live` test is not decoration. The sample window only advances
        in note_frame, i.e. only on a successful read, so a camera that stops
        delivering leaves it frozen and this property went on returning the
        last healthy rate for as long as the app stayed open. Measured with a
        camera killed at t=3 s: 29.65 fps at t=3.1, still 29.65 at t=15.1, with
        1168 read failures counted alongside it. The one number an operator has
        to tell a sick camera from a healthy one was reporting health on a dead
        one, so it now goes to zero when the frames do.
        """
        if len(self._recent) < 2 or not self.is_live:
            return 0.0
        span = self._recent[-1] - self._recent[0]
        return (len(self._recent) - 1) / span if span > 0 else 0.0

    @property
    def is_live(self) -> bool:
        """False once frames stop arriving, so the UI can say so."""
        if self.last_frame_at == 0.0:
            return False
        return (time.perf_counter() - self.last_frame_at) < LIVE_WINDOW


class RateWatch:
    """Logs a delivered frame rate that falls and stays down, and its recovery.

    open() measures the rate once, and nothing measured it again. In a
    three-hour soak take the Integrated Camera spent 66 s under 20 fps, as low
    as 12.3, two hours and 53 minutes in, with no failed reads and no dropped
    frames. Every alarm keyed on failure stayed quiet; -fps_mode cfr duplicated
    frames, so the file still declared 30 fps and the right length; and the only
    record was the live figure on the Preview tab, which nobody watches
    overnight. That stretch of the recording was choppy and the log never said.

    So this says so: once when every second of the last RATE_WATCH_SECONDS has
    delivered under RATE_TOLERANCE of the requested rate, once when every second
    of RATE_WATCH_SECONDS has been back over it, and with a total in the line
    capture stops with. It cannot do anything about the cause -- open()'s own
    warning gives the likely one, a camera lengthening its exposure in poor
    light -- and is not meant to. It is there so the log describes what the
    recording will show.

    A second with no frames at all counts towards neither. That is a dropout,
    which the failed-read alarm, the recorder's stall warning and the preview's
    reopen already report, and reading it as a rate of zero would report it
    twice.

    Nor is a camera warned about for what open() has already warned about.
    open() measures the rate before capture starts, and warns when even the
    best backend is under the floor: in poor light, or on a mode whose probed
    rate is more than it delivers. Every capture session on such a camera used
    to add a second warning ten seconds in, for a dip that began before the
    watch did. That dip is logged at INFO and still counted. Once the camera
    has delivered one good second, a fall is warned about like any other.

    Only the capture thread holds one, so it needs no lock. Times are
    perf_counter seconds passed in, so it can be tested without a camera.
    """

    def __init__(
        self,
        requested_fps: float,
        started_at: float,
        *,
        delivering_at_open: float | None = None,
    ) -> None:
        self.requested_fps = requested_fps
        #: Fewer frames than this in one second is a slow second.
        self.floor = requested_fps * RATE_TOLERANCE
        #: The rate open() settled on, when it measured one.
        self.delivering_at_open = delivering_at_open
        #: Totals over the watch's life, for the summary.
        self.dips = 0
        self.seconds_below = 0.0
        self.lowest: int | None = None

        #: open() warned the camera was under the floor, and no whole second
        #: has been at or over it since.
        self._slow_as_opened = (
            delivering_at_open is not None and delivering_at_open < self.floor
        )
        self._second_started = started_at
        self._frames = 0
        self._low = False
        self._low_since = 0.0
        self._dip_lowest = 0
        #: The run of whole seconds disagreeing with _low: how many, from when,
        #: their frames in total, and the fewest in any one of them.
        self._run = 0
        self._run_since = 0.0
        self._run_frames = 0
        self._run_lowest = 0

    def frame(self) -> None:
        """A frame arrived in the second now open."""
        self._frames += 1

    def tick(self, now: float) -> None:
        """Close every whole second that has ended by ``now``.

        Called on every pass of the capture loop, failed reads included, so
        seconds close on time however scarce the frames. A read that blocked for
        several seconds closes them all at once, the later ones empty.
        """
        while now - self._second_started >= 1.0:
            started = self._second_started
            self._second_started += 1.0
            frames, self._frames = self._frames, 0
            self._close_second(started, frames)

    def finish(self, now: float) -> None:
        """Count a dip still going when capture stops."""
        if self._low:
            self.seconds_below += max(0.0, now - self._low_since)
            self._low = False

    def summary(self) -> str:
        """For the line capture stops with. Empty if the rate never fell."""
        if not self.dips:
            return ""
        return (
            f"; under {self.floor:.0f} fps for {self.seconds_below:.0f} s in "
            f"{self.dips} dip(s), at worst {self.lowest} fps"
        )

    def _close_second(self, started: float, frames: int) -> None:
        if frames == 0:
            self._run = 0
            return
        low = frames < self.floor
        if not low:
            self._slow_as_opened = False
        if low == self._low:
            self._run = 0
            if low:
                self._dip_lowest = min(self._dip_lowest, frames)
                self.lowest = frames if self.lowest is None else min(self.lowest, frames)
            return

        if self._run == 0:
            self._run_since, self._run_frames, self._run_lowest = started, 0, frames
        self._run += 1
        self._run_frames += frames
        self._run_lowest = min(self._run_lowest, frames)
        if self._run < RATE_WATCH_SECONDS:
            return

        rate = self._run_frames / self._run
        self._run = 0
        self._low = low
        if low:
            self.dips += 1
            self._low_since = self._run_since
            self._dip_lowest = self._run_lowest
            self.lowest = (
                self._run_lowest
                if self.lowest is None
                else min(self.lowest, self._run_lowest)
            )
            if self._slow_as_opened:
                log.info(
                    "Camera delivering %.1f fps against the %g requested, under "
                    "%.0f for %d s now, as open() found it (%.1f fps)",
                    rate, self.requested_fps, self.floor, RATE_WATCH_SECONDS,
                    self.delivering_at_open,
                )
            else:
                log.warning(
                    "Camera delivering %.1f fps against the %g requested, under "
                    "%.0f for %d s now. The recording keeps its length but is "
                    "choppier while this lasts.",
                    rate, self.requested_fps, self.floor, RATE_WATCH_SECONDS,
                )
        else:
            length = self._run_since - self._low_since
            self.seconds_below += length
            log.warning(
                "Camera back to %.1f fps after %.0f s under %.0f, at worst %d fps",
                rate, length, self.floor, self._dip_lowest,
            )


class CameraCapture:
    """Opens a camera and runs a capture thread until stopped."""

    def __init__(
        self,
        settings: CaptureSettings,
        *,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.stats = CaptureStats()
        self._on_error = on_error
        #: Applied to each frame before it is queued. The compositor goes
        #: here so the overlay is drawn ONCE per frame and both consumers get
        #: the same picture. Compositing separately for preview and encoder
        #: would double the cost -- 18 ms per frame at 4K rather than 9 -- and
        #: risk the two showing different things, which is exactly the bug you
        #: do not want to discover in playback.
        self.frame_processor: Callable[[np.ndarray], np.ndarray] | None = None
        self._capture: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._backend_name = ""
        self._actual: tuple[int, int, float] = (0, 0, 0.0)
        #: perf_counter when the capture thread ended while the capture was still
        #: wanted: without being asked to, or after stop() gave up on it.
        self._died_at = 0.0
        self._read_exceptions = 0
        #: Times frames came back on this handle after a run of failed reads.
        self._resumes = 0
        #: The delivered rate open() settled on, so the rate watch knows what
        #: open() has already warned about. 0.0 when open() never ran.
        self._rate_at_open = 0.0
        #: Who releases the device when stop() gives up waiting; see stop().
        self._handle_lock = threading.Lock()
        self._exited = False
        self._release_on_exit = False
        #: perf_counter when stop() gave up on the thread; see stalled_for.
        self._given_up_at = 0.0

        self.preview_queue: queue.Queue[Frame] = queue.Queue(PREVIEW_QUEUE_DEPTH)
        self.encoder_queue: queue.Queue[Frame] = queue.Queue(ENCODER_QUEUE_DEPTH)
        #: Set while a recording is armed. Until then the encoder queue is not
        #: fed at all, so an idle preview costs nothing.
        self.feed_encoder = threading.Event()

    # ------------------------------------------------------------------ opening

    def open(self) -> bool:
        """Open the device on whichever backend actually delivers the rate.

        Opening is not the same as working, and neither is delivering a frame.
        Both Windows backends will open this laptop's camera, report 1920x1080
        and hand over frames -- and one of them does it at half the rate.
        Measured back to back in the same room, alternating to keep the light
        from confounding it: DirectShow 30.10 and 30.08 fps, Media Foundation
        15.59 and 22.26, at identical resolution and identical mean brightness.
        Media Foundation spent 97.7% and 55.8% of its seconds under 29 fps;
        DirectShow spent none.

        The difference only shows up in poor light, which is exactly when a
        theatre camera is working, and it is not fixable through the exposure
        properties: this camera accepts every value written to
        CAP_PROP_AUTO_EXPOSURE and CAP_PROP_EXPOSURE, returns True, and changes
        nothing. So the choice of backend IS the fix.

        DirectShow is tried first on that evidence, but the order is not the
        safeguard -- the measurement is. Whichever backend delivers closest to
        the requested rate is the one kept, so a machine where this comes out
        the other way corrects itself without anyone editing this list.

        Trying DirectShow first is right for a second reason that has nothing
        to do with speed. wer.video.devices is explicit that a device index is
        a DIRECTSHOW index -- the names come from pygrabber, which is
        DirectShow, and Media Foundation does not necessarily enumerate in the
        same order. Opening index 1 under Media Foundation could therefore
        open a different camera than the one the user picked by name, on any
        machine with more than one. Preferring the backend the index actually
        belongs to closes that quietly as well.
        """
        best: tuple[float, str, cv2.VideoCapture, tuple, str] | None = None

        # Whichever backend delivered for this camera last time goes first, so
        # a camera whose answer is already known costs one open instead of two.
        # It changes the ORDER, never the verdict: the measurement below still
        # decides, and a remembered backend that has since become poor is
        # overtaken exactly as it would have been.
        #
        # Worth it because the loser is expensive. Measured here on 12 Sep 2026
        # with three cameras connected: the USB camera takes 11.2 s to reject
        # DirectShow (10.0 fps, of which 6.8 s is DirectShow rebuilding its
        # graph for each property set) before reaching Media Foundation, which
        # opens it in 1.6 s at 30.1 fps. Remembering turns 12.8 s into 1.6 s.
        # The laptop's Integrated Camera is the opposite case -- Media
        # Foundation cannot open it at all -- which is why this is a
        # preference and not a new default.
        order = list(CAPTURE_BACKENDS)

        # WITH MORE THAN ONE CAMERA ATTACHED, DIRECTSHOW IS THE ONLY BACKEND
        # THAT MAY BE USED, and this is a correctness rule rather than a
        # preference.
        #
        # A device index here is a DIRECTSHOW index: the names come from
        # pygrabber, which is DirectShow (wer.video.devices). Media Foundation
        # enumerates in its own order, so the same integer means a different
        # camera. Opening index 2 under Media Foundation on a machine whose
        # DirectShow index 2 is the Blackmagic opens whatever Media Foundation
        # happens to call 2.
        #
        # That is not hypothetical. On 12 Sep 2026, with an SDI source on a
        # Blackmagic and three cameras attached, DirectShow could not open
        # 2048x1080 at 59.94, the code fell through to Media Foundation with
        # the same index, and Media Foundation opened the USB webcam at
        # 1920x1080@30. The interface went on saying "Blackmagic WDM Capture"
        # over a picture from a different camera, and a take recorded then
        # would have been of the wrong thing entirely.
        #
        # It also cost eleven seconds inside open() on the interface thread,
        # which Windows reported as AppHangTransient and a person reasonably
        # reads as a crash.
        #
        # So: one camera, and an index cannot be ambiguous -- try both, and the
        # measurement decides as before. More than one, and only the backend
        # the index actually belongs to may be used. Failing to open is the
        # right outcome then; opening something else is not.
        if self.settings.device_count > 1:
            order = [pair for pair in CAPTURE_BACKENDS if pair[0] == cv2.CAP_DSHOW]
            log.info(
                "%d cameras attached, so only DirectShow is safe for device %d: "
                "a device index is a DirectShow index and Media Foundation "
                "numbers them differently.",
                self.settings.device_count, self.settings.device_index,
            )
        else:
            preferred = remembered_backend(self.settings.device_name)
            if preferred is not None:
                order.sort(key=lambda pair: pair[0] != preferred)
                if order[0][0] == preferred:
                    log.info(
                        "Trying %s first: it delivered for %s last time",
                        order[0][1], self.settings.device_name,
                    )

        for backend, name in order:
            capture = self._try_backend(backend, name)
            if capture is None:
                continue
            rate = self._measure_rate(capture)
            log.info(
                "%s delivers %.1f fps (asked for %g)", name, rate, self.settings.fps
            )
            if best is None or rate > best[0]:
                if best is not None:
                    best[2].release()
                best = (rate, name, capture, self._actual, self._actual_fourcc)
            else:
                capture.release()
            if rate >= self.settings.fps * RATE_TOLERANCE:
                break

        if best is None:
            # Only what was actually tried. With more than one camera attached
            # that is DirectShow alone, and "either Media Foundation or
            # DirectShow" sent the reader after a backend never involved.
            tried = " or ".join(
                "DirectShow" if backend_code == cv2.CAP_DSHOW else "Media Foundation"
                for backend_code, _name in order
            )
            asked = self.settings.fourcc or "the driver's default format"
            self._fail(
                f"Could not open camera index {self.settings.device_index} at "
                f"{self.settings.width}x{self.settings.height}@{self.settings.fps:g} "
                f"({asked}) with {tried}."
            )
            return False

        rate, self._backend_name, self._capture, self._actual, delivered_format = best
        self._rate_at_open = rate
        # Only worth remembering a backend that actually delivered. Recording a
        # poor one would make the next launch try the loser first and pay for
        # both, which is the opposite of the point.
        if rate >= self.settings.fps * RATE_TOLERANCE:
            chosen = next(
                (code for code, name in CAPTURE_BACKENDS if name == self._backend_name),
                None,
            )
            if chosen is not None:
                remember_backend(self.settings.device_name, chosen)
        if rate < self.settings.fps * RATE_TOLERANCE:
            # Both were poor. Not fatal -- the recording still comes out the
            # right length, because the encoder holds constant rate from real
            # arrival times -- but it is a visibly choppier recording and the
            # user is the only one who can do anything about it.
            requested = self.settings.fourcc
            if requested and delivered_format and delivered_format != requested:
                # Blaming the light here sent the operator looking for a lamp
                # when the camera had simply been left on its slow format: a
                # USB camera that sends 1080p at 30 fps only as MJPEG, and at
                # 5 fps uncompressed, came up at 5.0 fps under a warning about
                # poor light.
                log.warning(
                    "Best available capture rate is %.1f fps against the %g requested, "
                    "and the camera is sending %s rather than the %s asked for. "
                    "Many USB cameras reach full rate at this size only in their "
                    "compressed format; try a smaller size or another format.",
                    rate, self.settings.fps, delivered_format, requested,
                )
            elif rate < NO_SIGNAL_RATE:
                # Not the light. Blaming it here sent the reader looking for a
                # lamp while the HDMI source had gone: the Blackmagic with
                # nothing reaching its input sends a placeholder at exactly
                # 1.0 fps, and a camera in poor light still manages several
                # frames a second. See NO_SIGNAL_RATE. Wording only -- nothing
                # here watches for or acts on a missing signal.
                log.warning(
                    "Best available capture rate is %.1f fps against the %g requested. "
                    "That is the rate a capture device sends when nothing is reaching "
                    "its input: check the source is on and its cable is in. A camera "
                    "in poor light usually still manages several frames a second.",
                    rate, self.settings.fps,
                )
            else:
                log.warning(
                    "Best available capture rate is %.1f fps against the %g requested. "
                    "In poor light a camera lengthens its exposure and cannot deliver "
                    "more; more light on the subject, or a smaller size, may help. "
                    "A capture device asked for a mode its source is not sending can "
                    "also deliver fewer frames than requested.",
                    rate, self.settings.fps,
                )
        return True

    def _measure_rate(self, capture: cv2.VideoCapture) -> float:
        """Frames per second this backend is really delivering, right now.

        Capped by time as well as by frames. This runs inside open(), which the
        preview calls on the interface thread, and it used to read a fixed 5
        frames and then time 20 more: well under a second from a healthy camera,
        and some 25 seconds from a source that sends one frame a second. That
        is exactly what a capture device sends with no input signal. Measured
        12 Sep 2026: opening the Blackmagic with its HDMI source gone took
        28.7 s, with the window frozen for all of it.

        At 30 fps and above the frame caps are reached before the time caps,
        so nothing about a healthy camera's measurement changes. A slow source
        stops at the time cap after a read or two, which is all it takes to
        tell one frame a second from thirty.
        """
        started = time.perf_counter()
        for _ in range(RATE_SETTLE_FRAMES):
            capture.read()
            if time.perf_counter() - started >= RATE_SETTLE_SECONDS:
                break

        started = time.perf_counter()
        delivered = 0
        for _ in range(RATE_SAMPLE_FRAMES):
            ok, _frame = capture.read()
            if ok:
                delivered += 1
            if time.perf_counter() - started >= RATE_SAMPLE_SECONDS:
                break
        elapsed = time.perf_counter() - started
        return delivered / elapsed if elapsed > 0 else 0.0

    #: The format the backend kept open() on, as OpenCV reports it, or "" when it
    #: reports none. A class default so a stand-in _try_backend need not set it.
    _actual_fourcc: str = ""

    @staticmethod
    def _fourcc_text(value: object) -> str:
        """The four characters OpenCV reports for a format code, or "" for none."""
        try:
            code = int(value)
        except (TypeError, ValueError):
            return ""
        text = "".join(chr((code >> (8 * shift)) & 0xFF) for shift in range(4))
        # ASCII only. The Blackmagic reports a code that decodes to "}ë6ä",
        # which isprintable() accepts, and the log printed it as a format name.
        return text if text.isascii() and text.isprintable() and text.strip() else ""

    def _try_backend(self, backend: int, name: str) -> cv2.VideoCapture | None:
        log.info("Trying %s backend for device %d", name, self.settings.device_index)
        try:
            capture = cv2.VideoCapture(self.settings.device_index, backend)
        except cv2.error:
            log.exception("%s backend raised while opening", name)
            return None

        if not capture.isOpened():
            log.info("%s backend could not open device", name)
            capture.release()
            return None

        # The format is set LAST, after the size and the rate. OpenCV's
        # DirectShow backend rebuilds the capture graph whenever the size or
        # rate is set and can drop the format on the way -- measured on a USB
        # Color Camera (0c45:6366), which sends 1920x1080 at 30 fps only as
        # MJPEG and at 5 fps uncompressed: asked for MJPG before the size
        # alone, OpenCV reported YUY2 and delivered 5.0 fps; asked after the
        # rate, it reported MJPG. Setting it afterwards is the fix, and it is
        # the only one of the two that ever mattered.
        #
        # It used to be set twice, before and after, which is where the "twice
        # over" in this comment came from. The first set was doing nothing but
        # costing time: each set rebuilds the graph, and on DirectShow that is
        # seconds, not milliseconds. Measured 12 Sep 2026, four alternating
        # trials per camera, identical results both ways -- USB Color Camera
        # 5.94 s of sets against 4.56 s, both giving 1920x1080 MJPG at 10.0
        # fps; Integrated Camera 2.46 s against 1.85 s, both giving 1920x1080
        # YUY2 at 30.5 fps. If a camera ever turns up that needs the early set
        # back, it will show as the wrong fourcc in the "got" line logged
        # below, which is how the original fault was found.
        code = cv2.VideoWriter_fourcc(*self.settings.fourcc) if self.settings.fourcc else None
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.settings.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.settings.height)
        capture.set(cv2.CAP_PROP_FPS, self.settings.fps)
        if code is not None:
            capture.set(cv2.CAP_PROP_FOURCC, code)

        # Prove it actually delivers before declaring success. isOpened() lying
        # is common enough on Windows to be the default assumption.
        ok, probe = capture.read()

        # A device can list a pixel format that OpenCV's DirectShow backend
        # cannot read. It opens, accepts the format, and read() returns nothing,
        # instantly and for ever. Measured 12 Sep 2026 on a Blackmagic
        # UltraStudio Recorder 3G: of the four formats it lists -- UYVY, V210,
        # R210 and BGR0 -- only UYVY delivered, at every size and rate tried.
        #
        # Asking again on the same handle does not help: re-setting the format
        # there left the capture at 0x0. A fresh open on the SAME backend and
        # the SAME index, with the size and rate but the format left to the
        # driver, delivered at once. Same backend and index means it is still
        # the device that was asked for -- unlike falling through to the other
        # backend, which is how the wrong camera was opened before.
        fell_back = False
        if (not ok or probe is None) and code is not None:
            log.warning(
                "%s opened device %d as %s but delivered no frame; opening it "
                "again at %dx%d@%g with the format left to the driver",
                name, self.settings.device_index, self.settings.fourcc,
                self.settings.width, self.settings.height, self.settings.fps,
            )
            capture.release()
            try:
                capture = cv2.VideoCapture(self.settings.device_index, backend)
            except cv2.error:
                log.exception("%s backend raised while reopening", name)
                return None
            if not capture.isOpened():
                log.info("%s backend could not reopen device", name)
                capture.release()
                return None
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.settings.width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.settings.height)
            capture.set(cv2.CAP_PROP_FPS, self.settings.fps)
            ok, probe = capture.read()
            fell_back = True

        if not ok or probe is None:
            log.info(
                "%s opened the device but delivered no frame (asked %dx%d@%g, %s)",
                name, self.settings.width, self.settings.height, self.settings.fps,
                "format left to the driver" if fell_back
                else (self.settings.fourcc or "driver default format"),
            )
            capture.release()
            return None

        height, width = probe.shape[:2]
        fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
        self._actual = (width, height, fps)
        self._actual_fourcc = self._fourcc_text(capture.get(cv2.CAP_PROP_FOURCC))
        log.info(
            "%s opened device %d: asked %dx%d@%g %s, got %dx%d@%g %s",
            name, self.settings.device_index,
            self.settings.width, self.settings.height, self.settings.fps,
            self.settings.fourcc or "driver default",
            width, height, fps, self._actual_fourcc or "(format not reported)",
        )
        if fell_back:
            log.warning(
                "%s cannot read %s from this device, so it is capturing in the "
                "driver's own format instead. OpenCV hands over 8-bit BGR "
                "whichever format is used; picking a readable format in the "
                "Preview tab saves this retry at every open.",
                name, self.settings.fourcc,
            )
        if (width, height) != (self.settings.width, self.settings.height):
            # Not fatal - the pipeline adapts - but the user must be told,
            # because it silently changes the recorded resolution.
            log.warning(
                "Camera delivered %dx%d rather than the requested %dx%d",
                width, height, self.settings.width, self.settings.height,
            )
        return capture

    # ------------------------------------------------------------------ running

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("capture already started")
        self._stop.clear()
        self._died_at = 0.0
        self._exited = False
        self._release_on_exit = False
        self._given_up_at = 0.0
        self.stats = CaptureStats(started_at=time.perf_counter())
        self._thread = threading.Thread(
            target=self._run, name="capture", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> bool:
        """Stop the capture thread and release the device.

        True once the device has been released. False when the thread is still
        inside a read that has not returned: the handle is then left for that
        thread to release on its way out, and the device is still in use.

        It used to be released from here regardless, underneath the read in
        progress. That was never safe, and it matters more now that the preview
        reopens a camera in the middle of a take: nothing may open a device
        while an old read still holds it, so the caller has to be told.

        Whether a DirectShow read can block like that when its device is pulled
        is not established. Reads measured on the Blackmagic come back in about
        10 ms, even with no signal. This is the safe answer if one ever does.

        Asked again once it has given up, it does not wait again; it only says
        whether the thread has gone since. The preview asks at every reopen
        attempt, on the interface thread, and each ask used to sit out the
        whole timeout again. With a read that stayed stuck, the interface --
        Stop Recording included -- froze for three seconds at every attempt,
        every 30 s for the rest of the take.

        Nor does it ever fall back to releasing the handle under the read.
        Tearing the device down might be what makes a stuck read return, and
        nothing here says either way. But nothing documents an OpenCV capture
        as safe to release from one thread while another is inside read() on
        it. If it is not, the crash takes the whole process, and the take's
        audio and markers with it. A camera that stays lost takes neither.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and not self._release_on_exit:
            thread.join(timeout)
        with self._handle_lock:
            if thread is not None and not self._exited:
                if not self._release_on_exit:
                    log.warning(
                        "Capture thread did not stop within %gs; leaving the "
                        "device for it to release when its read returns",
                        timeout,
                    )
                    self._release_on_exit = True
                    self._given_up_at = time.perf_counter()
                return False
            self._thread = None
            if self._capture is not None:
                self._capture.release()
                self._capture = None
        return True

    def _run(self) -> None:
        watch = RateWatch(
            self.settings.fps,
            time.perf_counter(),
            delivering_at_open=self._rate_at_open or None,
        )
        try:
            self._read_frames(watch)
        except Exception:  # noqa: BLE001 - thread boundary
            # Anything that got out of the loop used to end capture with a
            # crash line in the log and nothing more: no message on the
            # picture, and nothing the preview could reopen the camera on.
            log.exception("Capture thread failed")
            self._fail(
                "Capture stopped unexpectedly. The camera may need reopening; "
                "see the log for details."
            )
        finally:
            ended = time.perf_counter()
            watch.finish(ended)
            with self._handle_lock:
                self._exited = True
                if not self._stop.is_set() or self._release_on_exit:
                    # Under the lock, so it agrees with stop() about which of
                    # the two it was. A thread stop() gave up on used to be left
                    # out here, and it is still the preview's capture while a
                    # reopen waits for it. When its stuck read came back with a
                    # frame, that frame cleared failing_since on the way out, so
                    # stalled_for read 0.0 on a capture with no thread. The
                    # preview logged the camera as delivering again by itself
                    # and stopped looking, and the take had no more video.
                    self._died_at = ended
                if self._release_on_exit and self._capture is not None:
                    self._capture.release()
                    self._capture = None
                    log.info("Capture thread released the device on its way out")
            resumed = (
                f"; frames resumed on the same handle {self._resumes} time(s)"
                if self._resumes
                else ""
            )
            log.info(
                "Capture thread exiting after %d frames%s%s",
                self.stats.frames_captured, watch.summary(), resumed,
            )

    def _read_frames(self, watch: RateWatch) -> None:
        capture = self._capture
        if capture is None:
            self._fail("Capture thread started before the device was opened")
            return

        consecutive_failures = 0
        index = 0
        while not self._stop.is_set():
            try:
                ok, image = capture.read()
            except Exception:  # noqa: BLE001 - a native backend call
                # Nothing here has seen read() raise, but it is a call into a
                # native backend, and cv2.error is how OpenCV reports trouble
                # inside one. Either way no frame came, so it is counted as the
                # failed read it is. Uncaught, it ended this thread with a crash
                # line and nothing else: no failure count, no alarm, nothing for
                # the preview's reopen to see.
                self._read_exceptions += 1
                if self._read_exceptions in (1, 10, 100, 1000):
                    log.exception(
                        "Camera read raised (%d so far); counted as a failed read",
                        self._read_exceptions,
                    )
                ok, image = False, None
            now = time.perf_counter()
            watch.tick(now)

            if not ok or image is None:
                self.stats.read_failures += 1
                if consecutive_failures == 0:
                    self.stats.failing_since = now
                consecutive_failures += 1
                # A few dropped reads are normal when a device re-negotiates.
                # A sustained run means the camera is gone - unplugged, or
                # claimed by another app - and the user needs telling.
                if consecutive_failures == 30:
                    self._fail(
                        "The camera stopped delivering frames. It may have been "
                        "unplugged, or another application may have taken it."
                    )
                time.sleep(0.01)
                continue

            if consecutive_failures >= 30:
                # Said out loud because nobody knows yet whether it happens.
                # Whether a DirectShow handle delivers again after its device
                # drops out and comes back has never been measured here, and
                # this line is how a replug test on the rig finds out. Only the
                # 1st, 10th, 100th and 1000th time, though, with the total in
                # the line capture stops with: a device that flaps, thirty-odd
                # failed reads and a frame over and over, would otherwise add a
                # line about three times a second for as long as it lasted.
                self._resumes += 1
                if self._resumes in (1, 10, 100, 1000):
                    log.warning(
                        "Frames resumed on the same camera handle after %.1f s "
                        "and %d failed reads (%d time(s) so far)",
                        now - self.stats.failing_since, consecutive_failures,
                        self._resumes,
                    )
            consecutive_failures = 0
            self.stats.failing_since = 0.0
            watch.frame()
            image = self._orient(image)

            processor = self.frame_processor
            if processor is not None:
                try:
                    image = processor(image)
                except Exception:  # noqa: BLE001 - never lose a frame to the overlay
                    log.exception("Frame processor failed; using the clean frame")

            frame = Frame(image=image, timestamp=now, index=index)
            index += 1
            self.stats.note_frame(now)

            self._offer_preview(frame)
            if self.feed_encoder.is_set():
                self._offer_encoder(frame)

    def _orient(self, image: np.ndarray) -> np.ndarray:
        """Apply the rotation, then the flips.

        _read_frames calls this before frame_processor, which is where the
        overlay is drawn. So the overlay goes onto the picture as turned, and
        its text is never mirrored with it; and the preview, the recording and
        snapshots all take the one frame, so none of them can show the picture
        the other way round from the rest.

        The settings are read once per frame. The preview replaces
        ``self.settings`` from the interface thread when a flip is ticked, and
        read attribute by attribute, a frame landing on that change could take
        one flip from the old settings and the other from the new.
        """
        settings = self.settings
        rotation = settings.rotation % 360
        if rotation == 90:
            image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        elif rotation == 180:
            image = cv2.rotate(image, cv2.ROTATE_180)
        elif rotation == 270:
            image = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)

        if settings.flip_horizontal and settings.flip_vertical:
            image = cv2.flip(image, -1)
        elif settings.flip_horizontal:
            image = cv2.flip(image, 1)
        elif settings.flip_vertical:
            image = cv2.flip(image, 0)
        return image

    def _offer_preview(self, frame: Frame) -> None:
        """Drop-oldest. The preview must never apply backpressure to capture."""
        try:
            self.preview_queue.put_nowait(frame)
        except queue.Full:
            try:
                self.preview_queue.get_nowait()
                self.preview_queue.put_nowait(frame)
                self.stats.frames_dropped_preview += 1
            except (queue.Empty, queue.Full):
                # The consumer took it between our two calls. Harmless.
                pass

    def _offer_encoder(self, frame: Frame) -> None:
        """Never silently drop. A full queue means the encoder is not keeping up."""
        try:
            self.encoder_queue.put_nowait(frame)
        except queue.Full:
            self.stats.frames_dropped_encoder += 1
            if self.stats.frames_dropped_encoder in (1, 10, 100, 1000):
                log.error(
                    "Encoder queue full; dropped %d frame(s). The encoder is not "
                    "keeping up with capture.",
                    self.stats.frames_dropped_encoder,
                )

    # ------------------------------------------------------------------ status

    @property
    def backend(self) -> str:
        return self._backend_name

    @property
    def actual_resolution(self) -> tuple[int, int]:
        return self._actual[0], self._actual[1]

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stalled_for(self) -> float:
        """Seconds the device has been failing to deliver; 0.0 while it delivers.

        The preview reopens a camera mid-take on this, so what counts is kept
        narrow: failed reads with no good one between them, a capture stop()
        has given up on, or a capture thread that has ended while it was still
        wanted -- one that died, or one stop() gave up on whose read has since
        come back. The device itself saying it is gone, or nothing left to read
        it with.

        A capture stop() gave up on counts from when it did, while its thread
        is still running too. When the stuck read came back with a frame, that
        frame cleared the failed reads, and the thread took it through the
        overlay and the queues before it saw the stop. For that pass this read
        0.0 on a running thread, and a dropout check landing in it took the
        camera for delivering again by itself.

        Not a dark or flat picture. The Blackmagic goes on delivering black at
        30 fps, every read back in about 10 ms, when its HDMI signal is lost --
        known and accepted -- and no reopen could bring a signal back. Not
        merely time since the last frame, which a slow overlay or a starved
        thread can run up on a perfectly healthy camera. And not a read that
        has not returned at all on a capture nobody has stopped: that still
        holds the device, and nothing may open it again underneath (see stop()).
        """
        now = time.perf_counter()
        if self._died_at:
            return now - self._died_at
        since = self.stats.failing_since or self._given_up_at
        return now - since if since else 0.0

    def _fail(self, message: str) -> None:
        log.error("%s", message)
        if self._on_error is not None:
            self._on_error(message)
