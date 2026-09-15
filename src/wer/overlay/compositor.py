"""Composites overlay widgets onto captured frames.

Blending strategy
-----------------
The obvious implementation renders every widget into one full-frame RGBA image
and alpha-blends that over the picture. It is also the wrong one at 4K.

A full-frame blend costs the whole frame every frame regardless of how much is
actually drawn: at 3840x2160 that is 8.3 million pixels of multiply-add, thirty
times a second, to composite a cue number occupying maybe 2% of the picture.

So this blends **per widget rectangle**. Cost scales with the area the widgets
actually cover, not with the frame:

    1080p, five widgets covering ~6% of frame   ~0.12 Mpx per frame
    4K,    same layout                          ~0.50 Mpx per frame
    4K,    naive full-frame blend               8.29 Mpx per frame

That is the difference between a Blackmagic 4K feed being comfortable and being
marginal, and it is why the decision was made before the widgets were written
rather than retrofitted afterwards.

Widgets re-render only when a bound bus key changes or they are mid-animation.
The blend still happens every frame -- it has to, the picture underneath moves --
but it is blending cached images.
"""

from __future__ import annotations

import logging
import threading

import cv2
import numpy as np
from PySide6.QtGui import QImage

from wer.core.databus import BusEntry, DataBus
from wer.overlay.widgets import OverlayWidget

log = logging.getLogger(__name__)

__all__ = ["Compositor"]


