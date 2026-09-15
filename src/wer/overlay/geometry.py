"""Resolution-independent placement -- the sort of thing to get right first time.

No Qt -- this is arithmetic, and keeping it Qt-free means the placement rules can
be tested exhaustively without a QApplication.

The rule
--------
A widget's position is a **9-point anchor plus an offset in normalized
coordinates**, never absolute pixels. A layout built against a 1080p preview must
survive being recorded at 4K or 720p unchanged, and font sizes scale with output
height for the same reason.

The anchor does two jobs at once, and both are needed:

1. It picks a reference point in the frame (top-left corner, centre, and so on).
2. It decides which point *of the widget* sits there.

That second job is what keeps a bottom-right widget in the corner as its text
grows. If the anchor only positioned the widget's top-left, a command line
anchored bottom-right would slide off the frame the moment the operator typed a
longer command -- which is precisely when you most want to read it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum

__all__ = [
    "Anchor",
    "Placement",
    "Rect",
    "DEFAULT_MARGIN",
    "DragResult",
    "Guide",
    "SNAP_SCREEN_PIXELS",
    "STACK_GAP",
    "clamped_placement",
    "drag_placement",
]

#: Default inset from the frame edge, as a fraction of frame size. Widgets flush
#: against the edge look like a mistake, and on a projector or a TV with
#: overscan they can be clipped entirely.
DEFAULT_MARGIN = 0.02


class Anchor(str, Enum):
    TOP_LEFT = "top-left"
    TOP_CENTER = "top-center"
    TOP_RIGHT = "top-right"
    MIDDLE_LEFT = "middle-left"
    CENTER = "center"
    MIDDLE_RIGHT = "middle-right"
    BOTTOM_LEFT = "bottom-left"
    BOTTOM_CENTER = "bottom-center"
    BOTTOM_RIGHT = "bottom-right"

    @property
    def horizontal(self) -> float:
        """0.0 left, 0.5 centre, 1.0 right."""
        if self in (Anchor.TOP_LEFT, Anchor.MIDDLE_LEFT, Anchor.BOTTOM_LEFT):
            return 0.0
        if self in (Anchor.TOP_RIGHT, Anchor.MIDDLE_RIGHT, Anchor.BOTTOM_RIGHT):
            return 1.0
        return 0.5

    @property
    def vertical(self) -> float:
        """0.0 top, 0.5 middle, 1.0 bottom."""
        if self in (Anchor.TOP_LEFT, Anchor.TOP_CENTER, Anchor.TOP_RIGHT):
            return 0.0
        if self in (Anchor.BOTTOM_LEFT, Anchor.BOTTOM_CENTER, Anchor.BOTTOM_RIGHT):
            return 1.0
        return 0.5

    @property
    def label(self) -> str:
        return self.value.replace("-", " ").title()

    @classmethod
    def from_components(cls, horizontal: float, vertical: float) -> "Anchor":
        """The anchor at these fractions across and down, each 0, 0.5 or 1.

        Snapping settles each axis on its own -- a widget can be pulled flush
        to the right margin while it is still free to sit anywhere up and down
        -- so the anchor it ends up with has to be assembled from two halves.
        """
        for anchor in cls:
            if anchor.horizontal == horizontal and anchor.vertical == vertical:
                return anchor
        raise ValueError(f"no anchor at ({horizontal}, {vertical})")


@dataclass(frozen=True, slots=True)
class Rect:
    """An integer pixel rectangle."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def clipped_to(self, width: int, height: int) -> "Rect":
        """Clip to a frame, so a widget dragged off-frame cannot index outside it."""
        x = max(0, min(self.x, width))
        y = max(0, min(self.y, height))
        right = max(0, min(self.right, width))
        bottom = max(0, min(self.bottom, height))
        return Rect(x, y, right - x, bottom - y)

    def intersects(self, other: "Rect") -> bool:
        return not (
            self.right <= other.x
            or other.right <= self.x
            or self.bottom <= other.y
            or other.bottom <= self.y
        )


