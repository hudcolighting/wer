"""A small picture of the layout, beside the settings that change it.

The Overlay tab edits widgets; the Preview tab shows them. Making a widget
meant changing a size, going to look, coming back, changing it again -- and the
whole argument for editing live (see layout_editor) is that you judge a style
by looking at it. So the settings get a picture of their own: the layout drawn
on the sample stage, with the selected widget outlined, updated as you type.

It is deliberately NOT the live picture. Everything here draws on its own
Compositor over its own :func:`wer.overlay.sample.sample_bus`, fed deep copies
of the editor's widgets. Three things follow from that, all of them the point:

* a preview can never put a made-up cue number in front of a recording;
* rendering here cannot touch the live compositor's caches or its bus
  subscriptions, so drawing a small copy cannot freeze the widget that is being
  recorded;
* a layout built at a desk in the afternoon, with no console and no camera,
  still shows text of a believable length over something bright.

The caption under it says so, because a picture that looks live and is not is
worse than no picture at all.

Main thread only.
"""

from __future__ import annotations

import logging

import numpy as np
from PySide6.QtCore import QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from wer.overlay.compositor import Compositor
from wer.overlay.geometry import Rect
from wer.overlay.layout import widget_from_dict, widget_to_dict
from wer.overlay.sample import sample_bus, sample_frame
from wer.overlay.widgets import ImageWidget, OverlayWidget

log = logging.getLogger(__name__)

__all__ = ["LayoutPreview", "frame_to_image", "RENDER_SIZE", "SELECTION_COLOUR"]

#: What the picture is drawn at, before it is scaled onto the screen: a third
#: of 1080p, 16:9. Every size in a layout is a fraction of the frame, so this is
#: the same arrangement a recording gets -- text that is cut off here is cut off
#: there. Smaller than the preset dialog's thumbnail because this one is redrawn
#: while someone types.
RENDER_SIZE = (640, 360)

#: The largest the picture is shown at, in logical pixels, and 16:9 exactly.
#: Kept small on purpose: the Overlay tab has to fit a laptop screen with the
#: rest of the property sheet under it (see tests/test_window_height.py).
MAX_PICTURE = QSize(352, 198)

#: And the smallest, so the widget does not collapse to a sliver in a narrow
#: window and leave the caption describing nothing.
MIN_PICTURE_WIDTH = 160

#: The same green the Preview tab outlines a selected widget with
#: (wer.ui.preview), so the two pictures agree about what is selected.
SELECTION_COLOUR = "#27ae60"

#: How long changes are gathered up before the picture is redrawn. Long enough
#: that typing a template does not render per keystroke, short enough that it
#: feels like it answers.
RENDER_DELAY_MS = 80

_CAPTION_TEXT = "Sample data — the Preview tab shows the live picture"
_CAPTION_STYLE = "color:#9aa0aa; font-style: italic;"


def frame_to_image(frame: np.ndarray) -> QImage:
    """A BGR frame as a QImage.

    Copied before the array goes: QImage does not take ownership of the buffer
    it is given, so without the copy the image would be drawn from memory numpy
    has already handed back.
    """
    height, width = frame.shape[:2]
    return QImage(
        frame.data, width, height, frame.strides[0], QImage.Format.Format_BGR888
    ).copy()


def _carry_decoded_file(old: OverlayWidget, new: OverlayWidget) -> None:
    """Hand a rebuilt picture widget the file the copy it replaces had read.

    Keeping a copy until its settings change is only half the saving, because
    moving a widget IS a change: a drag on the preview is one every mouse
    move, and so is a held arrow key or a spinner. Measured on a twenty-step
    drag of a watermark -- twenty rebuilds, the logo decoded off the disk
    twenty times, and for a logo that has gone missing twenty
    "Watermark ...: No file at ..." warnings, in the log beside the exe that
    gets read after a bad take.

    What changed was where the picture sits, not which picture it is, so the
    new copy starts where the old one got to: the decoded image, and what is
    remembered about a file that would not load -- when to try it again, and
    that it has already been complained about. Guarded on the path, so
    pointing the widget at a different file still goes to the disk.

    This does reach into ImageWidget's own attributes. The alternative is a
    public "take over this cache" on the widget itself, which is a wider thing
    for every widget to carry for the sake of one small preview.
    """
    if not isinstance(old, ImageWidget) or not isinstance(new, ImageWidget):
        return
    if old._source_path != new.path:
        return
    new._source = old._source
    new._source_path = old._source_path
    new.problem = old.problem
    new._reported = old._reported
    new._retry_at = old._retry_at
    new._retry_delay = old._retry_delay