class Compositor:
    """Holds a layout's widgets and draws them onto frames.

    Not a QObject and not thread-affine: :meth:`composite` is called from
    whichever thread owns the frame. Widget rendering uses QPainter on a QImage,
    which is safe off the main thread -- Qt permits painting to a QImage from any
    thread; only QWidget painting is restricted.
    """

    def __init__(self, bus: DataBus) -> None:
        self.bus = bus
        self._widgets: list[OverlayWidget] = []
        self._lock = threading.RLock()
        #: Bus subscriptions per widget id, so one widget's bindings can be
        #: rebuilt when its content changes without disturbing anyone else's.
        self._subscriptions: dict[str, list] = {}
        #: Premultiplied blend layers, keyed by widget id. Rebuilt only when a
        #: widget's cached image is replaced, which is what makes the per-frame
        #: cost just the blend itself.
        self._layers: dict[str, _BlendLayers] = {}
        #: Geometry from the last composite, for the editor's snap and clamp.
        self._frame_size: tuple[int, int] | None = None
        self._widget_sizes: dict[str, tuple[int, int]] = {}
        #: The bus's removal count as of the last composite; see composite.
        self._removals_seen = bus.removals
        self._enabled = True
        #: What the "panic-hide overlay" hotkey sets.
        self._panic_hidden = False

    # ---------------------------------------------------------------- widgets

    def add(self, widget: OverlayWidget) -> OverlayWidget:
        """Put a widget in the layout, or replace the one already using its id.

        Idempotent on purpose. The editor calls this to force a re-sort after
        changing a z-order, and while it simply appended, every "Bring
        forward", "Send back" or Layer change left another copy in the layout:
        blended once per copy, so a translucent box drew twice as dark, and
        saved into the show file as a second widget on the next autosave.
        """
        with self._lock:
            existing = next(
                (w for w in self._widgets if w is widget or w.id == widget.id), None
            )
            if existing is None:
                self._widgets.append(widget)
            elif existing is not widget:
                self._widgets = [widget if w is existing else w for w in self._widgets]
                # Layers are keyed by id, and this id now means a different
                # widget; the stale one would be blended until its next render.
                self._layers.pop(widget.id, None)
            self._widgets.sort(key=lambda w: w.z_order)
        self._subscribe(widget)
        return widget

    def resort(self) -> None:
        """Re-order by z after someone has changed a widget's z_order."""
        with self._lock:
            self._widgets.sort(key=lambda w: w.z_order)

    def remove(self, widget_id: str) -> None:
        with self._lock:
            self._widgets = [w for w in self._widgets if w.id != widget_id]
            self._layers.pop(widget_id, None)
        for subscription in self._subscriptions.pop(widget_id, ()):
            subscription.cancel()

    def get(self, widget_id: str) -> OverlayWidget | None:
        with self._lock:
            return next((w for w in self._widgets if w.id == widget_id), None)

    @property
    def widgets(self) -> list[OverlayWidget]:
        with self._lock:
            return list(self._widgets)

    def clear(self) -> None:
        with self._lock:
            self._widgets.clear()
            self._layers.clear()
        self._cancel_subscriptions()

    def apply_layout(self, widgets: list[OverlayWidget]) -> None:
        """Swap the whole widget set at once (hot-switching).

        One call rather than a remove-then-add sequence, because this happens
        on a hotkey mid-recording and the capture thread may be compositing at
        the same moment. Swapping under the lock means a frame sees the old set
        or the new one, never half of each.

        Duplicate ids are dropped on the way in, keeping the first. Everything
        here is keyed by widget id -- subscriptions, blend layers, last drawn
        size -- so two widgets sharing one id cannot both be tracked: only the
        last to subscribe stays bound to the bus, and the other is never marked
        dirty again. It goes on blending whatever it happened to render first,
        underneath the live copy: a cue number from an hour ago sitting under
        the current one, in every frame, with nothing to say so.

        The precondition is not hypothetical. Show files autosaved before the
        "Bring forward adds a second copy" fix contain exactly this, and
        Layout.from_dict rebuilds them as two separate widget objects. Dropping
        the duplicate here repairs those files rather than freezing half of
        them; add() already refuses to hold two widgets with one id.
        """
        self._cancel_subscriptions()

        unique: list[OverlayWidget] = []
        seen: set[str] = set()
        for widget in widgets:
            if widget.id in seen:
                log.warning(
                    "Layout contains a second widget with id %r; keeping the "
                    "first and dropping the copy", widget.id
                )
                continue
            seen.add(widget.id)
            unique.append(widget)

        for widget in unique:
            # Whatever fade a widget was part-way through when this layout was
            # last on screen is over. Each starts where its condition says it
            # should be; see OverlayWidget.effective_opacity.
            widget.settle_fade()

        with self._lock:
            self._widgets = sorted(unique, key=lambda w: w.z_order)
            # Blend layers are keyed by widget id and the new set may reuse
            # ids with different content; dropping them forces a rebuild.
            self._layers.clear()

        for widget in self.widgets:
            self._subscribe(widget)
        log.info("Applied layout with %d widget(s)", len(unique))

    def resubscribe(self, widget: OverlayWidget) -> None:
        """Rebind a widget whose content has been edited.

        A widget's subscriptions are its keys *at the moment it was added*.
        Retyping a text widget's template in the layout editor changed which
        keys it reads and nothing re-bound it: the editor's own repaint made it
        look right, and then it froze. Whatever value the new keys held at that
        instant was burned into every frame for the rest of the take, because
        the keys it was still listening to had gone quiet -- a show name, a
        date, anything typed rather than sent.

        A widget this compositor does not hold is ignored rather than
        subscribed: bindings to something outside the layout would never be
        cancelled, because nothing would ever remove it.
        """
        with self._lock:
            if not any(w is widget for w in self._widgets):
                return
        self._subscribe(widget)

    def _subscribe(self, widget: OverlayWidget) -> None:
        """Mark a widget dirty when any key it reads changes.

        This is the whole of the rule that a widget re-renders only when its
        bound bus keys change: the widget does not poll, and nothing re-renders
        on a timer.

        Re-callable: a widget's previous subscriptions are dropped first, so
        rebinding after an edit cannot leave it dirtied by keys it no longer
        reads, or subscribed twice to the ones it still does.

        The widget is marked dirty here as well, because its cached picture is
        only as current as its subscriptions have been unbroken. A layout
        switched back to brings back the same widget objects, and while it was
        off screen they were subscribed to nothing. A command line was blank
        when its layout went off; the operator typed while another was up;
        back on the first it stayed blank until the next keystroke, which
        after an Enter may not come. A widget that had drawn something fared
        no better: "Cue 58" came back with its layout while the desk was
        on 60.

        Marked after subscribing, not before. apply_layout puts the new set in
        front of the capture thread before this runs, so a frame can redraw the
        widget in between, and a change landing after that redraw but ahead of
        the subscription would be missed in just the same way.
        """
        for subscription in self._subscriptions.pop(widget.id, ()):
            subscription.cancel()

        subscriptions = []
        for key in widget.bus_keys:
            def on_change(entry: BusEntry, target: OverlayWidget = widget) -> None:
                target.notify_changed()

            subscriptions.append(self.bus.subscribe(key, on_change))
        self._subscriptions[widget.id] = subscriptions
        widget.mark_dirty()

    def _cancel_subscriptions(self) -> None:
        for subscriptions in self._subscriptions.values():
            for subscription in subscriptions:
                subscription.cancel()
        self._subscriptions.clear()

    # ------------------------------------------------------------- visibility

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def panic_hide(self, hidden: bool = True) -> None:
        """Hide every widget at once, without losing the layout."""
        self._panic_hidden = hidden
        log.info("Overlay %s", "panic-hidden" if hidden else "restored")

    @property
    def is_hidden(self) -> bool:
        return self._panic_hidden or not self._enabled

    # ------------------------------------------------------------ compositing

    def composite(self, frame: np.ndarray, *, in_place: bool = True) -> np.ndarray:
        """Draw the overlay onto a BGR frame.

        ``in_place`` mutates the caller's array, which is what the recording
        path wants -- copying a 4K frame per composite would cost more than the
        blending does. The preview path passes ``in_place=False`` when it needs
        the clean frame kept.
        """
        if self.is_hidden:
            return frame

        target = frame if in_place else frame.copy()
        height, width = target.shape[:2]
        self._frame_size = (width, height)

        widgets = self.widgets
        removals = self.bus.removals
        if removals != self._removals_seen:
            # Keys were taken off the bus, which notifies nobody, so no
            # subscription marked the widgets that read them. Switching console
            # clears the old desk's keys: its cue label went on being drawn as
            # current, and a label over an unlabelled cue went on drawing
            # nothing instead of "--", for as long as the new desk took to
            # answer -- at a wrong address, the rest of the take. Every widget
            # is redrawn rather than working out which keys went, because this
            # happens on a console switch, not on a frame. Read before any of
            # them draws, so a removal landing mid-frame is caught on the next.
            self._removals_seen = removals
            for widget in widgets:
                widget.mark_dirty()

        for widget in widgets:
            try:
                self._blend_widget(widget, target, width, height)
            except Exception:  # noqa: BLE001 - one bad widget must not lose the frame
                log.exception("Widget %s failed to composite", widget.id)
        return target

    def _blend_widget(
        self, widget: OverlayWidget, target: np.ndarray, width: int, height: int
    ) -> None:
        opacity = widget.effective_opacity(self.bus)
        if opacity <= 0.001:
            return

        image = widget.image(self.bus, height, width)
        if image is None or image.isNull():
            self._widget_sizes.pop(widget.id, None)
            return
        self._widget_sizes[widget.id] = (image.width(), image.height())

        rect = widget.placement.resolve(width, height, image.width(), image.height())
        clipped = rect.clipped_to(width, height)
        if clipped.is_empty:
            return

        layers = self._layers.get(widget.id)
        if layers is None or layers.source_image is not image:
            layers = _BlendLayers(_qimage_to_rgba(image), image)
            self._layers[widget.id] = layers

        # Offsets into the widget image when it hangs off the frame edge.
        _alpha_blend_bgr(
            target, layers, clipped.x, clipped.y, opacity,
            src_x=clipped.x - rect.x,
            src_y=clipped.y - rect.y,
            width=clipped.width,
            height=clipped.height,
        )

    @property
    def frame_size(self) -> tuple[int, int] | None:
        """Width and height of the last frame composited, or None before one.

        The editor needs this to snap and clamp a drag in real pixels, and the
        compositor is the only thing that knows it -- the preview may be
        letterboxed and the recording may be a different size again.
        """
        return self._frame_size

    def widget_size(self, widget_id: str) -> tuple[int, int] | None:
        """How large a widget last drew, or None if it has not drawn."""
        return self._widget_sizes.get(widget_id)

    def dirty_area_fraction(self, width: int, height: int) -> float:
        """Fraction of the frame the widgets cover. Surfaced in the UI as a cost
        indicator -- it is what predicts whether 4K will keep up."""
        if not width or not height:
            return 0.0
        covered = 0
        for widget in self.widgets:
            rect = widget.rect(self.bus, width, height)
            if rect is not None:
                clipped = rect.clipped_to(width, height)
                covered += clipped.width * clipped.height
        return covered / float(width * height)


