"""Live camera preview with a device picker.

Main thread only. Frames are produced on the capture thread and handed over
through a bounded drop-oldest queue (see ``wer.video.capture``), so a slow UI
can never apply backpressure to capture -- the preview may drop frames, the
encoder may not.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, replace

import cv2
import numpy as np
from PySide6.QtCore import QLineF, QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from wer.overlay.geometry import DEFAULT_MARGIN, SNAP_SCREEN_PIXELS, Guide
from wer.video.capture import LIVE_WINDOW, CameraCapture, CaptureSettings, Frame
from wer.video.devices import (
    CaptureDevice,
    VideoFormat,
    best_format,
    enumerate_video_devices,
    probe_formats,
)
from wer.video.encoder import raw_bitrate_mbps

log = logging.getLogger(__name__)

__all__ = ["PreviewPanel", "VideoSurface"]

#: A camera whose reads have failed without a break for this long, during a
#: take, is closed and opened again. Longer than the recorder's 3 s stall
#: warning, so a hiccup that clears by itself is reported and left alone; short
#: next to what waiting costs, which is the rest of the take. A gap in the
#: frames is survivable: a six-second one was measured coming out of
#: -fps_mode cfr as a gap and nothing worse (Recorder._check_frames_still_arriving).
REOPEN_AFTER = 5.0
#: The wait after an attempt that did not bring the camera back, doubling up to
#: REOPEN_MAX_GAP. Attempts run on this thread. A camera that is not connected
#: costs a DirectShow enumeration, ~230 ms; one that is connected but will not
#: open can cost as much as open() does, 3.6-4.1 s on the Integrated Camera even
#: when it works. Thirty seconds at worst is a small price in a take that has
#: already lost its picture.
REOPEN_FIRST_GAP = 5.0
REOPEN_MAX_GAP = 30.0
#: A camera lost again within this long of being reopened is not tried again at
#: once, and the wait goes on growing from where it had got to. Otherwise a
#: device that opens and then fails within seconds would be closed and opened
#: every few seconds for the rest of the take, holding the interface for every
#: open and never settling.
REOPEN_HELD_FOR = 60.0

#: The half of each flip box's tooltip that is the same for both.
_FLIPS_ALSO = (
    "Tick both boxes for a camera mounted upside down: together they turn the "
    "picture half a turn.\n\n"
    "Takes effect at once, without reopening the camera, and the preview, the "
    "recording and snapshots all get the same picture. The overlay is drawn "
    "afterwards, so its text still reads the right way round. Can be changed "
    "while recording; every change is written to the log."
)


def _describe_flips(horizontal: bool, vertical: bool) -> str:
    """The picture's flips in the Preview tab's own words, for the log."""
    if horizontal and vertical:
        return "mirrored left to right and flipped top to bottom (upside down)"
    if horizontal:
        return "mirrored left to right"
    if vertical:
        return "flipped top to bottom"
    return "as the camera sends it"


@dataclass
class _LostCamera:
    """A camera that dropped out of a take, while it is being looked for."""

    device: CaptureDevice
    settings: CaptureSettings
    #: What the camera was delivering when it was lost, which is the size the
    #: take is being written at: the recorder's ffmpeg was started on
    #: actual_resolution, and reads raw frames of exactly that many bytes.
    resolution: tuple[int, int]
    #: perf_counter when it stopped delivering.
    since: float
    #: Devices with its name when it was lost. A name cannot pick between two.
    namesakes: int
    next_attempt: float
    gap: float = REOPEN_FIRST_GAP
    attempts: int = 0


class FormatProbe(QObject):
    """Probes camera formats off the main thread.

    Discovering a camera's capabilities has two halves with very different
    characteristics, and only one of them can be moved off the UI thread:

    - **Enumeration** (``enumerate_video_devices``) goes through DirectShow over
      COM and costs ~230 ms. It **must run on the main thread.** Running it on a
      worker made the process segfault during interpreter teardown, reliably and
      reproducibly -- DirectShow's COM objects do not survive being created in a
      short-lived apartment alongside a running Qt event loop. Attempting an
      explicit CoInitialize/CoUninitialize pair around the work did not help.
    - **Probing** (``probe_formats``) shells out to ffmpeg and costs ~1 s per
      device, with a 15 s timeout. It touches no COM at all, so it moves freely.

    Since probing is the expensive four-fifths, threading only that half keeps
    window construction at ~40 ms (down from ~800 ms) with no COM risk. A camera
    that hangs its probe now delays a combo box filling in, rather than freezing
    the whole application for fifteen seconds -- which the main thread must never
    be made to do.

    One look at a time, and only the newest look's answer is sent. Refresh
    pressed while "Checking supported formats..." is still showing -- about a
    second on the Blackmagic rig, and the button is not disabled for it -- used
    to start a second look alongside the first. The first to answer cleared the
    panel's note that the cameras were being probed while the second was still
    asking the same devices, so a held automatic start went ahead, and the main
    window started the audio check, with ffmpeg still querying the UltraStudio's
    video. That is the overlap wer.ui.startup exists to prevent, on the very
    open whose rate measurement decides whether a camera falls back to Media
    Foundation at half its rate.

    So a look asked for while one is running waits for it, replacing any look
    already waiting, and each answer carries its look's number. The number is
    checked here, on the interface thread, and ``finished`` is sent only if no
    newer look has been asked for since -- an answer already on its way when
    Refresh is handled is dropped rather than taken for the new look's.
    However many times ``probe`` is called, ``finished`` comes once, for the
    newest devices, with nothing else probing the cameras.
    """

    #: {device_index: [VideoFormat, ...]}
    #:
    #: Signal(object), NOT Signal(dict). Qt marshals a `dict` signal argument
    #: through QVariantMap, which only supports string keys -- integer device
    #: indices were silently dropped and the receiver got an empty dict, so the
    #: format picker always fell back to unverified defaults. `object` passes
    #: the Python value through untouched.
    finished = Signal(object)
    #: (look number, formats): the worker's answer, queued to this object's own
    #: thread to be checked against the newest look asked for.
    _answered = Signal(int, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._answered.connect(self._deliver)
        #: Looks asked for so far. Read and written on the interface thread only.
        self._asked = 0
        #: Guards the two below, which the worker reads as each look ends.
        self._lock = threading.Lock()
        self._running = False
        #: (number, devices, probe) for the look waiting on the running one.
        self._waiting: tuple | None = None

    def probe(self, devices: list[CaptureDevice]) -> None:
        """Start probing, or queue behind the look running. Returns immediately."""
        self._asked += 1
        # probe_formats is read here and handed in, not read on the worker.
        # There it would be read whenever the thread first ran, and by then a
        # test's stand-in can have been taken away again, which is how
        # conftest.windows_stay_off_real_hardware says real probes reached the
        # hardware during a test run.
        look = (self._asked, list(devices), probe_formats)
        with self._lock:
            if self._running:
                self._waiting = look
                return
            self._running = True
        threading.Thread(
            target=self._work, args=look, name="format-probe", daemon=True
        ).start()

    def _work(self, number: int, devices: list[CaptureDevice], probe) -> None:
        while True:
            try:
                formats = {d.index: probe(d) for d in devices}
            except Exception:  # noqa: BLE001 - thread boundary
                log.exception("Format probe failed")
                formats = {}
            with self._lock:
                if self._waiting is None:
                    self._running = False
                    break
                number, devices, probe = self._waiting
                self._waiting = None
            log.info(
                "Cameras were looked for again while their formats were being "
                "probed; probing the newest list before answering"
            )
        # Signals are queued across threads, so this lands on the main thread.
        self._answered.emit(number, formats)

    def _deliver(self, number: int, formats: dict) -> None:
        """Send the newest look's answer on. Interface thread, queued signal."""
        if number != self._asked:
            log.info(
                "Dropped the formats from camera look %d: look %d was asked for "
                "before they arrived",
                number, self._asked,
            )
            return
        self.finished.emit(formats)


