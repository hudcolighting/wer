"""The clap test: measure a take, and apply the offset that lines its sound up.

Main thread only. Reading the take and measuring it run on worker threads and
hand their answers back by queued signal; wer.video.clap does the measuring.

The take measured is an ordinary take recorded through Wer's own path, not a
special recording. The offset is a property of that whole path -- camera,
capture, the recording start, the audio input -- so the take measured should
be one a show would get. The operator records it: Record, someone on stage
claps six to ten times, Stop. The main window hands this dialog the take just
recorded, with the camera, audio input and offset it was recorded with.

A person confirms the contact frame for every clap. No automatic reading of the
picture was trustworthy on its own in the research of 12 Sep 2026, so Wer only
puts the cursor on a likely frame.
"""

from __future__ import annotations

import logging
import statistics
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from wer.video import clap as measure
from wer.video.clap import ClapTestError, FrameStrip, Measurement

log = logging.getLogger(__name__)

__all__ = ["ClapResult", "ClapTestDialog", "TakeToMeasure"]

#: How far either side of each clap's sound the frames are read. Wide enough
#: for a camera's picture to trail or lead its sound by a few hundred ms, which
#: is the size of offset the recording start produced before 13 Sep 2026.
WINDOW_S = 0.4
#: The size the still is shown at for marking the hands.
STILL_SIZE = QSize(640, 360)
#: The size each frame is shown at in the filmstrip.
FRAME_ICON = QSize(160, 120)

INSTRUCTIONS = (
    "<p><b>How to run a clap test</b></p>"
    "<ol>"
    "<li>Press <b>Record</b> with the camera and audio input the show will use.</li>"
    "<li>Have someone on stage, in view of the camera, clap sharply six to ten "
    "times, about a second apart, hands side-on to the camera.</li>"
    "<li>Stop recording, then measure the take here.</li>"
    "</ol>"
    "<p>Clap on stage, not at the microphone. The sound's travel time from the "
    "stage is then measured too, and taken out with everything else, so the "
    "recording matches what happens on stage.</p>"
)


@dataclass(frozen=True)
class TakeToMeasure:
    """A take, and what it was recorded with."""

    path: Path
    camera: str
    audio: str | None
    av_offset_ms: int


@dataclass(frozen=True)
class ClapResult:
    """An offset measured by a clap test, for the devices its take used."""

    camera: str
    audio: str
    offset_ms: int
    lag_ms: float
    recorded_offset_ms: int
    claps: int
    frame_period_ms: float


class _Answer(QObject):
    """Carries one worker's answer to the main thread.

    Not the dialog itself, for the reason RecordPanel's _Answer gives: the
    dialog can be closed while ffmpeg is still reading, and a parentless
    emitter held by the worker outlives it, with Qt dropping the queued call.
    """

    ready = Signal(object)


@dataclass(frozen=True)
class _ClapsFound:
    track_gaps: int
    track_largest_gap: float
    found: int
    series: list[measure.Clap]
    still: np.ndarray


def _find_claps(answer: _Answer, path: Path, generation: int) -> None:
    """Find the claps and a still to mark the hands on. Answers (generation, result)."""
    try:
        track = measure.read_audio(path)
        try:
            found = measure.find_claps(track)
        except measure.SilentTakeError as silence:
            # Not "clap louder": there is nothing for a clap to stand above.
            log.warning("Clap test: %s (%s)", silence, path)
            raise ClapTestError(
                "The take's sound is digital silence: the audio input delivered "
                "nothing at all, not quiet sound, so there is no clap in the "
                "take to find. Clapping louder will not change it; the audio "
                "input is the thing to change. A laptop microphone with its "
                "voice effects on does this, and so does a capture device whose "
                'HDMI carries no audio. See "The recording has no sound" under '
                "Help → Troubleshooting."
            ) from silence
        if not found:
            raise ClapTestError(
                "No claps were found in the take's sound. Clap sharply, loud "
                "enough to stand clearly above the room."
            )
        series = measure.regular_series(found)
        if not series:
            raise ClapTestError(
                f"The take's sound has {len(found)} sharp sound"
                f"{'s' if len(found) != 1 else ''}, but not "
                f"{measure.SERIES_MINIMUM} or more evenly spaced claps. Clap "
                "about once a second, six to ten times."
            )
        still, _time = measure.read_still(path, series[0].time)
        answer.ready.emit((generation, _ClapsFound(track.gaps, track.largest_gap, len(found), series, still)))
    except ClapTestError as exc:
        answer.ready.emit((generation, exc))
    except Exception:  # noqa: BLE001 - a worker must always answer
        log.exception("The clap test could not measure %s", path)
        answer.ready.emit((generation, ClapTestError("The take could not be measured; the log says why.")))


