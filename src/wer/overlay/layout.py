"""Layouts: named sets of widgets, switchable mid-recording.

A Layout is a named set of widget instances and their placements. Multiple per
show, hot-switchable by hotkey while recording -- a 'Tech' layout with
everything, a 'Minimal' one with the cue number, the record time and the show
name, and a 'None' layout that draws nothing at all.

Qt is imported here only because widgets are, and widgets draw. The
serialisation itself is plain data, so a layout can be read out of a show file
and inspected without a QApplication.

Serialisation
-------------
Every widget, style and placement round-trips through JSON, because layouts live
in the show file and that file has to stay human-readable and diffable. Anything
unrecognised on the way back in is skipped with a log line rather than taken as
a reason to refuse the whole layout -- a show file with one widget from a newer
version should still open, missing that widget, rather than not open at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from wer.overlay.geometry import Anchor, Placement
from wer.overlay.style import Align, BoxStyle, Colour, TextStyle
from wer.overlay.widgets import (
    ImageWidget,
    CueWidget,
    CueTimerWidget,
    FadeBarWidget,
    OverlayWidget,
    PanelWidget,
    StatusWidget,
    TextWidget,
    VisibilityRule,
)

log = logging.getLogger(__name__)

__all__ = ["Layout", "LayoutSet", "default_layouts", "widget_to_dict", "widget_from_dict"]


# --------------------------------------------------------------- serialisation


def _colour_to_dict(colour: Colour) -> dict[str, Any]:
    return {"hex": colour.to_hex(), "alpha": colour.a}


def _colour_from_dict(data: Any, fallback: Colour) -> Colour:
    if not isinstance(data, dict):
        return fallback
    try:
        return Colour.from_hex(data.get("hex", "#ffffff"), int(data.get("alpha", 255)))
    except (ValueError, TypeError):
        return fallback


def _style_to_dict(style: TextStyle) -> dict[str, Any]:
    return {
        "family": style.family,
        "size": style.size,
        "weight": style.weight,
        "italic": style.italic,
        "letter_spacing": style.letter_spacing,
        "line_spacing": style.line_spacing,
        "colour": _colour_to_dict(style.colour),
        "align": style.align.value,
        "opacity": style.opacity,
        "outline_width": style.outline_width,
        "outline": _colour_to_dict(style.outline),
        "shadow": style.shadow,
        "shadow_offset": style.shadow_offset,
        "shadow_colour": _colour_to_dict(style.shadow_colour),
        "box": {
            "enabled": style.box.enabled,
            "fill": _colour_to_dict(style.box.fill),
            "padding": style.box.padding,
            "corner_radius": style.box.corner_radius,
            "border_width": style.box.border_width,
            "border": _colour_to_dict(style.box.border),
        },
    }


def _style_from_dict(data: Any) -> TextStyle:
    if not isinstance(data, dict):
        return TextStyle()
    default = TextStyle()
    box_data = data.get("box") if isinstance(data.get("box"), dict) else {}
    default_box = default.box
    box = BoxStyle(
        enabled=bool(box_data.get("enabled", default_box.enabled)),
        fill=_colour_from_dict(box_data.get("fill"), default_box.fill),
        padding=float(box_data.get("padding", default_box.padding)),
        corner_radius=float(box_data.get("corner_radius", default_box.corner_radius)),
        border_width=float(box_data.get("border_width", default_box.border_width)),
        border=_colour_from_dict(box_data.get("border"), default_box.border),
    )
    try:
        align = Align(data.get("align", default.align.value))
    except ValueError:
        align = default.align
    return TextStyle(
        family=str(data.get("family", default.family)),
        size=float(data.get("size", default.size)),
        weight=int(data.get("weight", default.weight)),
        italic=bool(data.get("italic", default.italic)),
        letter_spacing=float(data.get("letter_spacing", default.letter_spacing)),
        line_spacing=float(data.get("line_spacing", default.line_spacing)),
        colour=_colour_from_dict(data.get("colour"), default.colour),
        align=align,
        opacity=float(data.get("opacity", default.opacity)),
        outline_width=float(data.get("outline_width", default.outline_width)),
        outline=_colour_from_dict(data.get("outline"), default.outline),
        shadow=bool(data.get("shadow", default.shadow)),
        shadow_offset=float(data.get("shadow_offset", default.shadow_offset)),
        shadow_colour=_colour_from_dict(data.get("shadow_colour"), default.shadow_colour),
        box=box,
    )


def _placement_to_dict(placement: Placement) -> dict[str, Any]:
    return {
        "anchor": placement.anchor.value,
        "offset_x": placement.offset_x,
        "offset_y": placement.offset_y,
        "margin": placement.margin,
    }


def _placement_from_dict(data: Any) -> Placement:
    if not isinstance(data, dict):
        return Placement()
    try:
        anchor = Anchor(data.get("anchor", Anchor.TOP_LEFT.value))
    except ValueError:
        log.info("Unknown anchor %r; using top-left", data.get("anchor"))
        anchor = Anchor.TOP_LEFT
    return Placement(
        anchor=anchor,
        offset_x=float(data.get("offset_x", 0.0)),
        offset_y=float(data.get("offset_y", 0.0)),
        margin=float(data.get("margin", Placement().margin)),
    )


def _visibility_to_dict(rule: VisibilityRule) -> dict[str, Any]:
    return {
        "only_when": rule.only_when,
        "only_when_present": rule.only_when_present,
        "hide_after": rule.hide_after,
        "fade": rule.fade,
    }


def _visibility_from_dict(data: Any) -> VisibilityRule:
    if not isinstance(data, dict):
        return VisibilityRule()
    return VisibilityRule(
        only_when=data.get("only_when") or None,
        only_when_present=data.get("only_when_present") or None,
        hide_after=float(data.get("hide_after", 0.0)),
        fade=float(data.get("fade", 0.25)),
    )


def widget_to_dict(widget: OverlayWidget) -> dict[str, Any]:
    """Serialise a widget. Raises TypeError for a kind we cannot write."""
    common = {
        "id": widget.id,
        "z_order": widget.z_order,
        "visible": widget.visible,
        "locked": widget.locked,
        "placement": _placement_to_dict(widget.placement),
        "style": _style_to_dict(widget.style),
        "visibility": _visibility_to_dict(widget.visibility),
        "max_width": widget.max_width,
    }
    # No widget type subclasses another today. If one ever does, test for it
    # before its parent: the parent's branch would catch it first, and it would
    # be saved as the parent with its own settings silently dropped.
    if isinstance(widget, CueWidget):
        return {
            **common,
            "type": "cue",
            "namespace": widget.ns,
            "show_pending": widget.show_pending,
            "show_progress": widget.show_progress,
            "show_label": widget.show_label,
            "show_previous": widget.show_previous,
            "show_list": widget.show_list,
        }
    if isinstance(widget, FadeBarWidget):
        return {
            **common,
            "type": "fadebar",
            "namespace": widget.ns,
            "width": widget.width,
            "height": widget.height,
            "hide_when_idle": widget.hide_when_idle,
        }
    if isinstance(widget, CueTimerWidget):
        return {
            **common,
            "type": "cuetimer",
            "namespace": widget.ns,
            "count_from": widget.count_from,
            "prefix": widget.prefix,
        }
    if isinstance(widget, PanelWidget):
        return {
            **common,
            "type": "panel",
            "width": widget.width,
            "height": widget.height,
        }
    if isinstance(widget, StatusWidget):
        return {
            **common,
            "type": "status",
            "key": widget.key,
            "on_text": widget.on_text,
            "off_text": widget.off_text,
            "on_colour": _colour_to_dict(widget.on_colour),
            "off_colour": _colour_to_dict(widget.off_colour),
            "show_dot": widget.show_dot,
            "hide_when_off": widget.hide_when_off,
        }
    if isinstance(widget, ImageWidget):
        return {
            **common,
            "type": "image",
            "path": widget.path,
            "height": widget.height,
            "opacity": widget.image_opacity,
        }
    if isinstance(widget, TextWidget):
        return {
            **common,
            "type": "text",
            "template": widget.template,
            "emphasise_first_line": widget.emphasise_first_line,
        }
    raise TypeError(f"cannot serialise widget type {type(widget).__name__}")


def widget_from_dict(data: dict[str, Any]) -> OverlayWidget | None:
    """Rebuild a widget. Returns None for anything unrecognised.

    None rather than an exception: a show file written by a newer Wer should
    open with the widgets this build understands, not refuse to open at all.
    """
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    widget_id = str(data.get("id") or kind or "widget")

    common = dict(
        placement=_placement_from_dict(data.get("placement")),
        style=_style_from_dict(data.get("style")),
        z_order=int(data.get("z_order", 0)),
        visible=bool(data.get("visible", True)),
        locked=bool(data.get("locked", False)),
        visibility=_visibility_from_dict(data.get("visibility")),
        # `or`, not a get() default: a hand-edited null means "no cap" as
        # plainly as a missing key does.
        max_width=float(data.get("max_width") or 0.0),
    )

    if kind == "text":
        return TextWidget(
            widget_id,
            str(data.get("template", "")),
            emphasise_first_line=bool(data.get("emphasise_first_line", False)),
            **common,
        )
    if kind == "image":
        return ImageWidget(
            widget_id,
            str(data.get("path", "")),
            height=float(data.get("height", 0.10)),
            opacity=float(data.get("opacity", 1.0)),
            **common,
        )
    if kind == "cue":
        return CueWidget(
            widget_id,
            namespace=str(data.get("namespace", "eos")),
            show_pending=bool(data.get("show_pending", True)),
            show_progress=bool(data.get("show_progress", True)),
            show_label=bool(data.get("show_label", True)),
            show_previous=bool(data.get("show_previous", False)),
            show_list=bool(data.get("show_list", True)),
            **common,
        )
    if kind == "fadebar":
        return FadeBarWidget(
            widget_id,
            namespace=str(data.get("namespace", "eos")),
            width=float(data.get("width", 0.30)),
            height=float(data.get("height", 0.012)),
            hide_when_idle=bool(data.get("hide_when_idle", True)),
            **common,
        )
    if kind == "cuetimer":
        return CueTimerWidget(
            widget_id,
            namespace=str(data.get("namespace", "eos")),
            count_from=str(data.get("count_from", "fade_start")),
            prefix=str(data.get("prefix", "In cue ")),
            **common,
        )
    if kind == "panel":
        return PanelWidget(
            widget_id,
            width=float(data.get("width", 1.0)),
            height=float(data.get("height", 0.10)),
            **common,
        )
    if kind == "status":
        from wer.overlay.style import GREEN, RED

        return StatusWidget(
            widget_id,
            str(data.get("key", "")),
            on_text=str(data.get("on_text", "")),
            off_text=str(data.get("off_text", "")),
            on_colour=_colour_from_dict(data.get("on_colour"), GREEN),
            off_colour=_colour_from_dict(data.get("off_colour"), RED),
            show_dot=bool(data.get("show_dot", True)),
            hide_when_off=bool(data.get("hide_when_off", False)),
            **common,
        )
    log.warning("Skipping widget of unknown type %r in layout", kind)
    return None


# ---------------------------------------------------------------------- model


@dataclass
class Layout:
    """A named set of widgets."""

    name: str
    widgets: list[OverlayWidget] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        serialised = []
        for widget in self.widgets:
            try:
                serialised.append(widget_to_dict(widget))
            except TypeError:
                log.exception("Skipping unserialisable widget %s", widget.id)
        return {"name": self.name, "widgets": serialised}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Layout":
        """Rebuild a layout, leaving out any widget that cannot be read.

        widget_from_dict already skips a widget of a kind it does not know. A
        widget of a kind it does know, carrying a value that is not a number --
        a hand-edited "height": "big" -- raised instead, out of
        LayoutSet.from_list, which the main window calls while it is building
        itself and does not guard. One typo in one widget stopped the window
        being built at all. Now that widget is left out, and the log says which.
        """
        widgets = []
        for entry in data.get("widgets") or []:
            try:
                widget = widget_from_dict(entry)
            except (TypeError, ValueError) as exc:
                log.warning(
                    "Skipping widget %r in layout %r, which could not be read: %s",
                    entry.get("id") if isinstance(entry, dict) else entry,
                    data.get("name"), exc,
                )
                continue
            if widget is not None:
                widgets.append(widget)
        return cls(name=str(data.get("name", "Untitled")), widgets=widgets)


class LayoutSet:
    """The layouts in a show, and which one is live.

    Switching is a whole-compositor swap rather than a per-widget toggle,
    because it happens on a hotkey mid-recording: one call, no half-applied
    state on the frame being composited at that moment.
    """

    def __init__(self, layouts: list[Layout] | None = None, active: str = "") -> None:
        self._layouts: list[Layout] = list(layouts or [])
        self._active = active
        self._settle_active()

    def _settle_active(self, quiet: bool = False) -> None:
        """Keep the active name pointing at a layout that is actually here.

        Nothing enforced this, and a show file is free to disagree with
        itself: ``active_layout`` naming a layout whose entry in ``layouts``
        did not survive the trip -- an empty list, a hand edit, a file written
        when it went by another name. from_list then substitutes the shipped
        layouts and keeps the saved active name, and the set has an active
        layout that resolves to nothing.

        Everything downstream went quiet rather than wrong, which is the worst
        way for it to go. The main window hands the compositor nothing, so the
        overlay is blank and the editor's widget list empty; it has no layout
        to fold an edit back into, so every change is dropped on the next
        switch; and Delete asks to remove a layout that is not in the set,
        removes nothing, and switches to the same missing name -- which is
        "in the overlay page, we aren't able to delete overlays". Falling back
        to the first layout costs one line in the log and leaves all of it
        working.

        ``quiet`` is for remove(), which takes the live layout away on purpose.
        The warning is there to report a file that disagrees with itself -- it
        is the line someone is asked to look for in the log -- and a Delete
        that printed it too would leave that line meaning nothing.
        """
        if self._active and self.get(self._active) is not None:
            return
        first = self._layouts[0].name if self._layouts else ""
        # Nothing said when there is no layout to fall back to: "using ''
        # instead" reads as nonsense, and a set with no layouts at all is the
        # louder problem, which the main window already handles.
        if self._active and first and not quiet:
            log.warning(
                "No layout called %r to be the active one; using %r instead",
                self._active, first,
            )
        self._active = first

    @property
    def names(self) -> list[str]:
        return [layout.name for layout in self._layouts]

    @property
    def active_name(self) -> str:
        return self._active

    @property
    def active(self) -> Layout | None:
        return self.get(self._active)

    def get(self, name: str) -> Layout | None:
        return next((l for l in self._layouts if l.name == name), None)

    def add(self, layout: Layout) -> Layout:
        existing = self.get(layout.name)
        if existing is not None:
            self._layouts[self._layouts.index(existing)] = layout
        else:
            self._layouts.append(layout)
        self._settle_active()
        return layout

    def remove(self, name: str) -> None:
        layout = self.get(name)
        if layout is None or len(self._layouts) == 1:
            # Never leave a show with no layout at all.
            return
        self._layouts.remove(layout)
        self._settle_active(quiet=True)

    def activate(self, name: str) -> Layout | None:
        layout = self.get(name)
        if layout is None:
            log.warning("No layout called %r", name)
            return None
        self._active = name
        log.info("Layout switched to %r", name)
        return layout

    def next(self) -> Layout | None:
        """Cycle. What a single hotkey should do when there are several."""
        if not self._layouts:
            return None
        names = self.names
        index = names.index(self._active) if self._active in names else -1
        return self.activate(names[(index + 1) % len(names)])

    def to_list(self) -> list[dict[str, Any]]:
        return [layout.to_dict() for layout in self._layouts]

    @classmethod
    def from_list(cls, data: list[Any], active: str = "") -> "LayoutSet":
        layouts = [
            Layout.from_dict(entry) for entry in data if isinstance(entry, dict)
        ]
        if data and not layouts:
            # The shipped layouts stand in, which is right -- a show with no
            # layout has no overlay -- but it is the operator's own layouts
            # that have just gone, and going quiet about it is how this reads
            # from the booth as "my overlays have vanished". Only said when
            # there was something to lose: an empty list is a first run.
            log.warning(
                "None of the %d saved layout(s) could be read; "
                "using the ready-made ones instead", len(data),
            )
        return cls(layouts or default_layouts(), active)

    def __len__(self) -> int:
        return len(self._layouts)


# ------------------------------------------------------------------- defaults


def default_layouts() -> list[Layout]:
    """The three layouts a show starts with, ready to use.

    These are the ones Hudson runs. **Tech** is what you want while teching:
    the cue block with the next cue and the fade bar top left, the show name
    top centre, the clock and date top right with the record time under them,
    and bottom left the seconds left in the running cue with how long the cue
    has been up just above it. The timer is lifted by its own offset, so the
    countdown is the line that sits in the corner.

    **Minimal** is the cue number bottom left, the record time bottom right
    and the show name top left, each on its backing box. It is what you want
    on a recording that will be watched rather than worked from.

    **None** puts nothing on the picture. It is there for a stretch that has
    to be recorded clean, and it is one hotkey away from the other two, so
    coming back is not a trip through the Overlay tab.

    Two widgets carry a ``max_width``: Tech's cue block and its show name.
    Both grow with whatever the desk and the show file put in them, and both
    share the top band with the clock; uncapped, a long cue label ran through
    the show name and on into the clock, one drawn over the other, for as long
    as that cue was live. Each is now held to its own lane and cut short with
    an ellipsis at the edge of it.

    A lane is worth having only where a widget can grow into a neighbour, so
    nothing else here has one. Most of the rest read a key of fixed length --
    a time, a cue number, the seconds left in a fade -- and could not reach
    anything however long the show ran. Minimal's show name is the exception,
    and worth knowing about: it grows exactly as Tech's does, and it is left
    uncapped because it has the whole top of the picture to itself, the cue
    number and the record time being down in the bottom corners. A desk
    carrying a long show name therefore draws it clear across the top of a
    Minimal recording, stopped only by the margin. That is what Hudson's file
    does and it runs into nothing; put a second widget in Minimal's top band
    and the show name wants a lane before it goes there.
    """
    tech = Layout(
        name="Tech",
        widgets=[
            CueWidget(
                "cue",
                placement=Placement(Anchor.TOP_LEFT),
                style=TextStyle(size=0.048),
                z_order=10,
                max_width=0.34,
            ),
            TextWidget(
                "show_name",
                "{eos.show.name}",
                placement=Placement(Anchor.TOP_CENTER),
                style=TextStyle(size=0.026),
                z_order=40,
                max_width=0.26,
            ),
            TextWidget(
                "elapsed",
                "{clock.elapsed}",
                placement=Placement(Anchor.TOP_RIGHT, offset_y=0.133),
                style=TextStyle(size=0.030),
                z_order=50,
            ),
            TextWidget(
                "clock_date",
                "{clock.wall12}\n{clock.date_long}",
                emphasise_first_line=True,
                placement=Placement(Anchor.TOP_RIGHT),
                style=TextStyle(size=0.034, align=Align.RIGHT),
                z_order=60,
            ),
            TextWidget(
                "cue_countdown",
                "{eos.cue.active.remaining}s left",
                placement=Placement(Anchor.BOTTOM_LEFT),
                style=TextStyle(size=0.030),
                z_order=70,
            ),
            CueTimerWidget(
                "cue_timer",
                placement=Placement(Anchor.BOTTOM_LEFT, offset_y=-0.095),
                style=TextStyle(size=0.030),
                z_order=80,
            ),
        ],
    )

    minimal = Layout(
        name="Minimal",
        widgets=[
            TextWidget(
                "cue_number",
                "Cue {eos.cue.active.number}",
                placement=Placement(Anchor.BOTTOM_LEFT),
                style=TextStyle(size=0.030),
                z_order=10,
            ),
            TextWidget(
                "elapsed",
                "{clock.elapsed}",
                placement=Placement(Anchor.BOTTOM_RIGHT),
                style=TextStyle(size=0.030),
                z_order=20,
            ),
            TextWidget(
                "show_name",
                "{eos.show.name}",
                placement=Placement(Anchor.TOP_LEFT),
                style=TextStyle(size=0.030),
                z_order=30,
            ),
        ],
    )

    return [tech, minimal, Layout(name="None")]