@dataclass(frozen=True, slots=True)
class Placement:
    """Where a widget sits, independent of output resolution."""

    anchor: Anchor = Anchor.TOP_LEFT
    #: Offset from the anchor point, as a fraction of frame width / height.
    #: Positive x is right, positive y is down, in both cases regardless of
    #: which anchor is used -- "move it 2% right" should mean the same thing
    #: everywhere, even for a right-anchored widget.
    offset_x: float = 0.0
    offset_y: float = 0.0
    #: Inset from the frame edge, as a fraction of frame size. Applied in the
    #: direction that pushes the widget inward, so it does nothing for a
    #: centred anchor.
    margin: float = DEFAULT_MARGIN

    def __post_init__(self) -> None:
        # Anchor subclasses str, so a QComboBox hands back a bare string and so
        # does JSON. Every use of `.horizontal` and `.value` would then fail.
        # frozen dataclass: assign through object.__setattr__.
        object.__setattr__(self, "anchor", Anchor(self.anchor))

    def resolve(
        self, frame_width: int, frame_height: int, widget_width: int, widget_height: int
    ) -> Rect:
        """Compute the widget's pixel rectangle within a frame of this size."""
        anchor_x = self.anchor.horizontal
        anchor_y = self.anchor.vertical

        # The point in the frame the widget hangs off.
        point_x = anchor_x * frame_width
        point_y = anchor_y * frame_height

        # Push inward from whichever edge we are anchored to. A centred anchor
        # gets no margin, which is right: it has no edge to be pushed from.
        point_x += (1.0 - 2.0 * anchor_x) * self.margin * frame_width
        point_y += (1.0 - 2.0 * anchor_y) * self.margin * frame_height

        # The matching point of the widget sits there, which is what keeps a
        # right-anchored widget in its corner as its content grows.
        x = point_x - anchor_x * widget_width + self.offset_x * frame_width
        y = point_y - anchor_y * widget_height + self.offset_y * frame_height

        return Rect(round(x), round(y), widget_width, widget_height)


def scale_to_height(fraction: float, frame_height: int, *, minimum: int = 8) -> int:
    """Convert a height fraction to pixels.

    Font sizes are stored as a fraction of frame height so that a layout built
    against a 1080p preview is legible at 4K and still readable at 720p.
    ``minimum`` stops a very small fraction rounding to zero and making a
    widget vanish rather than merely look wrong.
    """
    return max(minimum, round(fraction * frame_height))


#: However far a widget is dragged, at least this much of it stays on the
#: frame. Dragging one entirely out of view loses it: there is nothing left to
#: grab, and the only way back is to delete and rebuild it -- which is exactly
#: what happened, so the clamp is not a nicety.
MIN_VISIBLE = 0.35


def clamped_placement(
    placement: "Placement",
    frame_width: int,
    frame_height: int,
    widget_width: int,
    widget_height: int,
    *,
    min_visible: float = MIN_VISIBLE,
) -> "Placement":
    """Pull a widget back until enough of it is on the frame to grab again."""
    rect = placement.resolve(frame_width, frame_height, widget_width, widget_height)
    keep_x = max(1.0, widget_width * min_visible)
    keep_y = max(1.0, widget_height * min_visible)

    lowest_x = keep_x - widget_width
    highest_x = frame_width - keep_x
    lowest_y = keep_y - widget_height
    highest_y = frame_height - keep_y

    wanted_x = min(max(float(rect.x), lowest_x), highest_x)
    wanted_y = min(max(float(rect.y), lowest_y), highest_y)
    if (wanted_x, wanted_y) == (float(rect.x), float(rect.y)):
        return placement

    return replace(
        placement,
        offset_x=placement.offset_x + (wanted_x - rect.x) / frame_width,
        offset_y=placement.offset_y + (wanted_y - rect.y) / frame_height,
    )