def _draw_outline(image: QImage, rect: Rect) -> None:
    """Ring the selected widget, the way the Preview tab does."""
    painter = QPainter(image)
    try:
        # Off: a crisp two-pixel ring is easier to see at this size than a
        # smeared one, and it keeps the outline exactly where the widget is.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        pen = QPen(QColor(SELECTION_COLOUR))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRect(rect.x, rect.y, rect.width - 1, rect.height - 1))
    finally:
        painter.end()


class _StagePicture(QWidget):
    """Just the picture: a pixmap kept at 16:9, however wide it is given."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self.setMaximumWidth(MAX_PICTURE.width())
        self.setMinimumWidth(MIN_PICTURE_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._fit_height()

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        """Asked for, because a plain QWidget has no opinion about its size.

        Without one the box layout gave the picture whatever it had lying
        around -- a stage two thirds the size it is allowed, with the caption
        running out well past the edge of it.
        """
        return QSize(MAX_PICTURE)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(MIN_PICTURE_WIDTH, round(MIN_PICTURE_WIDTH * 9 / 16))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self) -> None:
        """Hold the box to 16:9, so the stage is never stretched or letterboxed.

        A fixed height rather than heightForWidth: this sits in a plain column
        with a scroll area under it, and a fixed height is the one thing every
        layout in that chain agrees about. Width does not depend on height
        here, so setting it from inside resizeEvent settles after one pass.
        """
        wanted = min(MAX_PICTURE.height(), round(self.width() * 9 / 16))
        if self.height() != wanted or self.minimumHeight() != wanted:
            self.setFixedHeight(wanted)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        area = self.rect()
        if self._pixmap is None:
            # Not left blank: an empty box beside a caption about sample data
            # reads as a preview that has failed, rather than one still to draw.
            painter.fillRect(area, QColor("#111"))
            return
        # Scaled at the screen's own pixel density and told about it, so the
        # picture stays sharp on a high-DPI laptop instead of being blown up.
        ratio = self.devicePixelRatioF()
        scaled = self._pixmap.scaled(
            QSize(area.width(), area.height()) * ratio,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(ratio)
        x = (area.width() - scaled.width() / ratio) / 2
        y = (area.height() - scaled.height() / ratio) / 2
        painter.drawPixmap(int(x), int(y), scaled)


class LayoutPreview(QWidget):
    """The layout on the sample stage, with the selected widget outlined."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        #: Where the widgets to draw come from. Read, copied, never touched.
        self._source: Compositor | None = None
        #: Ours alone, over a bus of its own. See the module docstring.
        self._compositor = Compositor(sample_bus())
        self._selected = ""
        self._image: QImage | None = None
        self._rect: Rect | None = None
        #: The widgets actually being drawn, by id, each with the serialised
        #: form it was built from. Kept between renders; see _sync_copies.
        self._copies: dict[str, tuple[dict, OverlayWidget]] = {}
        #: A render is wanted. Stays set while the tab is away, so coming back
        #: to it draws what changed rather than what was last on screen.
        self._pending = False

        self._picture = _StagePicture()
        self._caption = QLabel(_CAPTION_TEXT)
        self._caption.setStyleSheet(_CAPTION_STYLE)
        self._caption.setWordWrap(True)
        # Room for a second line, and not capped to the picture's width. A
        # word-wrapped label asks for the height of ONE line however many it
        # will actually need, so left to itself the caption was allotted one
        # line and drew its second over the top of the property sheet.
        self._caption.setMinimumHeight(self._caption.fontMetrics().lineSpacing() * 2)
        self._caption.setToolTip(
            "A stand-in show on a stand-in stage, so a layout can be built with "
            "no console and no camera. The console's own values are on the "
            "Preview tab."
        )

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addWidget(self._picture, 0, Qt.AlignmentFlag.AlignLeft)
        # No alignment flag on the caption: it takes the whole width of the
        # column, which is where it fits on one line.
        column.addWidget(self._caption)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(RENDER_DELAY_MS)
        self._timer.timeout.connect(self.render_now)

    # ------------------------------------------------------------------- API

    def set_source(self, compositor: Compositor | None) -> None:
        """The compositor whose widgets this draws a copy of."""
        self._source = compositor
        self.request_render()

    def set_selected(self, widget_id: str) -> None:
        """Outline this widget, or nothing at all for an empty id."""
        widget_id = widget_id or ""
        if widget_id == self._selected:
            return
        self._selected = widget_id
        self.request_render()

    def request_render(self) -> None:
        """Redraw soon, gathering up whatever else changes before then."""
        self._pending = True
        if not self.isVisible():
            # Nothing to draw on, and nobody to see it. The Overlay tab is one
            # page of a tab widget, so this is the usual case: while the
            # operator is on Preview or Recording, a preview that redrew on
            # every bus change would be pure cost during a take.
            return
        # Deliberately not a restart. A restart on every change means a fast
        # typist can hold the picture off indefinitely, which is exactly when
        # they most want to see it; this way the burst is still coalesced, but
        # something is drawn within RENDER_DELAY_MS of its first change.
        if not self._timer.isActive():
            self._timer.start()

    def render_now(self) -> None:
        """Draw it this instant, debounce or no debounce."""
        self._timer.stop()
        self._pending = False
        if self._source is None:
            return
        try:
            self._render()
        except Exception:  # noqa: BLE001 - a preview must never take the tab down
            log.exception("The overlay preview failed to draw")

    @property
    def render_pending(self) -> bool:
        """True while a requested redraw has not happened yet."""
        return self._pending

    def rendered_image(self) -> QImage | None:
        """The picture as drawn, at RENDER_SIZE, outline and all. None before
        the first render."""
        return self._image

    def selection_rect(self) -> Rect | None:
        """Where the outline was drawn, in RENDER_SIZE pixels, or None."""
        return self._rect

    # ------------------------------------------------------------- internals

    def _sync_copies(self, widgets: list[OverlayWidget]) -> None:
        """Bring the preview's own widgets into line with the editor's.

        Copies, never the editor's objects. Sharing them would mean this small
        picture marking the recorded widget dirty and resizing its cached image
        to 640x360 -- the overlay on the recording redrawn at a third of the
        size it is meant to be, every 80 ms, in the middle of a take.

        Kept between renders rather than rebuilt each time, because a copy
        carries more than its settings. A picture widget holds the decoded
        file: rebuilt on every render, it read the logo off the disk twelve
        times a second while someone typed, and a logo that had gone missing
        wrote its warning into the log beside the exe just as often -- the log
        that gets read after a bad take. So a widget is rebuilt only when the
        thing it was built from has actually changed -- and when it is rebuilt
        anyway, because moving a widget changes it, the file it had already
        read comes across with it. See _carry_decoded_file.
        """
        kept: dict[str, tuple[dict, OverlayWidget]] = {}
        rebuilt = False
        for widget in widgets:
            try:
                data = widget_to_dict(widget)
            except TypeError:
                # A kind the serialiser cannot write. It cannot be saved
                # either, so this is not the place to complain about it.
                continue
            known = self._copies.get(widget.id)
            if known is not None and known[0] == data:
                kept[widget.id] = known
                continue
            copy = widget_from_dict(data)
            if copy is None:
                continue
            if known is not None:
                _carry_decoded_file(known[1], copy)
            kept[widget.id] = (data, copy)
            rebuilt = True
        # The comparison catches a widget removed or reordered, which changes
        # nothing about the widgets that are left but changes the picture.
        if rebuilt or list(kept) != list(self._copies):
            self._copies = kept
            self._compositor.clear()
            for _data, copy in kept.values():
                self._compositor.add(copy)

    def _render(self) -> None:
        width, height = RENDER_SIZE
        self._sync_copies(self._source.widgets)
        for _data, copy in self._copies.values():
            # The sample bus never changes, so nothing here is ever marked
            # dirty by a key. Saying it has changed on each render keeps the
            # picture the same picture every time: a cue block left alone would
            # otherwise go on interpolating its fade bar from the sample's 62%
            # up to full, and a preview that drifts is one you cannot compare
            # a style against.
            copy.notify_changed()

        frame = sample_frame(width, height)
        self._compositor.composite(frame, in_place=True)
        image = frame_to_image(frame)

        self._rect = self._outline_rect(width, height)
        if self._rect is not None:
            _draw_outline(image, self._rect)
        self._image = image
        self._picture.set_pixmap(QPixmap.fromImage(image))

    def _outline_rect(self, width: int, height: int) -> Rect | None:
        """Where the selected widget landed, or None if it drew nothing.

        Asked of the copy, not the live widget: rendering the live one here
        would resize the cached image the capture thread is blending.
        """
        if not self._selected:
            return None
        widget = self._compositor.get(self._selected)
        if widget is None:
            return None
        rect = widget.rect(self._compositor.bus, width, height)
        if rect is None:
            return None
        clipped = rect.clipped_to(width, height)
        return None if clipped.is_empty else clipped

    # ---------------------------------------------------------------- events

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if self._pending or self._image is None:
            # At once, not on the timer: coming back to the tab after changing
            # a layout elsewhere should not show the old picture first.
            self.render_now()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().hideEvent(event)
        self._timer.stop()