def _qimage_to_rgba(image: QImage) -> np.ndarray:
    """Copy a QImage into an (h, w, 4) uint8 RGBA array.

    It must be a copy, and the copy must happen here, while ``image`` is still
    referenced. Both halves matter:

    * the QImage belongs to the widget's cache and may be replaced under us on
      the next render;
    * anything that is not already RGBA8888 is converted first, and that
      conversion is a **temporary** QImage. It is freed the moment this
      function returns, so a view over its bits reads whatever the allocator
      has put there since.

    That second half was the live bug, and it was invisible for a long time
    because only pictures take it: ImageWidget renders ARGB32, render_text_block
    renders RGBA8888. Measured on the shipped watermark preset at its default
    height, barely half the logo's pixels survived the trip -- a comb-stripe of
    missing columns burned into every recorded frame with nothing logged. Above
    roughly a megabyte of scaled pixels the same read segfaults the process,
    which the try/except around composite() cannot catch.

    ``np.ascontiguousarray`` is not a copy when the array is already contiguous,
    which it is whenever bytesPerLine == width*4. It cannot stand in for this.
    """
    if image.format() != QImage.Format.Format_RGBA8888:
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = image.width(), image.height()
    pointer = image.constBits()
    array = np.frombuffer(pointer, dtype=np.uint8, count=height * image.bytesPerLine())
    # bytesPerLine can exceed width*4 due to row padding; slice it off.
    array = array.reshape(height, image.bytesPerLine() // 4, 4)[:, :width, :]
    return array.copy()


class _BlendLayers:
    """Premultiplied float32 layers for one widget image, computed once.

    Measured on a realistic widget patch (a translucent rounded box with opaque
    text) blended onto a 4K frame:

        integer uint16 numpy, recomputed per frame   14.8 ms
        premultiplied uint16, cached                  9.1 ms
        cv2 float32, premultiplied and cached         3.3 ms   <-- this
        opaque-copy fast path + soft-edge blend      23.2 ms

    OpenCV's arithmetic is SIMD-vectorised where numpy's uint16 path builds
    several full-size temporaries, which is where the time went. The "clever"
    fourth option -- straight-copying fully opaque pixels and blending only the
    soft edge -- was the slowest of the lot, because boolean fancy-indexing in
    numpy costs more than the arithmetic it avoids. Worth recording so nobody
    re-derives it.
    """

    __slots__ = ("premultiplied", "inverse", "source_image")

    def __init__(self, rgba: np.ndarray, source_image: object) -> None:
        self.source_image = source_image
        bgr = np.ascontiguousarray(rgba[:, :, 2::-1]).astype(np.float32)
        # Alpha broadcast to three channels: cv2 arithmetic requires matching
        # channel counts and will not broadcast an (h, w, 1) against (h, w, 3).
        alpha = cv2.merge([rgba[:, :, 3]] * 3).astype(np.float32) / 255.0
        self.premultiplied = bgr * alpha
        self.inverse = 1.0 - alpha


def _alpha_blend_bgr(
    target: np.ndarray, layers: _BlendLayers, x: int, y: int, opacity: float,
    src_x: int = 0, src_y: int = 0, width: int | None = None, height: int | None = None,
) -> None:
    """Alpha-blend a premultiplied patch onto a BGR frame, in place."""
    height = layers.premultiplied.shape[0] if height is None else height
    width = layers.premultiplied.shape[1] if width is None else width

    region = target[y : y + height, x : x + width]
    if region.shape[:2] != (height, width):
        return

    premultiplied = layers.premultiplied[src_y : src_y + height, src_x : src_x + width]
    inverse = layers.inverse[src_y : src_y + height, src_x : src_x + width]

    if opacity < 1.0:
        # Only during a fade, so the extra two multiplies are not on the hot
        # path. Scaling alpha means scaling the premultiplied source and
        # re-deriving the inverse, since inverse = 1 - alpha * opacity.
        premultiplied = premultiplied * opacity
        inverse = 1.0 - (1.0 - inverse) * opacity

    blended = cv2.add(
        premultiplied, cv2.multiply(region.astype(np.float32), inverse)
    )
    region[:] = blended.astype(np.uint8)