# -------------------------------------------------------------------- dragging

#: How close a dragged widget must come to a guide before it snaps, in pixels
#: ON SCREEN. The preview converts it to frame pixels at its own scale.
#:
#: It used to be 2% of the frame's smaller side in frame pixels -- about 22 px
#: of a 1080p frame, which is 10 px on a small preview and 18 px on a large
#: one, so the same movement of the hand caught on one monitor and not on the
#: other. Measuring it on screen makes it feel the same at any window size.
SNAP_SCREEN_PIXELS = 6.0

#: The gap left between two widgets snapped beside or below each other, as a
#: fraction of frame height (about 6 px at 1080p). Boxes that touch read as one
#: block with a seam through it; a few pixels apart they read as two things
#: that line up.
STACK_GAP = 0.006

#: Which guide wins when two are exactly as close. The frame's own margins and
#: centre come first because snapping to one also settles the anchor, and the
#: anchor is what keeps a widget in its place at another resolution.
_GUIDE_PRIORITY = {"frame": 0, "widget": 1, "grid": 2}


@dataclass(frozen=True, slots=True)
class Guide:
    """A line a dragged widget has snapped to, for the preview to draw."""

    #: "x" for a vertical line at a position across the frame, "y" for a
    #: horizontal line at a position down it.
    axis: str
    #: Where the line is, in frame pixels.
    position: float
    #: "frame" (a margin edge or a centre line), "widget" (lined up with, or
    #: stacked against, another widget) or "grid".
    kind: str


@dataclass(frozen=True, slots=True)
class DragResult:
    """Where a dragged widget lands, and the guides that put it there."""

    placement: Placement
    guides: tuple[Guide, ...] = ()


def drag_placement(
    start: Placement,
    delta_x: float,
    delta_y: float,
    frame_width: int,
    frame_height: int,
    widget_width: int,
    widget_height: int,
    *,
    within: float = 0.0,
    others: Sequence[Rect] = (),
    grid: tuple[int, int] | None = None,
    axis: str = "",
) -> DragResult:
    """Where a widget lands after being dragged ``delta`` from where it started.

    ``delta_x`` and ``delta_y`` are the WHOLE movement since the button went
    down, as fractions of the frame, and ``start`` is the placement the widget
    had at that moment. That is the fix for snapping being sticky, and the
    threshold was never the problem. The preview used to send each mouse
    movement as a small step, and each step was added to wherever the previous
    one had snapped to: a widget sitting on a guide moved one pixel, was still
    in range, and was put straight back -- and so was the next pixel, and the
    one after. The only way off a guide was to cover the whole snap distance
    between two mouse events, which is a flick. Snapping the pointer's real
    position instead lets go the moment the pointer is further than ``within``
    from the line, and from then on the widget follows the hand exactly.

    Each axis snaps on its own, to the nearest of:

    * the frame's margin edges and its centre line. These also set that half
      of the anchor, so a widget pulled flush to the right margin becomes
      right-anchored and grows leftwards, which is what being there means;
    * another widget in ``others``: edges or centres lined up, or placed just
      beside, above or below it with a small gap. This is how a date goes
      under a clock without landing on top of it -- which is what snapping to
      the nine anchor points did, because two widgets dropped near one corner
      were given exactly the same position;
    * with ``grid`` as (columns, rows), the nearest grid line, meeting the
      widget's anchored point: the edge it grows away from, or its centre.

    ``within`` is in frame pixels, and 0 turns snapping off. ``axis`` "x" keeps
    the widget on its starting row and "y" in its starting column; the axis
    held still does not snap either, or a straight drag could wander.
    """
    moved_x = 0.0 if axis == "y" else delta_x
    moved_y = 0.0 if axis == "x" else delta_y
    raw = replace(
        start,
        offset_x=start.offset_x + moved_x,
        offset_y=start.offset_y + moved_y,
    )

    def clamp(placement: Placement) -> Placement:
        return clamped_placement(
            placement, frame_width, frame_height, widget_width, widget_height
        )

    if within <= 0 or frame_width <= 0 or frame_height <= 0:
        return DragResult(clamp(raw))

    gap = STACK_GAP * frame_height
    horizontal, vertical = raw.anchor.horizontal, raw.anchor.vertical
    offset_x, offset_y = raw.offset_x, raw.offset_y
    guides: list[Guide] = []

    if axis != "y":
        horizontal, offset_x, found = _snap_axis(
            frame=frame_width, size=widget_width, component=horizontal,
            margin=raw.margin, offset=offset_x,
            spans=[(other.x, other.right) for other in others],
            divisions=grid[0] if grid else 0, within=within, gap=gap,
        )
        if found is not None:
            guides.append(Guide("x", *found))
    if axis != "x":
        vertical, offset_y, found = _snap_axis(
            frame=frame_height, size=widget_height, component=vertical,
            margin=raw.margin, offset=offset_y,
            spans=[(other.y, other.bottom) for other in others],
            divisions=grid[1] if grid else 0, within=within, gap=gap,
        )
        if found is not None:
            guides.append(Guide("y", *found))

    snapped = replace(
        raw,
        anchor=Anchor.from_components(horizontal, vertical),
        offset_x=offset_x,
        offset_y=offset_y,
    )
    kept = clamp(snapped)
    if kept != snapped:
        # Pulled back onto the frame, so it is no longer on the line it
        # snapped to, and drawing that line would say it was.
        return DragResult(kept)
    return DragResult(snapped, tuple(guides))