def _read_strips(answer: _Answer, path: Path, claps: list[measure.Clap], region, generation: int) -> None:
    """Read the frames around each clap. Answers (generation, result)."""
    try:
        strips = []
        for heard in claps:
            strip = measure.read_frames(
                path, start=heard.time - WINDOW_S, duration=2 * WINDOW_S, region=region
            )
            strips.append((heard, strip, measure.suggest_contact(strip, heard.time)))
        answer.ready.emit((generation, strips))
    except ClapTestError as exc:
        answer.ready.emit((generation, exc))
    except Exception:  # noqa: BLE001 - a worker must always answer
        log.exception("The clap test could not read the frames of %s", path)
        answer.ready.emit((generation, ClapTestError("The take's frames could not be read; the log says why.")))


def _pixmap(bgr: np.ndarray) -> QPixmap:
    image = np.ascontiguousarray(bgr)
    height, width = image.shape[:2]
    qimage = QImage(image.data, width, height, 3 * width, QImage.Format.Format_BGR888)
    return QPixmap.fromImage(qimage.copy())


class _RegionPicker(QLabel):
    """A still of the take, on which the operator drags a box around the hands."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scale = 1.0
        self._source_size = (0, 0)
        self._anchor: QPoint | None = None
        self._box: QRect | None = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_image(self, bgr: np.ndarray) -> None:
        height, width = bgr.shape[:2]
        self._source_size = (width, height)
        self._scale = min(STILL_SIZE.width() / width, STILL_SIZE.height() / height, 1.0)
        pixmap = _pixmap(bgr).scaled(
            round(width * self._scale), round(height * self._scale),
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(pixmap)
        self.setFixedSize(pixmap.size())
        self._box = None
        self.update()

    def region(self) -> tuple[int, int, int, int] | None:
        """The box in the take's own pixels, or None until one is drawn."""
        if self._box is None or self._box.width() < 4 or self._box.height() < 4:
            return None
        width, height = self._source_size
        x = max(0, round(self._box.left() / self._scale))
        y = max(0, round(self._box.top() / self._scale))
        w = min(width - x, round(self._box.width() / self._scale))
        h = min(height - y, round(self._box.height() / self._scale))
        return (x, y, w, h) if w >= 2 and h >= 2 else None

    def set_region(self, region: tuple[int, int, int, int]) -> None:
        x, y, w, h = region
        self._box = QRect(
            round(x * self._scale), round(y * self._scale),
            max(4, round(w * self._scale)), max(4, round(h * self._scale)),
        )
        self.update()
        self.changed.emit()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._anchor = event.position().toPoint()
        self._box = QRect(self._anchor, self._anchor)
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._anchor is not None:
            self._box = QRect(self._anchor, event.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._anchor is not None:
            self._box = QRect(self._anchor, event.position().toPoint()).normalized()
            self._anchor = None
            self.update()
            self.changed.emit()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().paintEvent(event)
        if self._box is None:
            return
        painter = QPainter(self)
        painter.setPen(QPen(QColor("#ffd400"), 2))
        painter.drawRect(self._box)
        painter.end()


class ClapTestDialog(QDialog):
    """Measure a take of claps, and offer the offset that cancels its lag."""

    #: The operator applied the result. Carries a ClapResult.
    applied = Signal(object)

    def __init__(self, take: TakeToMeasure | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Clap test")
        self.take = take
        #: Answers from a worker started before the latest Start over are
        #: dropped; each start bumps this.
        self._generation = 0
        self._answers: list[_Answer] = []
        self._series: list[measure.Clap] = []
        self._gaps = (0, 0.0)
        self._strips: list[tuple[measure.Clap, FrameStrip, int | None]] = []
        self._clap_index = 0
        self._lags: list[float] = []
        self._periods: list[float] = []
        self.measurement: Measurement | None = None
        self.result: ClapResult | None = None

        self._pages = QStackedWidget()
        self._page_names: dict[str, int] = {}
        self._build_intro()
        self._build_working()
        self._build_region()
        self._build_pick()
        self._build_result()

        layout = QVBoxLayout(self)
        layout.addWidget(self._pages)
        self._show_intro()

    # ------------------------------------------------------------------ pages

    @property
    def page(self) -> str:
        index = self._pages.currentIndex()
        return next(name for name, number in self._page_names.items() if number == index)

    def _add_page(self, name: str, widget: QWidget) -> None:
        self._page_names[name] = self._pages.addWidget(widget)

    def _build_intro(self) -> None:
        page = QWidget()
        column = QVBoxLayout(page)
        guide = QLabel(INSTRUCTIONS)
        guide.setWordWrap(True)
        column.addWidget(guide)
        self._take_label = QLabel()
        self._take_label.setWordWrap(True)
        column.addWidget(self._take_label)
        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color: #c0392b; font-weight: bold;")
        column.addWidget(self._error)
        column.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self._measure_button = QPushButton("Measure this take")
        self._measure_button.clicked.connect(self.measure)
        row.addWidget(self._measure_button)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        column.addLayout(row)
        self._add_page("intro", page)

    def _build_working(self) -> None:
        page = QWidget()
        column = QVBoxLayout(page)
        column.addStretch(1)
        self._working_label = QLabel()
        self._working_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self._working_label)
        busy = QProgressBar()
        busy.setRange(0, 0)
        column.addWidget(busy)
        column.addStretch(1)
        self._add_page("working", page)

    def _build_region(self) -> None:
        page = QWidget()
        column = QVBoxLayout(page)
        self._region_label = QLabel()
        self._region_label.setWordWrap(True)
        column.addWidget(self._region_label)
        self._picker = _RegionPicker()
        self._picker.changed.connect(self._region_changed)
        column.addWidget(self._picker, alignment=Qt.AlignmentFlag.AlignCenter)
        row = QHBoxLayout()
        again = QPushButton("Start over")
        again.clicked.connect(self.start_over)
        row.addWidget(again)
        row.addStretch(1)
        self._region_next = QPushButton("Next")
        self._region_next.setEnabled(False)
        self._region_next.clicked.connect(self.read_frames_around_claps)
        row.addWidget(self._region_next)
        column.addLayout(row)
        self._add_page("region", page)

    def _build_pick(self) -> None:
        page = QWidget()
        column = QVBoxLayout(page)
        self._pick_label = QLabel()
        self._pick_label.setWordWrap(True)
        column.addWidget(self._pick_label)
        self._film = QListWidget()
        self._film.setViewMode(QListView.ViewMode.IconMode)
        self._film.setFlow(QListView.Flow.LeftToRight)
        self._film.setWrapping(False)
        self._film.setMovement(QListView.Movement.Static)
        self._film.setIconSize(FRAME_ICON)
        self._film.setFixedHeight(FRAME_ICON.height() + 60)
        # Bound methods for every slot here, not lambdas: see RecordPanel's
        # Clap test button for the crash on exit a lambda slot caused.
        self._film.itemDoubleClicked.connect(self._frame_double_clicked)
        column.addWidget(self._film)
        row = QHBoxLayout()
        again = QPushButton("Start over")
        again.clicked.connect(self.start_over)
        row.addWidget(again)
        row.addStretch(1)
        skip = QPushButton("Skip this clap")
        skip.clicked.connect(self.skip_clap)
        row.addWidget(skip)
        use = QPushButton("Use this frame")
        use.setDefault(True)
        use.clicked.connect(self.use_frame)
        row.addWidget(use)
        column.addLayout(row)
        self._add_page("pick", page)

    def _build_result(self) -> None:
        page = QWidget()
        column = QVBoxLayout(page)
        self._headline = QLabel()
        self._headline.setWordWrap(True)
        self._headline.setStyleSheet("font-size: 15pt; font-weight: bold;")
        column.addWidget(self._headline)
        self._details = QLabel()
        self._details.setWordWrap(True)
        column.addWidget(self._details)
        self._refusal = QLabel()
        self._refusal.setWordWrap(True)
        self._refusal.setStyleSheet("color: #c0392b; font-weight: bold;")
        column.addWidget(self._refusal)
        column.addStretch(1)
        row = QHBoxLayout()
        again = QPushButton("Start over")
        again.clicked.connect(self.start_over)
        row.addWidget(again)
        row.addStretch(1)
        self._apply_button = QPushButton()
        self._apply_button.clicked.connect(self.apply)
        row.addWidget(self._apply_button)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        column.addLayout(row)
        self._add_page("result", page)

    def _goto(self, name: str) -> None:
        self._pages.setCurrentIndex(self._page_names[name])

    # --------------------------------------------------------------- the flow

    def _show_intro(self, error: str = "") -> None:
        self._generation += 1
        take = self.take
        if take is None:
            self._take_label.setText(
                "<p>No take has been recorded since Wer opened. Record one as above, "
                "then open the clap test again.</p>"
            )
            self._measure_button.setEnabled(False)
        elif not take.audio:
            self._take_label.setText(
                f"<p>The last take, <b>{take.path.name}</b>, was recorded without "
                "sound, so there is nothing to line up. Choose an audio input on the "
                "Recording tab and record the claps again.</p>"
            )
            self._measure_button.setEnabled(False)
        else:
            self._take_label.setText(
                f"<p>Take to measure: <b>{take.path.name}</b><br>"
                f"Camera: {take.camera or 'not known'}<br>"
                f"Audio input: {take.audio}<br>"
                f"Recorded with an A/V offset of {take.av_offset_ms} ms</p>"
            )
            self._measure_button.setEnabled(True)
        self._error.setText(error)
        self._goto("intro")

    def start_over(self) -> None:
        """Back to the first page with the same take.

        A slot of its own because clicked() passes a bool, and the Start over
        buttons were connected to _show_intro, which took it for an error to
        show: in the first clap test on the Blackmagic rig (13 Sep 2026) the
        button raised a TypeError and left the dialog where it was.
        """
        self._show_intro()

    def measure(self) -> None:
        """Find the claps in the take's sound, off the main thread."""
        if self.take is None or not self.take.audio:
            return
        self._generation += 1
        generation = self._generation
        self._working_label.setText("Finding the claps in the take's sound…")
        self._goto("working")
        answer = _Answer()
        answer.ready.connect(self._claps_found)
        self._answers.append(answer)
        threading.Thread(
            target=_find_claps, args=(answer, self.take.path, generation),
            name="clap-test-sound", daemon=True,
        ).start()

    def _claps_found(self, answered) -> None:
        generation, found = answered
        if generation != self._generation:
            return
        if isinstance(found, ClapTestError):
            self._show_intro(str(found))
            return
        self._series = list(found.series)
        self._gaps = (found.track_gaps, found.track_largest_gap)
        others = found.found - len(found.series)
        extra = (
            f" {others} other sound{'s were' if others != 1 else ' was'} left out as "
            "not in time with the claps." if others else ""
        )
        self._region_label.setText(
            f"<p>Found {len(self._series)} claps.{extra}</p>"
            "<p>Drag a box around the clapper's hands, with room for them to move "
            "a little, then press <b>Next</b>.</p>"
        )
        self._picker.set_image(found.still)
        self._region_next.setEnabled(False)
        self._goto("region")

    def set_region(self, region: tuple[int, int, int, int]) -> None:
        """Mark the hands' region, as dragging a box does."""
        self._picker.set_region(region)

    def _region_changed(self) -> None:
        self._region_next.setEnabled(self._picker.region() is not None)

    def read_frames_around_claps(self) -> None:
        region = self._picker.region()
        if region is None or self.take is None:
            return
        self._generation += 1
        generation = self._generation
        self._working_label.setText("Reading the frames around each clap…")
        self._goto("working")
        answer = _Answer()
        answer.ready.connect(self._strips_read)
        self._answers.append(answer)
        threading.Thread(
            target=_read_strips, args=(answer, self.take.path, list(self._series), region, generation),
            name="clap-test-frames", daemon=True,
        ).start()

    def _strips_read(self, answered) -> None:
        generation, strips = answered
        if generation != self._generation:
            return
        if isinstance(strips, ClapTestError):
            self._show_intro(str(strips))
            return
        self._strips = list(strips)
        self._clap_index = 0
        self._lags = []
        self._periods = []
        for _heard, strip, _suggested in self._strips:
            try:
                self._periods.append(measure.frame_period_ms(strip.times))
            except ClapTestError:
                continue
        self._show_clap()

    def current_strip(self) -> FrameStrip:
        return self._strips[self._clap_index][1]

    def select_frame(self, row: int) -> None:
        self._film.setCurrentRow(row)

    def _show_clap(self) -> None:
        heard, strip, suggested = self._strips[self._clap_index]
        self._film.clear()
        for index in range(strip.times.size):
            pixmap = _pixmap(strip.frames[index]).scaled(
                FRAME_ICON, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            label = f"{strip.times[index]:.3f} s"
            if strip.duplicate[index]:
                label += "\nrepeat"
            self._film.addItem(QListWidgetItem(QIcon(pixmap), label))
        if suggested is None:
            suggested = int(np.argmin(np.abs(strip.times - heard.time))) if strip.times.size else 0
        self.select_frame(suggested)
        self._film.scrollToItem(self._film.item(suggested))
        self._film.setFocus()
        notes = []
        if heard.weak:
            notes.append("This clap was quiet.")
        if heard.clipped:
            notes.append("This clap was loud enough to clip; it is still usable.")
        self._pick_label.setText(
            f"<p><b>Clap {self._clap_index + 1} of {len(self._strips)}</b>, heard at "
            f"{heard.time:.2f} s.</p>"
            "<p>Click the first frame where the hands meet. If no frame shows them "
            "together, click the one where they are closest. The arrow keys move one "
            "frame; Skip this clap if the hands cannot be seen.</p>"
            + (f"<p>{' '.join(notes)}</p>" if notes else "")
        )
        self._goto("pick")

    def _frame_double_clicked(self, _item) -> None:
        self.use_frame()

    def use_frame(self) -> None:
        row = self._film.currentRow()
        if row < 0 or not self._strips:
            return
        heard, strip, _suggested = self._strips[self._clap_index]
        self._lags.append((heard.time - measure.picture_time(strip, row)) * 1000.0)
        self._next_clap()

    def skip_clap(self) -> None:
        if self._strips:
            self._next_clap()

    def _next_clap(self) -> None:
        self._clap_index += 1
        if self._clap_index < len(self._strips):
            self._show_clap()
        else:
            self._show_result()

    def _show_result(self) -> None:
        period = statistics.median(self._periods) if self._periods else 33.3
        result = measure.combine(self._lags, period)
        self.measurement = result
        take = self.take
        self._headline.setText(measure.describe_lag(result.lag_ms))
        lines = []
        if result.lags_ms:
            used = len(result.used)
            lines.append(
                f"From {used} of {len(self._strips)} claps"
                + (f"; {len(result.set_aside)} set aside as too far from the rest" if result.set_aside else "")
                + f". They agree to within {result.spread_frames:.1f} frames "
                f"({result.frame_period_ms:.0f} ms each)."
            )
            uncertainty = max(
                value for value in (result.standard_error_ms, result.pick_uncertainty_ms)
                if value == value  # not NaN
            )
            lines.append(f"Uncertainty: about ±{uncertainty:.0f} ms.")
        gaps, largest = self._gaps
        if gaps:
            lines.append(
                f"The take's sound has {gaps} small timestamp jump{'s' if gaps != 1 else ''} "
                f"(largest {largest * 1000:.0f} ms); it was timed by counting samples."
            )
        self._details.setText("<p>" + "</p><p>".join(lines) + "</p>" if lines else "")
        self._refusal.setText(" ".join(result.reasons))
        if take is not None and result.can_apply:
            new = measure.corrected_offset(take.av_offset_ms, result.lag_ms)
            self.result = ClapResult(
                camera=take.camera,
                audio=take.audio or "",
                offset_ms=new,
                lag_ms=result.lag_ms,
                recorded_offset_ms=take.av_offset_ms,
                claps=len(result.used),
                frame_period_ms=result.frame_period_ms,
            )
            self._apply_button.setText(f"Use {new} ms for this camera and audio input")
            self._apply_button.setToolTip(
                f"The take was recorded at {take.av_offset_ms} ms. {new} ms cancels the "
                f"measured lag for {take.camera or 'the camera'} with {take.audio}. "
                "The next take uses it."
            )
            self._apply_button.setEnabled(True)
        else:
            self.result = None
            self._apply_button.setText("Use this offset")
            self._apply_button.setEnabled(False)
        self._goto("result")

    def apply(self) -> None:
        if self.result is None:
            return
        self.applied.emit(self.result)
        self._apply_button.setEnabled(False)
        self._apply_button.setText("Applied: the next take uses it")