class VideoSurface(QLabel):
    """Draws frames letterboxed, and in edit mode lets widgets be dragged.

    Coordinate mapping is the whole trick here. Widgets are placed in *frame*
    coordinates at the capture resolution, but the preview shows a scaled,
    letterboxed copy. Every hit test and every drag has to convert between the
    two, or a widget lands somewhere other than where it was dropped -- and the
    error grows with how much the preview is scaled, so it would look fine on a
    maximised window and wrong on a small one.

    A drag is reported as the whole movement since the button went down, never
    as a string of small steps: geometry.drag_placement explains why that is
    the difference between a widget that snaps and one that sticks. The editor
    decides where the widget lands and hands back the guides it snapped to,
    which are drawn here over a grid for as long as the widget is moving.
    """

    #: An arrow-key nudge: (widget_id, delta_x, delta_y) as frame fractions.
    widget_moved = Signal(str, float, float)
    #: A widget was clicked, or empty space was (empty string).
    widget_selected = Signal(str)
    #: The button went down on a widget: (widget_id).
    drag_started = Signal(str)
    #: The pointer moved during a drag: (widget_id, delta_x, delta_y, within,
    #: axis). The deltas are the whole movement since the press, as frame
    #: fractions. ``within`` is the snap distance in frame pixels, 0 while Alt
    #: is held. ``axis`` is "" normally, or "x" / "y" while Shift keeps the
    #: drag to one direction.
    widget_dragged = Signal(str, float, float, float, str)
    #: The drag ended: (widget_id, cancelled). Escape cancels it.
    drag_finished = Signal(str, bool)

    #: How long the grid stays up after an arrow-key nudge, in seconds. Long
    #: enough to see where the widget now sits against it, short enough to be
    #: gone before the next thing you look at.
    NUDGE_GRID_SECONDS = 1.2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # The text colour is set as well as the background, and must stay set.
        # With only the background given, the label took its text colour from
        # the Windows theme: white in dark mode, where this was written and
        # never looked wrong, and BLACK in light mode -- black on #111. On a
        # Surface in light mode on 12 Sep 2026 "No camera" and "Starting ..."
        # were both there and unreadable, and the preview looked as though it
        # said nothing at all for the whole start-up wait.
        self.setStyleSheet("background-color: #111; color: #d0d0d0;")
        self.setText("No camera")
        self._pixmap: QPixmap | None = None

        self.edit_mode = False
        self._rects: dict[str, tuple[int, int, int, int]] = {}
        self._frame_size = (0, 0)
        self._selected = ""
        self._dragging = ""
        #: Where the button went down, in frame pixels. Every drag event is
        #: measured from here rather than from the event before it.
        self._press_at = (0.0, 0.0)
        #: The guides the dragged widget is on, as the editor last reported.
        self._guides: tuple[Guide, ...] = ()
        #: The anchor the dragged widget would take if dropped now.
        self._anchor_label = ""
        #: Grid divisions (columns, rows) drawn while a widget is moving.
        self._grid = (32, 18)
        self._grid_until = 0.0
        self._grid_timer = QTimer(self)
        self._grid_timer.setSingleShot(True)
        self._grid_timer.timeout.connect(self.update)

    # ------------------------------------------------------------------ video

    def show_frame(self, image: np.ndarray) -> None:
        height, width = image.shape[:2]
        self._frame_size = (width, height)

        # Shrink FIRST, on the array, then convert. This runs on the main
        # thread for every frame shown, and it used to do three full-frame
        # passes at capture size: a QImage copy, a conversion to QPixmap, and
        # a smooth scale down to the widget. At 1080p that is invisible. At 4K
        # a frame is 23.7 MB and those passes land up to thirty times a second
        # on the thread that also has to answer the Stop button.
        #
        # cv2.resize with INTER_AREA is the right shrink -- it averages the
        # pixels it discards, where a plain scale drops them -- and it releases
        # the GIL and uses SIMD, which none of the Qt steps here do.
        #
        # It also removes the reason for the .copy() that used to be here:
        # QImage does not own the buffer it is handed, so a frame still being
        # written by the capture thread would tear. resize returns a NEW array
        # that nothing else holds, so there is nothing left to race with. The
        # full-size path below keeps the copy, because there the buffer is the
        # capture thread's.
        target = self.size()
        shown = image
        if target.width() > 0 and target.height() > 0 and (
            width > target.width() or height > target.height()
        ):
            scale = min(target.width() / width, target.height() / height)
            shown = cv2.resize(
                image,
                (max(1, int(width * scale)), max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )

        shown_h, shown_w = shown.shape[:2]
        qimage = QImage(
            shown.data, shown_w, shown_h, shown.strides[0], QImage.Format.Format_BGR888
        )
        if shown is image:
            qimage = qimage.copy()
        self._pixmap = QPixmap.fromImage(qimage)
        self._rescale()

    def clear_frame(self, message: str = "No camera") -> None:
        self._pixmap = None
        self.setText(message)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        self.setPixmap(
            self._pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    # -------------------------------------------------------------- edit mode

    def set_edit_mode(self, enabled: bool) -> None:
        if not enabled and self._dragging:
            # Leaving edit mode mid-drag keeps the widget where it is; there is
            # no pointer left to finish the drag with.
            self._finish_drag(cancelled=False)
        self.edit_mode = enabled
        self.setCursor(
            Qt.CursorShape.OpenHandCursor if enabled else Qt.CursorShape.ArrowCursor
        )
        # Click focus while editing, so the arrow keys and Escape come here
        # once a widget has been clicked, rather than to whatever had focus.
        self.setFocusPolicy(
            Qt.FocusPolicy.ClickFocus if enabled else Qt.FocusPolicy.NoFocus
        )
        self.update()

    def set_widget_rects(
        self, rects: dict[str, tuple[int, int, int, int]], frame_size: tuple[int, int]
    ) -> None:
        """Where each widget currently is, in frame pixels, in z-order."""
        self._rects = rects
        if frame_size[0] and frame_size[1]:
            self._frame_size = frame_size
        if self.edit_mode:
            self.update()

    def set_selected(self, widget_id: str) -> None:
        self._selected = widget_id
        self.update()

    def set_snap_feedback(self, guides, anchor_label: str) -> None:
        """What the editor made of the latest drag event, to draw."""
        self._guides = tuple(guides)
        self._anchor_label = anchor_label
        self.update()

    def set_grid(self, columns: int, rows: int) -> None:
        self._grid = (max(1, int(columns)), max(1, int(rows)))
        self.update()

    @property
    def is_moving(self) -> bool:
        """True while the grid should be drawn: a drag, or just after a nudge."""
        return bool(self._dragging) or time.monotonic() < self._grid_until

    # ---------------------------------------------------------------- mapping

    def _display_geometry(self) -> tuple[float, float, float] | None:
        """(offset_x, offset_y, scale) of the video inside this widget."""
        frame_width, frame_height = self._frame_size
        if not frame_width or not frame_height:
            return None
        scale = min(self.width() / frame_width, self.height() / frame_height)
        return (
            (self.width() - frame_width * scale) / 2.0,
            (self.height() - frame_height * scale) / 2.0,
            scale,
        )

    def _to_frame(self, x: float, y: float) -> tuple[float, float] | None:
        geometry = self._display_geometry()
        if geometry is None:
            return None
        offset_x, offset_y, scale = geometry
        return ((x - offset_x) / scale, (y - offset_y) / scale)

    # --------------------------------------------------------------- painting

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        if not self.edit_mode:
            return
        geometry = self._display_geometry()
        if geometry is None:
            return
        offset_x, offset_y, scale = geometry
        frame_width, frame_height = self._frame_size
        video = QRectF(offset_x, offset_y, frame_width * scale, frame_height * scale)

        painter = QPainter(self)
        try:
            if self.is_moving:
                self._paint_grid(painter, video)
                self._paint_guides(painter, video, scale)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self._paint_outlines(painter, offset_x, offset_y, scale)
        finally:
            painter.end()

    def _paint_grid(self, painter: QPainter, video: QRectF) -> None:
        """A faint grid over the picture, with the frame's own guides on top.

        Every line is drawn twice, dark and then light a pixel away. A light
        line alone vanishes into a lit stage and a dark one into a blackout,
        and a tech recording has both within the same minute.
        """
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        columns, rows = self._grid
        lines = [
            QLineF(x, video.top(), x, video.bottom())
            for x in (
                video.left() + video.width() * index / columns
                for index in range(1, columns)
            )
        ] + [
            QLineF(video.left(), y, video.right(), y)
            for y in (
                video.top() + video.height() * index / rows
                for index in range(1, rows)
            )
        ]
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(0, 0, 0, 70), 1))
        for line in lines:
            painter.drawLine(line.translated(1, 1))
        painter.setPen(QPen(QColor(255, 255, 255, 55), 1))
        for line in lines:
            painter.drawLine(line)

        # The lines the frame itself snaps to: the centre lines, and the
        # default margin, which is also roughly where a TV's overscan starts.
        centre = QPen(QColor(255, 255, 255, 150), 1, Qt.PenStyle.DashLine)
        painter.setPen(centre)
        middle = video.center()
        painter.drawLine(QLineF(middle.x(), video.top(), middle.x(), video.bottom()))
        painter.drawLine(QLineF(video.left(), middle.y(), video.right(), middle.y()))
        inset_x = video.width() * DEFAULT_MARGIN
        inset_y = video.height() * DEFAULT_MARGIN
        painter.setPen(QPen(QColor(243, 156, 18, 170), 1, Qt.PenStyle.DashLine))
        painter.drawRect(video.adjusted(inset_x, inset_y, -inset_x, -inset_y))

    def _paint_guides(self, painter: QPainter, video: QRectF, scale: float) -> None:
        """The lines the dragged widget has snapped to, right across the picture.

        Magenta, because it is neither the green of the selection, the amber of
        the margin, nor anything likely to be lit on the stage behind it.
        """
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        pen = QPen(QColor(255, 64, 160, 235))
        pen.setWidth(2)
        painter.setPen(pen)
        for guide in self._guides:
            if guide.axis == "x":
                x = video.left() + guide.position * scale
                painter.drawLine(QLineF(x, video.top(), x, video.bottom()))
            else:
                y = video.top() + guide.position * scale
                painter.drawLine(QLineF(video.left(), y, video.right(), y))

    def _paint_outlines(
        self, painter: QPainter, offset_x: float, offset_y: float, scale: float
    ) -> None:
        for widget_id, (x, y, width, height) in self._rects.items():
            rect = QRectF(
                offset_x + x * scale, offset_y + y * scale,
                width * scale, height * scale,
            )
            selected = widget_id == self._selected
            pen = QPen(
                QColor("#27ae60") if selected else QColor(255, 255, 255, 110)
            )
            pen.setWidth(2 if selected else 1)
            if not selected:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)
            if selected:
                label = widget_id
                if widget_id == self._dragging and self._anchor_label:
                    # Which corner it will hang from if dropped here, because
                    # that decides which way it grows when its text does.
                    label = f"{widget_id}  ·  {self._anchor_label}"
                self._paint_label(painter, rect, label)

    def _paint_label(self, painter: QPainter, rect: QRectF, text: str) -> None:
        """A widget's name on a dark chip, so it reads over any picture."""
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 10
        height = metrics.height() + 4
        # Above the widget when there is room, below it when it is at the top.
        top = rect.top() - height - 3
        if top < 0:
            top = rect.bottom() + 3
        chip = QRectF(rect.left(), top, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawRoundedRect(chip, 3, 3)
        painter.setPen(QColor("#2ecc71"))
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, text)

    # ------------------------------------------------------------------ mouse

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self.edit_mode:
            return super().mousePressEvent(event)
        point = self._to_frame(event.position().x(), event.position().y())
        if point is None:
            return

        # Rects arrive in z-order, so the last match is the topmost widget.
        hit = ""
        for widget_id, (x, y, width, height) in self._rects.items():
            if x <= point[0] <= x + width and y <= point[1] <= y + height:
                hit = widget_id
        self._selected = hit
        self.widget_selected.emit(hit)
        self.setFocus(Qt.FocusReason.MouseFocusReason)

        if hit and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = hit
            self._press_at = point
            self._guides = ()
            self._anchor_label = ""
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.drag_started.emit(hit)
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self.edit_mode or not self._dragging:
            return super().mouseMoveEvent(event)
        geometry = self._display_geometry()
        point = self._to_frame(event.position().x(), event.position().y())
        if geometry is None or point is None:
            return
        frame_width, frame_height = self._frame_size
        moved_x = point[0] - self._press_at[0]
        moved_y = point[1] - self._press_at[1]

        modifiers = event.modifiers()
        axis = ""
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            axis = "x" if abs(moved_x) >= abs(moved_y) else "y"
        # The snap distance is fixed on screen and converted to frame pixels
        # here, the one place that knows how far the picture is scaled.
        within = (
            0.0
            if modifiers & Qt.KeyboardModifier.AltModifier
            else SNAP_SCREEN_PIXELS / geometry[2]
        )
        # As fractions of the frame, the unit placements are stored in, so a
        # drag means the same thing whatever resolution is being previewed.
        self.widget_dragged.emit(
            self._dragging, moved_x / frame_width, moved_y / frame_height, within, axis
        )
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._dragging:
            self._finish_drag(cancelled=False)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self.edit_mode:
            return super().keyPressEvent(event)
        key = event.key()
        if key == Qt.Key.Key_Escape and self._dragging:
            self._finish_drag(cancelled=True)
            return

        steps = {
            Qt.Key.Key_Left: (-1, 0),
            Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1),
            Qt.Key.Key_Down: (0, 1),
        }
        frame_width, frame_height = self._frame_size
        if (
            key in steps and self._selected and not self._dragging
            and frame_width and frame_height
        ):
            step_x, step_y = steps[key]
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                # A whole grid square, for moving a long way in tidy steps.
                columns, rows = self._grid
                delta = (step_x / columns, step_y / rows)
            else:
                # One pixel of the frame: the last bit of placement, which a
                # mouse on a scaled-down preview cannot do.
                delta = (step_x / frame_width, step_y / frame_height)
            self.widget_moved.emit(self._selected, *delta)
            self._grid_until = time.monotonic() + self.NUDGE_GRID_SECONDS
            self._grid_timer.start(int(self.NUDGE_GRID_SECONDS * 1000) + 50)
            self.update()
            return
        super().keyPressEvent(event)

    def _finish_drag(self, *, cancelled: bool) -> None:
        widget_id = self._dragging
        self._dragging = ""
        self._guides = ()
        self._anchor_label = ""
        self.setCursor(
            Qt.CursorShape.OpenHandCursor if self.edit_mode else Qt.CursorShape.ArrowCursor
        )
        self.drag_finished.emit(widget_id, cancelled)
        self.update()


