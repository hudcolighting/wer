"""Ready-made widgets you can drop onto a layout.

Every preset is something the DataBus already carries, so adding one is a menu
choice rather than a template you have to know the key names to write. The key
names are still shown, because once you have added a few you will want to edit
the template directly, and guessing bus keys from memory is miserable.

Grouped the way a lighting person would look for them, not the way the code is
organised.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from wer.overlay.geometry import Anchor, Placement
from wer.overlay.style import AMBER, RED, Align, BoxStyle, Colour, TextStyle
from wer.overlay.widgets import (
    CueWidget,
    CueTimerWidget,
    FadeBarWidget,
    ImageWidget,
    OverlayWidget,
    PanelWidget,
    StatusWidget,
    TextWidget,
    VisibilityRule,
)

__all__ = ["WidgetPreset", "PRESETS", "preset_groups", "make_widget", "unique_id"]


@dataclass(frozen=True)
class WidgetPreset:
    """One entry in the Add Widget menu."""

    key: str
    label: str
    group: str
    description: str
    factory: Callable[[str], OverlayWidget]
    #: Bus keys this reads, shown in the UI so the source is never a mystery.
    reads: tuple[str, ...] = field(default_factory=tuple)

    def build(self, widget_id: str) -> OverlayWidget:
        return self.factory(widget_id)


def _text(
    template: str,
    *,
    anchor: Anchor = Anchor.TOP_LEFT,
    size: float = 0.032,
    colour: Colour | None = None,
    only_when_present: str | None = None,
    z_order: int = 50,
) -> Callable[[str], OverlayWidget]:
    def build(widget_id: str) -> OverlayWidget:
        style = TextStyle(size=size)
        if colour is not None:
            style = style.with_colour(colour)
        return TextWidget(
            widget_id,
            template,
            placement=Placement(anchor),
            style=style,
            z_order=z_order,
            visibility=VisibilityRule(only_when_present=only_when_present),
        )

    return build


def _backing(
    *, width: float, height: float, placement: Placement, corner_radius: float,
) -> Callable[[str], OverlayWidget]:
    """A dark panel on layer 1, underneath every other preset."""

    def build(widget_id: str) -> OverlayWidget:
        return PanelWidget(
            widget_id,
            width=width,
            height=height,
            placement=placement,
            style=TextStyle(
                box=BoxStyle(fill=Colour(0, 0, 0, 150), corner_radius=corner_radius)
            ),
            z_order=1,
        )

    return build


PRESETS: tuple[WidgetPreset, ...] = (
    # ------------------------------------------------------------------ cue
    WidgetPreset(
        "cue_block", "Cue block", "Cue",
        "Cue number, label, next cue and a fade-progress bar. The workhorse.",
        lambda i: CueWidget(i, placement=Placement(Anchor.TOP_LEFT),
                            style=TextStyle(size=0.048), z_order=10),
        ("eos.cue.active.number", "eos.cue.active.label",
         "eos.cue.pending.number", "eos.cue.active.progress"),
    ),
    WidgetPreset(
        "cue_stack", "Cue stack (last, now, next)", "Cue",
        "The cue you came from, the live cue with its label, and the one "
        "standing by, stacked in the order they run, with a fade bar under "
        "them. For a tech recording you will scrub back through looking for "
        "where a transition went wrong.",
        lambda i: CueWidget(i, show_previous=True, show_pending=True,
                            show_progress=True,
                            placement=Placement(Anchor.TOP_LEFT),
                            style=TextStyle(size=0.044), z_order=10),
        ("eos.cue.previous.number", "eos.cue.previous.label",
         "eos.cue.active.number", "eos.cue.active.label",
         "eos.cue.pending.number", "eos.cue.pending.label",
         "eos.cue.active.progress"),
    ),
    WidgetPreset(
        "cue_number", "Cue number only", "Cue",
        "Just the number, large. For an archive recording where the label is "
        "clutter.",
        _text("Cue {eos.cue.active.number}", size=0.055, z_order=10),
        ("eos.cue.active.number",),
    ),
    WidgetPreset(
        "cue_label", "Cue label", "Cue",
        "The cue's name as recorded on the desk.",
        _text("{eos.cue.active.label}", size=0.030),
        ("eos.cue.active.label",),
    ),
    WidgetPreset(
        "cue_duration", "Cue duration", "Cue",
        "The active cue's fade time in seconds, as the console reports it.",
        _text("Time {eos.cue.active.duration}s", size=0.028,
              anchor=Anchor.MIDDLE_LEFT),
        ("eos.cue.active.duration",),
    ),
    WidgetPreset(
        "cue_countdown", "Fade countdown", "Cue",
        "Seconds remaining in the running fade. Blank when nothing is fading, "
        "so it does not sit at zero all night.",
        lambda i: TextWidget(
            i, "{eos.cue.active.remaining}s left",
            placement=Placement(Anchor.MIDDLE_LEFT),
            style=TextStyle(size=0.030, colour=AMBER),
            z_order=50,
            visibility=VisibilityRule(only_when="eos.cue.active.fading"),
        ),
        ("eos.cue.active.remaining", "eos.cue.active.fading"),
    ),
    WidgetPreset(
        "cue_timer", "Time in cue", "Cue",
        "How long the live cue has been up, as m:ss. Counts from the start of "
        "the fade into the cue, or from the end of it; choose which in its "
        "settings. Shows -- until it has seen a cue fire, so a cue that was "
        "already up when Wer connected is never given a made-up start.",
        lambda i: CueTimerWidget(
            i,
            placement=Placement(Anchor.MIDDLE_LEFT),
            style=TextStyle(size=0.030),
            z_order=50,
        ),
        ("eos.cue.active.number", "eos.cue.active.list", "eos.cue.active.fading"),
    ),
    WidgetPreset(
        "fade_bar", "Fade progress bar", "Cue",
        "A bar along the bottom of the frame that fills as the cue fade runs, "
        "and is gone between fades. The cue block's bar on its own, for a "
        "layout that wants to see the fade without more text.",
        lambda i: FadeBarWidget(
            i, width=0.5, height=0.012,
            placement=Placement(Anchor.BOTTOM_CENTER),
            z_order=50,
        ),
        ("eos.cue.active.progress", "eos.cue.active.duration",
         "eos.cue.active.fading"),
    ),
    WidgetPreset(
        "cue_pending", "Next cue", "Cue",
        "The cue standing by.",
        _text("Next {eos.cue.pending.number} {eos.cue.pending.label}", size=0.026),
        ("eos.cue.pending.number", "eos.cue.pending.label"),
    ),
    WidgetPreset(
        "cue_previous", "Previous cue", "Cue",
        "The cue you just came from, which is the one you usually want to go "
        "back to.",
        _text("Last {eos.cue.previous.number} {eos.cue.previous.label}",
              size=0.024, anchor=Anchor.BOTTOM_LEFT),
        ("eos.cue.previous.number", "eos.cue.previous.label"),
    ),
    WidgetPreset(
        "cue_text", "Cue line (console format)", "Cue",
        "The console's own one-line summary, exactly as it sends it.",
        _text("{eos.cue.active.text}", size=0.028),
        ("eos.cue.active.text",),
    ),

    WidgetPreset(
        "watermark", "Watermark / logo", "Picture",
        "A picture over the video -- a theatre logo, a production mark, a "
        "PROPERTY OF stamp. Choose the image file in the settings beside it.",
        lambda i: ImageWidget(
            i, "",
            height=0.10,
            opacity=0.85,
            placement=Placement(Anchor.BOTTOM_RIGHT),
            z_order=5,
        ),
        (),
    ),

    # ------------------------------------------------------------- console
    WidgetPreset(
        "cmdline", "Command line", "Console",
        "The live Eos command line, updating per keystroke. Enormously useful "
        "in a tech recording.",
        lambda i: TextWidget(
            i, "{eos.cmdline.text}",
            placement=Placement(Anchor.BOTTOM_LEFT),
            style=TextStyle(size=0.030), z_order=20,
            visibility=VisibilityRule(only_when_present="eos.cmdline.text"),
        ),
        ("eos.cmdline.text",),
    ),
    WidgetPreset(
        "show_name", "Show name (from console)", "Console",
        "The show file loaded on the desk.",
        _text("{eos.show.name}", size=0.026, anchor=Anchor.BOTTOM_RIGHT),
        ("eos.show.name",),
    ),
    WidgetPreset(
        "selected_channels", "Selected channels", "Console",
        "What is selected on the desk right now.",
        lambda i: TextWidget(
            i, "Chan {eos.chan.active}",
            placement=Placement(Anchor.BOTTOM_CENTER),
            style=TextStyle(size=0.026), z_order=50,
            visibility=VisibilityRule(only_when_present="eos.chan.active"),
        ),
        ("eos.chan.active",),
    ),
    WidgetPreset(
        "console_lamp", "Console status lamp", "Console",
        "A dot that is green while the console is talking and red when it is "
        "not.",
        lambda i: StatusWidget(
            i, "eos.connected",
            placement=Placement(Anchor.TOP_RIGHT, offset_y=0.06),
            style=TextStyle(size=0.030), z_order=60,
            on_text="Eos", off_text="Eos lost",
        ),
        ("eos.connected",),
    ),

    # ---------------------------------------------------------------- time
    WidgetPreset(
        "clock_24", "Wall clock (24 hour)", "Time",
        "The time of day.",
        _text("{clock.wall}", size=0.034, anchor=Anchor.TOP_RIGHT, z_order=30),
        ("clock.wall",),
    ),
    WidgetPreset(
        "clock_12", "Wall clock (12 hour)", "Time",
        "The time of day, with am/pm.",
        _text("{clock.wall12}", size=0.034, anchor=Anchor.TOP_RIGHT, z_order=30),
        ("clock.wall12",),
    ),
    WidgetPreset(
        "clock_date", "Clock with date", "Time",
        "The time of day, large, with the date written out small beneath it. "
        "One box, so the two stay lined up whatever size you make them.",
        lambda i: TextWidget(
            i, "{clock.wall}\n{clock.date_long}",
            emphasise_first_line=True,
            placement=Placement(Anchor.TOP_RIGHT),
            style=TextStyle(size=0.034, align=Align.RIGHT),
            z_order=30,
        ),
        ("clock.wall", "clock.date_long"),
    ),
    WidgetPreset(
        "date", "Date", "Time",
        "Today's date, as 2026-09-08.",
        _text("{clock.date}", size=0.024, anchor=Anchor.TOP_RIGHT),
        ("clock.date",),
    ),
    WidgetPreset(
        "date_long", "Date (written out)", "Time",
        "Today's date, as Tuesday 08 September 2026. Worth having on an "
        "archive recording that will be found years later.",
        _text("{clock.date_long}", size=0.022, anchor=Anchor.TOP_RIGHT),
        ("clock.date_long",),
    ),
    WidgetPreset(
        "elapsed", "Record elapsed", "Time",
        "How long this take has been running.",
        _text("{clock.elapsed}", size=0.030, anchor=Anchor.TOP_CENTER),
        ("clock.elapsed",),
    ),
    WidgetPreset(
        "rec_lamp", "Recording lamp", "Time",
        "A red REC indicator with the elapsed time, shown only while "
        "recording.",
        lambda i: StatusWidget(
            i, "clock.recording",
            placement=Placement(Anchor.TOP_CENTER),
            style=TextStyle(size=0.032), z_order=60,
            on_text="REC {clock.elapsed}", off_text="",
            on_colour=RED, off_colour=Colour(120, 120, 120),
            hide_when_off=True,
        ),
        ("clock.recording", "clock.elapsed"),
    ),
    WidgetPreset(
        "take", "Take number", "Time",
        "Which take this is, counting up each time you stop.",
        _text("Take {clock.take}", size=0.024, anchor=Anchor.TOP_CENTER),
        ("clock.take",),
    ),

    # -------------------------------------------------------------- typed
    WidgetPreset(
        "manual_show", "Show title (typed)", "Typed",
        "The production name, from the Manual tab.",
        _text("{manual.show}", size=0.026, anchor=Anchor.BOTTOM_RIGHT,
              colour=AMBER, only_when_present="manual.show", z_order=40),
        ("manual.show",),
    ),
    WidgetPreset(
        "manual_act_scene", "Act and scene", "Typed",
        "Where you are in the show, from the Manual tab.",
        _text("Act {manual.act}  Sc {manual.scene}", size=0.026,
              anchor=Anchor.BOTTOM_CENTER, only_when_present="manual.act"),
        ("manual.act", "manual.scene"),
    ),
    WidgetPreset(
        "manual_note", "Note", "Typed",
        "A free line you can type at any time, including mid-take.",
        _text("{manual.note}", size=0.026, anchor=Anchor.BOTTOM_CENTER,
              colour=AMBER, only_when_present="manual.note"),
        ("manual.note",),
    ),
    WidgetPreset(
        "custom_text", "Custom text", "Typed",
        "An empty text widget. Write your own template with any bus key -- the "
        "Data Monitor lists everything available.",
        _text("Cue {eos.cue.active.number}", size=0.030, anchor=Anchor.CENTER),
        (),
    ),

    # -------------------------------------------------------------- shapes
    WidgetPreset(
        "backing_strip", "Backing strip", "Shapes",
        "A dark band across the bottom of the frame, drawn behind everything "
        "else. Put the command line, show name or a note on it and they read "
        "as one lower third rather than a row of separate boxes.",
        _backing(width=1.0, height=0.11,
                 placement=Placement(Anchor.BOTTOM_CENTER, margin=0.0),
                 corner_radius=0.0),
        (),
    ),
    WidgetPreset(
        "backing_box", "Backing box", "Shapes",
        "A dark box drawn behind other widgets, to gather a few readouts into "
        "one place on a busy picture.",
        _backing(width=0.30, height=0.15, placement=Placement(Anchor.CENTER),
                 corner_radius=0.010),
        (),
    ),
)


def preset_groups() -> dict[str, list[WidgetPreset]]:
    """Presets grouped for a menu, in the order defined above."""
    groups: dict[str, list[WidgetPreset]] = {}
    for preset in PRESETS:
        groups.setdefault(preset.group, []).append(preset)
    return groups


def get_preset(key: str) -> WidgetPreset | None:
    return next((p for p in PRESETS if p.key == key), None)


def unique_id(base: str, existing: list[str]) -> str:
    """A widget id not already used in this layout."""
    if base not in existing:
        return base
    index = 2
    while f"{base}{index}" in existing:
        index += 1
    return f"{base}{index}"


def make_widget(preset_key: str, existing_ids: list[str]) -> OverlayWidget | None:
    preset = get_preset(preset_key)
    if preset is None:
        return None
    return preset.build(unique_id(preset.key, existing_ids))