def _snap_axis(
    *,
    frame: float,
    size: float,
    component: float,
    margin: float,
    offset: float,
    spans: Sequence[tuple[float, float]],
    divisions: int,
    within: float,
    gap: float,
) -> tuple[float, float, tuple[float, str] | None]:
    """Snap one axis of a drag.

    Works on the widget's leading edge (its left, or its top) in frame pixels.
    Returns the anchor component and offset to use, and the guide it snapped
    to as (position, kind), or None when nothing was close enough.
    """

    def leading(at: float) -> float:
        """The leading edge of a widget anchored at ``at``, with no offset."""
        point = at * frame + (1.0 - 2.0 * at) * margin * frame
        return point - at * size

    current = leading(component) + offset * frame
    #: (leading edge it would snap to, kind, where the line is drawn,
    #: the anchor component it adopts or None to keep its own)
    candidates: list[tuple[float, str, float, float | None]] = []

    for at in (0.0, 0.5, 1.0):
        edge = leading(at)
        candidates.append((edge, "frame", edge + at * size, at))

    for begin, end in spans:
        middle = (begin + end) / 2.0
        candidates += [
            (begin, "widget", begin, None),
            (end - size, "widget", end, None),
            (middle - size / 2.0, "widget", middle, None),
            (end + gap, "widget", end + gap, None),
            (begin - gap - size, "widget", begin - gap, None),
        ]

    if divisions > 0:
        step = frame / divisions
        # The anchored point goes on the line, because it is the point that
        # stays put when the widget's content grows or shrinks.
        point = current + component * size
        line = round(point / step) * step
        candidates.append((line - component * size, "grid", line, None))

    best: tuple[tuple[float, int], float, str, float, float | None] | None = None
    for target, kind, line, at in candidates:
        distance = abs(target - current)
        if distance > within:
            continue
        rank = (round(distance, 3), _GUIDE_PRIORITY[kind])
        if best is None or rank < best[0]:
            best = (rank, target, kind, line, at)

    if best is None:
        return component, offset, None
    _, target, kind, line, at = best
    if at is not None:
        return at, 0.0, (line, kind)
    return component, offset + (target - current) / frame, (line, kind)