class PreviewPanel(QWidget):
    """Device picker, format picker, live preview and capture statistics."""

    #: Emitted when capture starts or stops, so the rest of the UI can react.
    capture_changed = Signal(bool)
    #: The Record / Stop Recording button was pressed.
    record_toggled = Signal()
    #: An arrow-key nudge on the preview: (id, delta_x, delta_y).
    widget_moved = Signal(str, float, float)
    #: A widget being dragged on the preview. VideoSurface documents the
    #: arguments; these only pass them on.
    drag_started = Signal(str)
    widget_dragged = Signal(str, float, float, float, str)
    drag_finished = Signal(str, bool)
    #: A widget was clicked on the preview (empty string for empty space).
    widget_selected = Signal(str)
    #: The Snapshot button was pressed.
    snapshot_requested = Signal()
    #: A capture-thread failure, marshalled onto the main thread. Internal:
    #: emitting a signal is the only cross-thread hand-off Qt guarantees, and
    #: the capture thread has no event loop for anything else to run on.
    capture_failed = Signal(str)
    #: A look for cameras has finished: every camera found has had its formats
    #: probed, or none was found. Sent after every look, Refresh included; the
    #: main window starts the audio input check on the first one.
    formats_probed = Signal()
    #: Mirror left to right or Flip top to bottom was ticked or cleared. The
    #: main window saves the show on it, so a flip set for a camera mounted
    #: upside down is not lost to a crash before the next autosave.
    flips_changed = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        preferred_device: str = "",
        preferred_format: VideoFormat | None = None,
        hold_autostart: bool = False,
        flip_horizontal: bool = False,
        flip_vertical: bool = False,
    ) -> None:
        super().__init__(parent)
        self.capture: CameraCapture | None = None
        #: Set by the main window. When present, the overlay is drawn on the
        #: preview so what you see is what gets recorded.
        self.compositor: object | None = None
        #: Go live as soon as a camera is found. Turned off by the user pressing
        #: Stop, so an explicit stop is not undone by the next device refresh.
        self.autostart = True
        self._autostarted = False
        #: Keeps the automatic start at launch waiting until release_autostart().
        #: The main window holds it while the start-up queries run; see
        #: wer.ui.startup. It comes in with the constructor because the first
        #: look for cameras happens in here, and nothing may be able to start
        #: the camera before the hold is in place.
        self._autostart_held = hold_autostart
        self._recording = False
        self._devices: list[CaptureDevice] = []
        self._formats: list[VideoFormat] = []
        #: The most recent frame shown, kept so a snapshot saves exactly what
        #: is on screen rather than racing the queue for the next one.
        self._last_frame: np.ndarray | None = None
        self._formats_by_index: dict[int, list[VideoFormat]] = {}
        #: The camera to come back to by NAME, not index: DirectShow indices
        #: move when something is replugged, and re-enumerating in index order
        #: is how Refresh used to move a rig from the stage camera to the
        #: laptop webcam without saying anything. It comes from the show file,
        #: and it has to arrive in the constructor: the first enumeration
        #: happens below, before the caller gets the object back.
        self.preferred_device_name = preferred_device
        #: The format the user picked, and the device it was picked for, so a
        #: restart does not throw the choice away and re-guess.
        self._preferred_format: VideoFormat | None = preferred_format
        self._preferred_format_for = preferred_device if preferred_format else ""
        #: The device the running capture was opened on, so a camera that drops
        #: out of a take can be found again by name when DirectShow renumbers.
        self._capture_device: CaptureDevice | None = None
        #: Set while a camera that dropped out of a take is being brought back.
        self._lost: _LostCamera | None = None
        #: When the last reopen worked, and the wait it had reached, so a camera
        #: that fails again straight away is not cycled (see REOPEN_HELD_FOR).
        self._reopened_at = 0.0
        self._reopen_gap = REOPEN_FIRST_GAP
        #: True while ffmpeg is listing formats, which touches the devices too.
        #: A reopen waits for it rather than opening a camera alongside.
        self._probing = False
        #: True while a reopen attempt runs; see _check_camera_still_delivering.
        self._reopening = False

        self._build_ui()
        self.capture_failed.connect(self._show_capture_error)
        # Ticked before the boxes are connected, and before the first look for
        # cameras below, which can start the camera: it has to open with the
        # show file's flips, and they are not a change anyone made just now,
        # so they are not logged as one. bool(), because a hand-edited show
        # file can hold null there, and null means not set.
        self._mirror_box.setChecked(bool(flip_horizontal))
        self._flip_box.setChecked(bool(flip_vertical))
        self._mirror_box.toggled.connect(self._flips_toggled)
        self._flip_box.toggled.connect(self._flips_toggled)

        # 33 ms: matched to 30 fps. The queue is drop-oldest, so a slow repaint
        # costs a dropped preview frame and nothing else.
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(33)
        self._frame_timer.timeout.connect(self._pull_frame)

        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(500)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start()

        # Once a second is plenty against a five-second threshold, and on a
        # healthy camera it costs two attribute reads.
        self._dropout_timer = QTimer(self)
        self._dropout_timer.setInterval(1000)
        self._dropout_timer.timeout.connect(self._check_camera_still_delivering)
        self._dropout_timer.start()

        self._prober = FormatProbe(self)
        self._prober.finished.connect(self._probe_finished)
        self.refresh_devices()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        picker = QGroupBox("Camera")
        rows = QVBoxLayout(picker)
        row = QHBoxLayout()
        rows.addLayout(row)

        self._device_box = QComboBox()
        self._device_box.setMinimumWidth(220)
        self._device_box.currentIndexChanged.connect(self._device_changed)
        row.addWidget(QLabel("Device:"))
        row.addWidget(self._device_box, 1)

        self._format_box = QComboBox()
        self._format_box.setMinimumWidth(220)
        self._format_box.currentIndexChanged.connect(self._format_changed)
        self._format_box.setToolTip(
            "Resolution, rate and pixel format. Many USB cameras cannot sustain "
            "1080p30 in an uncompressed format and will silently drop to a few "
            "frames per second — if that happens, choose MJPG."
        )
        row.addWidget(QLabel("Format:"))
        row.addWidget(self._format_box, 1)

        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.setToolTip("Look for cameras again.")
        self._refresh_button.clicked.connect(self.refresh_devices)
        row.addWidget(self._refresh_button)

        # The flips get a row of their own under Device and Format, so the
        # Camera box grows no wider for them. They are connected in __init__,
        # once the show file's flips are in, and set_recording leaves both
        # unlocked; _flips_toggled says why.
        flips = QHBoxLayout()
        self._mirror_box = QCheckBox("Mirror left to right")
        self._mirror_box.setToolTip(
            "Mirror the picture left to right, for a camera that sees the stage "
            "in a mirror.\n\n" + _FLIPS_ALSO
        )
        self._flip_box = QCheckBox("Flip top to bottom")
        self._flip_box.setToolTip("Flip the picture top to bottom.\n\n" + _FLIPS_ALSO)
        flips.addWidget(QLabel("Picture:"))
        flips.addWidget(self._mirror_box)
        flips.addWidget(self._flip_box)
        flips.addWidget(QLabel("Tick both for a camera mounted upside down."))
        flips.addStretch(1)
        rows.addLayout(flips)
        layout.addWidget(picker)

        # The one prominent button on this tab is the RECORD control. The
        # camera itself is not something to start and stop: it runs whenever
        # the app is open, before, during and after a take. Having a camera
        # button here as well invited stopping the picture while meaning to
        # stop a take, which is exactly what happened.
        self._record_button = QPushButton("● Record")
        record_font = QFont()
        record_font.setPointSize(13)
        record_font.setBold(True)
        self._record_button.setFont(record_font)
        self._record_button.setMinimumHeight(48)
        self._record_button.clicked.connect(self.record_toggled.emit)

        self._snapshot_button = QPushButton("Snapshot")
        self._snapshot_button.setMinimumHeight(48)
        self._snapshot_button.setToolTip(
            "Save a still of what you can see, overlay included (F12).\n"
            "Works whether or not you are recording."
        )
        self._snapshot_button.clicked.connect(self.snapshot_requested.emit)

        buttons = QHBoxLayout()
        buttons.addWidget(self._record_button, 3)
        buttons.addWidget(self._snapshot_button, 1)
        layout.addLayout(buttons)
        self._update_record_button()

        self._surface = VideoSurface()
        self._surface.widget_moved.connect(self.widget_moved.emit)
        self._surface.widget_selected.connect(self.widget_selected.emit)
        self._surface.drag_started.connect(self.drag_started.emit)
        self._surface.widget_dragged.connect(self.widget_dragged.emit)
        self._surface.drag_finished.connect(self.drag_finished.emit)
        layout.addWidget(self._surface, 1)

        # Under the picture rather than painted on it, where it would sit on
        # top of the very widgets it explains how to move.
        self._edit_hint = QLabel(
            "Editing the overlay: drag a widget to move it.    Alt: no snapping"
            "    Shift: straight line    Arrow keys: nudge (with Shift: a grid"
            " square)    Esc: cancel a drag"
        )
        self._edit_hint.setStyleSheet("color: #2ecc71;")
        self._edit_hint.setWordWrap(True)
        self._edit_hint.hide()
        layout.addWidget(self._edit_hint)

        self._stats = QLabel("Not capturing.")
        self._stats.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self._stats)

    # --------------------------------------------------------------- devices

    def refresh_devices(self) -> None:
        """Enumerate on this thread (COM), then probe formats on a worker."""
        if self._recording:
            # Refreshing stops the camera and builds a new one, and the take's
            # encoder was left reading the old one: the picture came back
            # perfect while the file ended at the moment the button was
            # pressed, with the clock, the markers and the chapters all still
            # covering the whole take. The button is disabled during a take;
            # this is the guard for every other caller.
            log.info("Not re-enumerating cameras while recording")
            return

        wanted = self.current_device_name() or self.preferred_device_name
        self._devices = enumerate_video_devices()
        self._formats_by_index = {}

        self._device_box.blockSignals(True)
        self._device_box.clear()
        for device in self._devices:
            self._device_box.addItem(device.name, device)
        # By name, not index. Rebuilding the list in enumeration order landed
        # on device 0 every time, so Refresh silently reverted to the first
        # camera -- and a replug can move an index without moving a name.
        if wanted:
            found = self._device_box.findText(wanted)
            if found >= 0:
                self._device_box.setCurrentIndex(found)
        self._device_box.blockSignals(False)

        has_devices = bool(self._devices)
        if not has_devices:
            self._device_box.addItem("No camera found", None)
            self._format_box.clear()
            self._surface.clear_frame("No camera found")
            self._stats.setText(
                "No capture device found. Connect a camera and press Refresh."
            )
            # Nothing to probe is a look that has finished too -- unless an
            # earlier look is still asking ffmpeg about the cameras that were
            # there. That look's answer sends this (see _probe_finished), and
            # sending it now as well would let the audio check start while a
            # camera was still being queried.
            if not self._probing:
                self.formats_probed.emit()
            return

        self._format_box.clear()
        self._format_box.addItem("Checking supported formats...", None)
        self._probing = True
        self._prober.probe(self._devices)

    @property
    def is_probing(self) -> bool:
        """True while ffmpeg is listing the cameras' formats."""
        return self._probing

    def _probe_finished(self, formats: dict) -> None:
        """Fill in the format picker, then go live. Main thread, queued signal.

        FormatProbe sends only the newest look's answer, and only once nothing
        else is probing the cameras. That is what makes clearing _probing here
        true when Refresh was pressed during a look.
        """
        self._probing = False
        self._formats_by_index = formats
        self._device_changed()
        self._autostart()
        # After the automatic start, never before it. Once the hold has been
        # let go, the line above may be the camera opening, and the main window
        # starts the audio input check from this signal: sent first, that
        # check would be querying devices while this thread opened one.
        self.formats_probed.emit()

    def _autostart(self) -> None:
        """Go live on the camera found, unless the main window is holding it."""
        # The camera runs whenever the app is open, not only while recording.
        # Opening Wer and seeing black until you find a button is the wrong
        # first impression, and during a tech you want to confirm framing long
        # before you arm anything. Recording is a separate act entirely - see
        # CameraCapture.feed_encoder, which stays clear until a take starts, so
        # a live preview costs nothing but the capture thread.
        if (
            self.autostart
            and not self._autostarted
            and self._devices
            and not self._autostart_held
        ):
            self._autostarted = True
            self.start_capture()

    def release_autostart(self) -> None:
        """Let the automatic start at launch go ahead.

        At once if the formats are in. If a probe is still running -- the
        start-up wait ran out before it answered, or Refresh was pressed while
        the start was held -- _probe_finished starts the camera when the probe
        answers, as it would have with no hold at all.
        """
        self._autostart_held = False
        if not self._probing:
            self._autostart()

    def _device_changed(self) -> None:
        if self._recording:
            return
        device = self._device_box.currentData()
        if device is None:
            return

        was_running = self.capture is not None and self.capture.is_running
        if was_running:
            self.stop_capture()
        self._fill_formats(device)

        # Switching camera mid-session restarts capture on its own rather than
        # asking the user to stop and start anything -- there is deliberately
        # no camera Start button on this tab. It used to only stop: picking a
        # second camera killed the picture and left it dead, and the only route
        # back was Refresh, which reopened the FIRST device. Device 1 was never
        # opened at any point.
        if was_running or (self.autostart and self._autostarted):
            self.start_capture()

    def _fill_formats(self, device: CaptureDevice) -> None:
        """Rebuild the format list for a device, keeping the user's choice.

        Signals are blocked throughout: filling the box is not the user
        choosing a format, and treating it as one would restart the camera on
        every refresh.
        """
        self._formats = self._formats_by_index.get(device.index, [])
        self._format_box.blockSignals(True)
        try:
            self._format_box.clear()
            if not self._formats:
                # Probing can fail on devices that do not answer DirectShow's
                # enumeration. Offering common modes beats offering nothing.
                for width, height in ((1920, 1080), (1280, 720), (640, 480)):
                    fmt = VideoFormat(width, height, 30.0, 30.0, "MJPG")
                    self._format_box.addItem(f"{fmt} (not verified)", fmt)
                return
            for fmt in self._formats:
                self._format_box.addItem(str(fmt), fmt)

            # A format the user picked for THIS camera outranks best_format.
            # Re-guessing on every refresh is how a deliberate choice of MJPG
            # -- the tooltip's own advice for a camera stuck at a few frames
            # per second -- was thrown away by pressing Refresh.
            chosen = None
            if self._preferred_format_for == device.name:
                chosen = next(
                    (
                        fmt
                        for fmt in self._formats
                        if self._same_mode(fmt, self._preferred_format)
                    ),
                    None,
                )
            if chosen is None:
                chosen = best_format(self._formats)
            if chosen is not None:
                self._format_box.setCurrentIndex(self._formats.index(chosen))
        finally:
            self._format_box.blockSignals(False)

    @staticmethod
    def _same_mode(one: VideoFormat, other: VideoFormat | None) -> bool:
        """Same capture mode, ignoring the rate limits.

        The show file stores a single fps, while a probed format carries a
        min and a max, so the two never compare equal. Resolution and pixel
        format are what a choice of mode actually means.
        """
        if other is None:
            return False
        return (one.width, one.height, one.fourcc) == (
            other.width, other.height, other.fourcc
        )

    def _format_changed(self) -> None:
        """The user picked a format. Restart the camera on it.

        This box was connected to nothing at all: choosing a resolution or a
        pixel format changed the combo's text and never reached the camera, and
        the next refresh snapped the text back to whatever was really being
        captured.
        """
        if self._recording:
            return
        device = self._device_box.currentData()
        fmt: VideoFormat | None = self._format_box.currentData()
        if device is None or fmt is None:
            return
        self._preferred_format = fmt
        self._preferred_format_for = device.name
        if self.capture is None or not self.capture.is_running:
            return
        self.stop_capture()
        self.start_capture()

    def current_device_name(self) -> str:
        """The camera currently selected, for the show file."""
        device = self._device_box.currentData()
        return device.name if device is not None else ""

    def current_format(self) -> VideoFormat | None:
        """The format currently selected, for the show file."""
        return self._format_box.currentData()

    # --------------------------------------------------------------- picture

    @property
    def flip_horizontal(self) -> bool:
        """Mirror left to right is ticked. For the show file and every capture."""
        return self._mirror_box.isChecked()

    @property
    def flip_vertical(self) -> bool:
        """Flip top to bottom is ticked. For the show file and every capture."""
        return self._flip_box.isChecked()

    def _flips_toggled(self) -> None:
        """Turn the live picture now. Main thread, either box's toggled signal.

        The camera is not reopened. Opening one takes 3.6-4.1 s on the
        Integrated Camera, with no picture meanwhile, and there is no need: the
        capture thread reads its settings afresh for every frame
        (CameraCapture._orient), so replacing them is the whole change, and the
        next frame read is turned.

        Allowed while recording, unlike Device, Format and Refresh, which are
        locked because each one ends the take's video. A flip leaves the frame
        the size the take's ffmpeg was started on, so nothing in the file
        breaks, and a camera found to be upside down just after Record would
        otherwise stay that way for the whole take. Every change is logged, so
        a picture that turns over partway through a recording can be explained
        afterwards.
        """
        horizontal, vertical = self.flip_horizontal, self.flip_vertical
        capture = self.capture
        if capture is not None:
            capture.settings = replace(
                capture.settings, flip_horizontal=horizontal, flip_vertical=vertical
            )
        log.info(
            "Picture flips changed%s: now %s",
            " during a take" if self._recording else "",
            _describe_flips(horizontal, vertical),
        )
        self.flips_changed.emit()

    # --------------------------------------------------------------- capture

    def start_capture(self) -> None:
        device = self._device_box.currentData()
        fmt: VideoFormat | None = self._format_box.currentData()
        if device is None:
            return

        settings = CaptureSettings(
            device_index=device.index,
            device_name=device.name,
            # See CameraCapture.open: with more than one camera attached, a
            # device index only means what it says under DirectShow.
            device_count=len(self._devices),
            width=fmt.width if fmt else 1920,
            height=fmt.height if fmt else 1080,
            fps=fmt.fps_max if fmt else 30.0,
            fourcc=fmt.fourcc if fmt else None,
            flip_horizontal=self.flip_horizontal,
            flip_vertical=self.flip_vertical,
        )
        # open() blocks this thread while the driver negotiates -- 1.6 s on a
        # camera whose backend is already known, and longer on one being
        # discovered for the first time. Until now the surface went on saying
        # "No camera" for all of it, which is both wrong and the most alarming
        # thing it could say: the operator is looking at a black panel that
        # claims the camera is missing, at exactly the moment it is coming up.
        # repaint() rather than update(): update() only queues a paint, and
        # nothing returns to the event loop until open() comes back.
        self._surface.clear_frame(f"Starting {device.name}…")
        self._surface.repaint()

        capture = CameraCapture(settings, on_error=self._on_capture_error)
        if self.compositor is not None:
            capture.frame_processor = self.compositor.composite
        if not capture.open():
            # Genuinely not coming up. Now the panel may say so.
            self._surface.clear_frame(f"{device.name} would not open")
            QMessageBox.warning(
                self,
                "Could not open camera",
                f"{device.name} could not be opened.\n\n"
                "It may be in use by another application — Teams, Zoom and the "
                "Windows Camera app all hold a camera exclusively.",
            )
            return

        capture.start()
        self.capture = capture
        self._capture_device = device
        if self._recording:
            # A take is rolling and this camera replaced the one it started
            # on. Arm the new one, or the encoder is fed by nothing while the
            # preview shows a perfect picture.
            capture.feed_encoder.set()
        self._frame_timer.start()
        self.capture_changed.emit(True)
        self._update_record_button()
        # The picture as it starts, so the log says which way up a take began,
        # not only what was changed afterwards.
        log.info(
            "Preview started on %s, picture %s",
            device.name, _describe_flips(self.flip_horizontal, self.flip_vertical),
        )

    def stop_capture(self) -> None:
        self._frame_timer.stop()
        self._last_frame = None
        if self.capture is not None:
            self.capture.stop()
            self.capture = None
        self._capture_device = None
        self._lost = None
        self._surface.clear_frame("Stopped")
        self.capture_changed.emit(False)
        self._update_record_button()

    # --------------------------------------------------------------- dropouts

    def _check_camera_still_delivering(self) -> None:
        """Reopen a camera that has dropped out of a take. Main thread, 1 s timer.

        Nothing used to. A camera whose device dropped off its connection went
        on being read through the same OpenCV handle for the rest of the take,
        and every control that could have reopened it -- Device, Format,
        Refresh -- is locked while recording, for the reason set_recording
        gives. The take stayed open, markers and chapters kept landing, and the
        video after the drop existed only if that handle started delivering
        again. Whether a DirectShow handle does, once its device has gone and
        come back, has never been measured here, so this does not wait to find
        out.

        Everything that touches DirectShow stays on this thread, where it
        already was: enumeration has to (see FormatProbe), and opening and
        releasing a camera have only ever been done here. The old capture is
        stopped, and must really have let go, before the device is opened
        again. The new one goes into self.capture armed for the take, and that
        is the attribute the encoder pump re-reads on every pass.

        Only during a take. Outside one the operator is here and Refresh works,
        and a camera another application has just taken is theirs to keep.
        What counts as lost is CameraCapture.stalled_for's call, and it never
        counts a black picture: the Blackmagic without a signal must not be
        cycled through reopens that cannot give it one.
        """
        # Never inside an attempt, either. An attempt enumerates through COM and
        # opens through DirectShow on this thread, and either may pump this
        # thread's messages while it waits: COM can in a single-threaded
        # apartment, and whether these calls do has not been checked. A tick
        # delivered in there would start a second attempt on the same device
        # halfway through the first -- two opens, and the outer attempt then
        # stopping the camera the inner one had just armed. Qt may already
        # refuse to re-enter a timer's slot; nothing here relies on that.
        if not self._recording or self._reopening:
            return
        now = time.perf_counter()
        lost = self._lost
        if lost is None:
            capture, device = self.capture, self._capture_device
            if capture is None or device is None:
                return
            stalled = capture.stalled_for
            # A capture with no thread left cannot deliver, whatever stalled_for
            # says, and waiting on it is waiting for nothing. stalled_for once
            # read 0.0 on exactly that (see CameraCapture._run), so the thread
            # is looked at here too rather than trusted to it.
            ended = not capture.is_running
            if stalled < REOPEN_AFTER and not ended:
                return
            gap, first_attempt = REOPEN_FIRST_GAP, now
            if self._reopened_at and now - self._reopened_at < REOPEN_HELD_FOR:
                # Failed again soon after it came back: wait, and longer than
                # last time, rather than cycling the device.
                gap = min(self._reopen_gap * 2, REOPEN_MAX_GAP)
                first_attempt = now + gap
            lost = self._lost = _LostCamera(
                device=device,
                settings=capture.settings,
                resolution=capture.actual_resolution,
                since=now - stalled,
                namesakes=sum(1 for known in self._devices if known.name == device.name),
                next_attempt=first_attempt,
                gap=gap,
            )
            log.error(
                "%s %s during the take; closing it and opening it again in %.0f s",
                device.name,
                f"has delivered nothing for {stalled:.0f} s"
                if stalled >= REOPEN_AFTER
                else "has no capture thread left",
                first_attempt - now,
            )
            self._surface.clear_frame(
                f"{device.name} stopped delivering. Reopening it; the gap will be "
                "in the recording."
            )
        elif (
            self.capture is not None
            and self.capture.is_running
            and self.capture.stalled_for == 0.0
        ):
            # Back on the handle it had, before an attempt got to it. Running is
            # part of that. An attempt that found the old capture stuck in a
            # read leaves it here; when the read came back with a frame, its
            # thread ended with stalled_for at 0.0, this said the camera was
            # delivering again, and nothing looked for it for the rest of the
            # take.
            log.warning(
                "%s is delivering again by itself after %.0f s",
                lost.device.name, now - lost.since,
            )
            self._lost = None
            return

        if now >= lost.next_attempt and not self._probing:
            self._reopening = True
            try:
                self._reopen(lost)
            finally:
                self._reopening = False

    def _reopen(self, lost: _LostCamera) -> None:
        """One attempt at bringing a lost camera back. See the method above.

        Nothing may escape the attempt. It runs in a timer slot, and an
        exception out of one went past _reopen_later, so the backoff never
        moved: every one-second tick enumerated, opened on this thread for up
        to four seconds and logged a crash traceback, for the rest of the take.
        open() calls into OpenCV's native backends, whose way of reporting
        trouble is to raise cv2.error, and enumeration goes through COM.
        Neither is known never to raise, so both are guarded here.
        """
        lost.attempts += 1
        name = lost.device.name
        width, height = lost.resolution
        capture: CameraCapture | None = None
        try:
            devices = enumerate_video_devices()
            matches = [device for device in devices if device.name == name]
            if not matches:
                self._reopen_later(lost, "it is not connected", logging.INFO)
                return
            if len(matches) > 1 or lost.namesakes > 1:
                # The name is all there is to go on, and nothing says DirectShow
                # numbers two identical devices the same way round when one
                # comes back. Opening the other one would record the wrong
                # camera for the rest of the take, which looks fine and is not.
                self._reopen_later(
                    lost,
                    f"more than one camera is called {name}, and nothing says "
                    "which one this take was using",
                    logging.ERROR,
                )
                return
            device = matches[0]

            old = self.capture
            if old is not None:
                if not old.stop():
                    self._reopen_later(
                        lost,
                        "the old capture is still inside a read and has not let "
                        "go of the device",
                        logging.ERROR,
                    )
                    return
                # Stopped, but left as self.capture until a replacement is
                # armed. The main window reads the take's frame rate from
                # preview.capture when the take ends, and numbers every marker
                # in the markers file by it. With None here it fell back to
                # 30, so a take at any other rate whose camera never came back
                # got a markers file at the wrong rate, and nothing said so. A
                # stopped capture is not running, so Record and Snapshot still
                # say there is no camera, and stopping it again is a no-op.
                self._frame_timer.stop()
                self._last_frame = None
                self._update_record_button()

            # Asked for the size the take is being written at, not the one
            # picked in the Format box. Cameras accept a mode and deliver
            # another; the take was started on what was delivered, and that is
            # the only size its pipe can carry. Asked for the picked mode, a
            # camera that honoured it this time would be refused below at every
            # attempt for the rest of the take.
            # And with the flips ticked now, not those it had when it was lost:
            # a box ticked while it was being looked for would otherwise show
            # ticked over a picture that came back the other way up.
            capture = CameraCapture(
                replace(
                    lost.settings, device_index=device.index,
                    device_name=device.name, device_count=len(self._devices),
                    width=width,
                    height=height, flip_horizontal=self.flip_horizontal,
                    flip_vertical=self.flip_vertical,
                ),
                on_error=self._on_capture_error,
            )
            if self.compositor is not None:
                capture.frame_processor = self.compositor.composite
            if not capture.open():
                self._reopen_later(
                    lost, "it is connected but would not open", logging.WARNING
                )
                return
            if capture.actual_resolution != lost.resolution:
                # The recorder writes each frame's bytes down a rawvideo pipe
                # sized when the take started, and checks nothing. A frame of
                # another size misaligns that one and every frame after it, so
                # the rest of the take would be sheared noise under a log line
                # saying the camera had been reopened. No picture is the honest
                # failure; this one is not armed. A device can come back in
                # another mode after a replug, and open() can settle on the
                # other backend, so this is not assumed away.
                got_width, got_height = capture.actual_resolution
                capture.stop()
                self._reopen_later(
                    lost,
                    f"it came back delivering {got_width}x{got_height}, and the "
                    f"take is being written at {width}x{height}",
                    logging.ERROR,
                )
                return
            capture.start()
        except Exception:  # noqa: BLE001 - a timer slot; see the docstring
            log.exception("Reopening %s raised (attempt %d)", name, lost.attempts)
            if capture is not None:
                try:
                    capture.stop()
                except Exception:  # noqa: BLE001
                    log.exception("Letting go of %s after that raised too", name)
            self._reopen_later(
                lost, "the attempt raised; the traceback is above", logging.ERROR
            )
            return

        self.capture = capture
        self._capture_device = device
        if self._recording:
            capture.feed_encoder.set()
        self._frame_timer.start()
        if devices != self._devices:
            self._adopt_devices(devices, device)
        self._reopened_at, self._reopen_gap = time.perf_counter(), lost.gap
        self._lost = None
        log.warning(
            "%s reopened as DirectShow device %d after %.0f s without frames "
            "(attempt %d); the gap is in the recording",
            name, device.index, time.perf_counter() - lost.since, lost.attempts,
        )
        self.capture_changed.emit(True)
        self._update_record_button()

    def _reopen_later(self, lost: _LostCamera, reason: str, level: int) -> None:
        lost.next_attempt = time.perf_counter() + lost.gap
        log.log(
            level,
            "Could not reopen %s (attempt %d): %s. Trying again in %.0f s.",
            lost.device.name, lost.attempts, reason, lost.gap,
        )
        lost.gap = min(lost.gap * 2, REOPEN_MAX_GAP)

    def _adopt_devices(self, devices: list[CaptureDevice], current: CaptureDevice) -> None:
        """Put the device list a reopen found into the picker.

        A camera can come back under a different DirectShow index, and the
        picker was still holding the old numbers. Left like that, choosing a
        camera after the take would open whatever now sat at an old index --
        the wrong camera, without a word, which is exactly what refresh_devices
        goes out of its way to prevent. Formats already probed are carried
        across by name rather than probed again in the middle of a take.
        """
        formats_by_name = {
            known.name: self._formats_by_index[known.index]
            for known in self._devices
            if known.index in self._formats_by_index
        }
        self._devices = list(devices)
        self._formats_by_index = {
            device.index: formats_by_name[device.name]
            for device in devices
            if device.name in formats_by_name
        }
        self._device_box.blockSignals(True)
        try:
            self._device_box.clear()
            for device in devices:
                self._device_box.addItem(device.name, device)
            self._device_box.setCurrentIndex(devices.index(current))
        finally:
            self._device_box.blockSignals(False)

    def current_frame(self):
        """The frame currently on screen, or None.

        Copied, because the capture thread reuses its arrays and a snapshot
        that gets encoded a moment later would otherwise be a different frame,
        or half of two.
        """
        frame = self._last_frame
        return None if frame is None else frame.copy()

    def set_edit_mode(self, enabled: bool) -> None:
        self._surface.set_edit_mode(enabled)
        self._edit_hint.setVisible(enabled)

    def highlight(self, widget_id: str) -> None:
        self._surface.set_selected(widget_id)

    def set_snap_feedback(self, guides, anchor_label: str) -> None:
        """The guides a dragged widget is on, from the editor, to draw."""
        self._surface.set_snap_feedback(guides, anchor_label)

    def set_grid(self, columns: int, rows: int) -> None:
        """The grid drawn while widgets move, from the editor's setting."""
        self._surface.set_grid(columns, rows)

    def set_recording(self, recording: bool) -> None:
        """Told by the main window when a take starts or ends.

        The camera controls are locked for the duration. Every one of them
        stops the capture the take is being fed from, and the take does not
        notice: the recorder stays in RECORDING, the clock keeps counting,
        markers keep landing and the file simply ends at the moment the control
        was touched. Measured: a 17.6 s take with Refresh pressed at 5.6 s
        produced a 5.1 s video carrying a 17.9 s chapter.

        Mirror left to right and Flip top to bottom are not locked: they turn
        the running capture's picture without stopping it (_flips_toggled).
        """
        self._recording = recording
        if not recording and self._lost is not None:
            # The reopen only runs during a take. Past this point the operator
            # is back and Refresh works, but they have to be told it is needed.
            name = self._lost.device.name
            log.error("The take ended with %s still lost; press Refresh to look for it", name)
            self._surface.clear_frame(
                f"{name} was lost during the take. Press Refresh to look for it."
            )
            self._lost = None
        for control in (self._device_box, self._format_box, self._refresh_button):
            control.setEnabled(not recording)
        locked = (
            "Locked while recording: changing the camera ends the take's video."
            if recording else ""
        )
        self._device_box.setToolTip(locked)
        self._refresh_button.setToolTip(locked or "Look for cameras again.")
        self._update_record_button()

    def _update_record_button(self) -> None:
        live = self.capture is not None and self.capture.is_running
        if self._recording:
            self._record_button.setText("■ Stop Recording")
            self._record_button.setStyleSheet(
                "background-color: #c0392b; color: white;"
            )
        else:
            self._record_button.setText("● Record")
            self._record_button.setStyleSheet("")
        # Nothing to record without a camera, but stopping must stay available.
        self._record_button.setEnabled(live or self._recording)
        self._snapshot_button.setEnabled(live)

    def _on_capture_error(self, message: str) -> None:
        """Called from the capture thread. Only touch Qt via a queued signal.

        This used to defer through QTimer.singleShot, which looks like the same
        thing and is not: a QTimer created on a thread with no event loop never
        fires. The message never arrived, so an unplugged camera left the last
        good frame frozen on screen with no banner and nothing said. A signal
        emitted across threads is queued to the receiver's own thread, which is
        the hand-off Qt actually guarantees.
        """
        log.error("Capture error: %s", message)
        self.capture_failed.emit(message)

    def _show_capture_error(self, message: str) -> None:
        """Main thread, via the queued signal above."""
        self._surface.clear_frame(message)

    def _pull_frame(self) -> None:
        if self.capture is None:
            return
        frame: Frame | None = None
        # Drain to the newest frame: showing a backlog one at a time would let
        # the preview fall progressively further behind the live feed.
        while True:
            try:
                frame = self.capture.preview_queue.get_nowait()
            except queue.Empty:
                break
        if frame is None:
            return

        # Frames arrive with the overlay already drawn: the compositor runs
        # once in the capture thread (CameraCapture.frame_processor), so the
        # preview shows exactly what is being recorded.
        self._last_frame = frame.image
        self._surface.show_frame(frame.image)

        if self._surface.edit_mode and self.compositor is not None:
            self._report_widget_rects(frame.image.shape[1], frame.image.shape[0])

    def _report_widget_rects(self, width: int, height: int) -> None:
        """Tell the surface where each widget is, so it can be clicked.

        Recomputed per frame while editing, because a widget's size changes
        with its content -- a command line grows as you type -- and a hit box
        that lags the picture is worse than none.
        """
        rects: dict[str, tuple[int, int, int, int]] = {}
        for widget in self.compositor.widgets:
            rect = widget.rect(self.compositor.bus, width, height)
            if rect is not None and not rect.is_empty:
                rects[widget.id] = (rect.x, rect.y, rect.width, rect.height)
        self._surface.set_widget_rects(rects, (width, height))

    def _refresh_stats(self) -> None:
        lost = self._lost
        if lost is not None:
            # A reopen is under way, and neither the old capture's frozen
            # numbers nor "Not capturing." once it has been let go would say so.
            self._stats.setText(
                f"CAMERA LOST {time.perf_counter() - lost.since:.0f} s ago: "
                f"reopening {lost.device.name} ({lost.attempts} attempt(s) so far)"
            )
            return
        if self.capture is None:
            self._stats.setText("Not capturing.")
            return
        stats = self.capture.stats
        width, height = self.capture.actual_resolution
        fps = stats.measured_fps
        starting = (
            stats.last_frame_at == 0.0
            and time.perf_counter() - stats.started_at < LIVE_WINDOW
        )
        if starting:
            # Not the same thing as a dead camera, and it must not be said the
            # same way. is_live is False until the FIRST frame lands, so every
            # app launch and every restart after a device or format change used
            # to read "NO SIGNAL (no frames yet)" -- the stats timer is 500 ms,
            # so at least one refresh always showed it. An alarm that cries
            # wolf on every launch is an alarm the operator learns to ignore,
            # and this one is the tell for a stage camera that has been
            # unplugged mid-tech.
            #
            # Measured on the real camera here: open() takes 3.6-4.1 s, because
            # it probes both backends for the delivered rate, and start() to
            # frame one is 32-35 ms -- one frame period. So the honest wait is
            # short, and LIVE_WINDOW is generous cover for a slower device
            # without ever masking a camera that is genuinely not coming up.
            bits = [
                "Starting…",
                f"{width}x{height}",
                f"{self.capture.backend} backend",
            ]
        elif not stats.is_live:
            # Lead with it. This line used to go on reporting the last healthy
            # rate and a raw bitrate derived from it -- "29.7 fps measured, 184
            # MB/s raw" on a camera that had been dead for ten seconds. The
            # frame count quietly ceasing to advance was the only honest tell.
            since = (
                f"no frames for {time.perf_counter() - stats.last_frame_at:.0f} s"
                if stats.last_frame_at
                else "no frames yet"
            )
            bits = [
                f"NO SIGNAL ({since})",
                f"{width}x{height}",
                f"{self.capture.backend} backend",
                f"{stats.frames_captured} frames",
            ]
        else:
            bits = [
                f"{width}x{height}",
                f"{fps:.1f} fps measured",
                f"{self.capture.backend} backend",
                f"{stats.frames_captured} frames",
                f"{raw_bitrate_mbps(width, height, fps):.0f} MB/s raw",
            ]
        if stats.frames_dropped_preview:
            bits.append(f"{stats.frames_dropped_preview} preview drops")
        if stats.frames_dropped_encoder:
            bits.append(f"{stats.frames_dropped_encoder} ENCODER DROPS")
        if stats.read_failures:
            bits.append(f"{stats.read_failures} read failures")
        self._stats.setText("   ".join(bits))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.stop_capture()
        super().closeEvent(event)
