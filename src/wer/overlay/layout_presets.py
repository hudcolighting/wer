"""Ready-made layouts: whole overlays to start a new layout from.

presets.py is the catalogue of single widgets; this is the catalogue of layouts
built out of them. A blank layout asks whoever is building it to know which of
thirty widgets they want and where each one should go, at a desk in the
afternoon, before the console is even connected. Most recordings are one of a
handful of jobs -- a tech, a programming session, a notes review, an archive of
the run -- and each of those wants much the same things in much the same
places. Starting from one of these and moving what does not suit is quicker
than building from nothing, and much harder to get wrong.

Every layout here is checked by tests/test_layout_presets.py against the sample
bus at 720p, 1080p and 4K: nothing runs off the picture, and nothing is drawn
over anything else except text sitting wholly on its own backing strip. Every
one is checked again with a 200-character command line and with labels, titles
and notes far longer than any real one, which is why every widget here that can
grow sideways into a neighbour is capped with ``max_width`` to the lane it was
given. The uncapped ones are all on the three a show ships with, and each
either reads a key of fixed length or has the width of the picture to itself;
default_layouts() has that detail. Sizes and offsets are fractions of the frame
like everything else, so what was checked at one resolution is what gets
recorded at another.

Tech, Minimal and None are built by calling default_layouts() rather than
copied out of it. They are the three a new show file starts with, and they are
also what "Reset to default" puts back, and two definitions of one layout would
sooner or later disagree. Archive shipped as a default until those three took
over; it is built here now, so a show still carrying an Archive layout can
still be reset, and anyone who picks it out of this catalogue gets what it has
always been.

No QWidget code: the dialog that offers these is wer.ui.layout_preset_dialog.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from wer.overlay.geometry import Anchor, Placement
from wer.overlay.style import AMBER, RED, Align, BoxStyle, Colour, TextStyle
from wer.overlay.widgets import (
    CueWidget,
    FadeBarWidget,
    OverlayWidget,
    PanelWidget,
    StatusWidget,
    TextWidget,
    VisibilityRule,
)

if TYPE_CHECKING:
    from wer.overlay.layout import Layout

__all__ = [
    "LayoutPreset",
    "LAYOUT_PRESETS",
    "get_layout_preset",
    "layout_preset_named",
    "unique_layout_name",
]


@dataclass(frozen=True)
class LayoutPreset:
    """One ready-made layout in the New Layout dialog."""

    #: Stable identifier, for code and tests. Never shown, never renamed.
    key: str
    #: The name the layout it creates is given, e.g. "Programming".
    name: str
    #: One line, for a list.
    summary: str
    #: Two or three sentences: who it is for and what is on it.
    description: str
    #: Makes the widgets. Must build new objects on every call -- see layout().
    build: Callable[[], list[OverlayWidget]]

    def layout(self, name: str | None = None) -> "Layout":
        """A new Layout of this preset's widgets, called ``name`` or the preset's own name.

        The widgets are built fresh every time, never handed out from a list
        kept here. Two layouts made from one preset would otherwise share
        widget objects, and dragging the cue block in "Programming 2" would
        move it in "Programming" as well -- including in whichever of the two
        is being recorded at the time.
        """
        from wer.overlay.layout import Layout

        return Layout(name=self.name if name is None else name, widgets=self.build())


# --------------------------------------------------------------------- styles


def _style(
    size: float,
    *,
    colour: Colour | None = None,
    align: Align = Align.LEFT,
    box: bool = True,
) -> TextStyle:
    """A text style at ``size``, keeping the legibility defaults unless told.

    ``box=False`` drops the backing box only. The outline and shadow stay,
    because they are what keeps white text readable when a followspot crosses
    behind it; a box is only safe to lose where something else is the backing,
    which today means a widget sitting wholly on a panel.
    """
    style = TextStyle(size=size, align=align)
    if colour is not None:
        style = style.with_colour(colour)
    if not box:
        style = replace(style, box=BoxStyle(enabled=False))
    return style


#: The dot and text of a lamp that is off but still worth showing: "Not
#: recording" before the show goes up. Grey rather than red, because red on a
#: REC lamp means the opposite.
_OFF_GREY = Colour(170, 170, 170)


def _shipped(name: str) -> Callable[[], list[OverlayWidget]]:
    """Build one of the layouts default_layouts() ships, by name."""

    def build() -> list[OverlayWidget]:
        # Imported here, not at the top. layout.py is where layouts are read
        # out of a show file, and the natural place for "Reset to default" to
        # look a preset up -- with both imports at the top, that is a cycle.
        from wer.overlay.layout import default_layouts

        return next(layout.widgets for layout in default_layouts() if layout.name == name)

    return build


# ------------------------------------------------------------------- builders
#
# Stacked widgets are placed by offset, as fractions of the frame height. Each
# offset is the height of whatever sits between the widget and its edge, plus a
# gap of about 0.85% (9 px at 1080p): enough that two boxes read as two things
# lined up rather than one block with a seam through it, and enough that font
# hinting, which does not scale exactly with the frame, cannot close it at 720p
# or 4K.
#
# Widgets that share a band across the frame are given lanes with max_width
# that cannot meet, however long the text in them gets. The margins alone keep
# a widget on the picture, not off its neighbour.


def _programming() -> list[OverlayWidget]:
    return [
        CueWidget(
            "cue",
            show_previous=True,
            placement=Placement(Anchor.TOP_LEFT),
            style=_style(0.042),
            z_order=10,
            max_width=0.42,
        ),
        TextWidget(
            "chan",
            "Chan {eos.chan.active}",
            placement=Placement(Anchor.BOTTOM_LEFT, offset_y=-0.119),
            style=_style(0.034),
            z_order=20,
            visibility=VisibilityRule(only_when_present="eos.chan.active"),
            max_width=0.96,
        ),
        # The largest thing on the picture, and the whole width to itself.
        TextWidget(
            "cmdline",
            "{eos.cmdline.text}",
            placement=Placement(Anchor.BOTTOM_LEFT),
            style=_style(0.046),
            z_order=30,
            visibility=VisibilityRule(only_when_present="eos.cmdline.text"),
            max_width=0.96,
        ),
        TextWidget(
            "clock",
            "{clock.wall}",
            placement=Placement(Anchor.TOP_RIGHT),
            style=_style(0.040),
            z_order=40,
            max_width=0.30,
        ),
        StatusWidget(
            "console",
            "eos.connected",
            on_text="Eos",
            off_text="Eos lost",
            placement=Placement(Anchor.TOP_RIGHT, offset_y=0.105),
            style=_style(0.030),
            z_order=60,
            max_width=0.30,
        ),
    ]


def _performance() -> list[OverlayWidget]:
    return [
        # Without the list number: on a one-list show "1/" sits in front of
        # every cue in the recording and says nothing to someone watching the
        # calling. The fade has its own bar along the bottom.
        CueWidget(
            "cue",
            show_list=False,
            show_progress=False,
            placement=Placement(Anchor.TOP_LEFT),
            style=_style(0.062),
            z_order=10,
            max_width=0.34,
        ),
        TextWidget(
            "countdown",
            "Fade {eos.cue.active.remaining}s",
            placement=Placement(Anchor.TOP_LEFT, offset_y=0.261),
            style=_style(0.036, colour=AMBER),
            z_order=20,
            visibility=VisibilityRule(only_when="eos.cue.active.fading"),
            max_width=0.34,
        ),
        TextWidget(
            "act",
            "Act {manual.act}  Sc {manual.scene}",
            placement=Placement(Anchor.TOP_CENTER),
            style=_style(0.034),
            z_order=30,
            visibility=VisibilityRule(only_when_present="manual.act"),
            max_width=0.24,
        ),
        TextWidget(
            "clock",
            "{clock.wall}\nElapsed {clock.elapsed}",
            emphasise_first_line=True,
            placement=Placement(Anchor.TOP_RIGHT),
            style=_style(0.046, align=Align.RIGHT),
            z_order=40,
            max_width=0.30,
        ),
        FadeBarWidget(
            "fade",
            width=0.96,
            height=0.010,
            placement=Placement(Anchor.BOTTOM_CENTER),
            style=TextStyle(colour=AMBER),
            z_order=50,
        ),
    ]


def _lower_third() -> list[OverlayWidget]:
    # The strip is 12% of the height: the least that holds a two-line cue and
    # label with room above and below. Blending cost follows area (see
    # PanelWidget), and at 4K this is about 1.1 Mpx a frame -- the price of the
    # look, paid only by whoever picks it.
    #
    # Everything on it is placed by its centre, so it stays centred on the
    # strip whatever size the text is made. The clock and title sit a few
    # pixels above and below the cue and label lines they face across the
    # picture: exactly level, the clock's shadow would run into the title, and
    # a difference of 8 px is not something the eye can compare from one end
    # of the frame to the other. Text any larger than this no longer fits two
    # separate lines on the strip at 720p once rounding has had its way.
    return [
        PanelWidget(
            "strip",
            width=1.0,
            height=0.12,
            placement=Placement(Anchor.BOTTOM_CENTER, margin=0.0),
            style=TextStyle(box=BoxStyle(fill=Colour(0, 0, 0, 170), corner_radius=0.0)),
            z_order=1,
        ),
        CueWidget(
            "cue",
            show_list=False,
            show_pending=False,
            show_progress=False,
            placement=Placement(Anchor.MIDDLE_LEFT, offset_y=0.44),
            style=_style(0.034, box=False),
            z_order=10,
            max_width=0.46,
        ),
        TextWidget(
            "clock",
            "{clock.wall}",
            placement=Placement(Anchor.MIDDLE_RIGHT, offset_y=0.417),
            style=_style(0.034, align=Align.RIGHT, box=False),
            z_order=20,
            max_width=0.30,
        ),
        # Its own widget, not a second line of the clock's: a title nobody has
        # typed was never published, and would sit under the clock as "--".
        # The size of the cue block's label line, which it faces.
        TextWidget(
            "show",
            "{manual.show}",
            placement=Placement(Anchor.MIDDLE_RIGHT, offset_y=0.474),
            style=_style(0.021, colour=AMBER, align=Align.RIGHT, box=False),
            z_order=30,
            visibility=VisibilityRule(only_when_present="manual.show"),
            max_width=0.40,
        ),
    ]


def _archive() -> list[OverlayWidget]:
    # The layout that shipped as a default beside Tech until the three Hudson
    # runs took over. Its widgets are spelled out here rather than fetched from
    # default_layouts(), which no longer builds it, so a show file still
    # carrying an Archive layout has something to be reset to.
    return [
        CueWidget(
            "cue",
            show_pending=False,
            show_progress=False,
            placement=Placement(Anchor.TOP_LEFT),
            style=_style(0.040),
            z_order=10,
            max_width=0.46,
        ),
        TextWidget(
            "clock",
            "{clock.wall}",
            placement=Placement(Anchor.TOP_RIGHT),
            style=_style(0.030),
            z_order=20,
            max_width=0.30,
        ),
    ]


def _notes_session() -> list[OverlayWidget]:
    return [
        CueWidget(
            "cue",
            placement=Placement(Anchor.TOP_LEFT),
            style=_style(0.044),
            z_order=10,
            max_width=0.34,
        ),
        # Still shown between takes, greyed, so the take number the next
        # recording will get is on screen before anyone presses record.
        StatusWidget(
            "rec",
            "clock.recording",
            on_text="REC {clock.elapsed}   Take {clock.take}",
            off_text="Take {clock.take}",
            on_colour=RED,
            off_colour=_OFF_GREY,
            placement=Placement(Anchor.TOP_CENTER),
            style=_style(0.032),
            z_order=60,
            max_width=0.28,
        ),
        TextWidget(
            "clock",
            "{clock.wall}",
            placement=Placement(Anchor.TOP_RIGHT),
            style=_style(0.036),
            z_order=30,
            max_width=0.30,
        ),
        TextWidget(
            "act",
            "Act {manual.act}  Sc {manual.scene}",
            placement=Placement(Anchor.TOP_RIGHT, offset_y=0.099),
            style=_style(0.028),
            z_order=40,
            visibility=VisibilityRule(only_when_present="manual.act"),
            max_width=0.30,
        ),
        TextWidget(
            "note",
            "{manual.note}",
            placement=Placement(Anchor.BOTTOM_CENTER),
            style=_style(0.046, colour=AMBER),
            z_order=50,
            visibility=VisibilityRule(only_when_present="manual.note"),
            max_width=0.92,
        ),
    ]


def _documentation() -> list[OverlayWidget]:
    return [
        # One widget, two lines, so the date cannot drift out from under the
        # title. Deliberately not hidden when no title has been typed: on an
        # archive, "--" where the show's name should be is a thing to notice
        # tonight, not to discover when the remount opens the file.
        TextWidget(
            "show",
            "{manual.show}\n{clock.date_long}",
            emphasise_first_line=True,
            placement=Placement(Anchor.TOP_LEFT),
            style=_style(0.040),
            z_order=10,
            max_width=0.60,
        ),
        TextWidget(
            "act",
            "Act {manual.act}  Sc {manual.scene}",
            placement=Placement(Anchor.TOP_LEFT, offset_y=0.141),
            style=_style(0.028),
            z_order=20,
            visibility=VisibilityRule(only_when_present="manual.act"),
            max_width=0.60,
        ),
        CueWidget(
            "cue",
            show_pending=False,
            show_progress=False,
            placement=Placement(Anchor.BOTTOM_LEFT),
            style=_style(0.036),
            z_order=30,
            max_width=0.60,
        ),
        TextWidget(
            "clock",
            "{clock.wall}",
            placement=Placement(Anchor.BOTTOM_RIGHT),
            style=_style(0.030),
            z_order=40,
            max_width=0.30,
        ),
    ]


def _system_check() -> list[OverlayWidget]:
    # Nothing here waits for a value before it appears. On every other layout
    # a widget with nothing to say gets out of the way; on this one, a feed
    # that has not arrived has to show as "--", or it looks exactly like a
    # corner that was always empty -- the one thing this layout exists to catch.
    return [
        StatusWidget(
            "console",
            "eos.connected",
            on_text="Eos connected",
            off_text="Eos not connected",
            placement=Placement(Anchor.TOP_LEFT),
            style=_style(0.032),
            z_order=60,
            max_width=0.40,
        ),
        StatusWidget(
            "rec",
            "clock.recording",
            on_text="REC {clock.elapsed}",
            off_text="Not recording",
            on_colour=RED,
            off_colour=_OFF_GREY,
            placement=Placement(Anchor.TOP_LEFT, offset_y=0.092),
            style=_style(0.032),
            z_order=60,
            max_width=0.40,
        ),
        TextWidget(
            "take",
            "Take {clock.take}",
            placement=Placement(Anchor.TOP_LEFT, offset_y=0.184),
            style=_style(0.032),
            z_order=50,
            max_width=0.40,
        ),
        TextWidget(
            "clock",
            "{clock.wall}\n{clock.date_long}",
            emphasise_first_line=True,
            placement=Placement(Anchor.TOP_RIGHT),
            style=_style(0.040, align=Align.RIGHT),
            z_order=40,
            max_width=0.40,
        ),
        TextWidget(
            "cue",
            "{eos.cue.active.text}",
            placement=Placement(Anchor.BOTTOM_LEFT, offset_y=-0.190),
            style=_style(0.032),
            z_order=20,
            max_width=0.96,
        ),
        TextWidget(
            "chan",
            "Chan {eos.chan.active}",
            placement=Placement(Anchor.BOTTOM_LEFT, offset_y=-0.098),
            style=_style(0.032),
            z_order=20,
            max_width=0.96,
        ),
        TextWidget(
            "cmdline",
            "{eos.cmdline.text}",
            placement=Placement(Anchor.BOTTOM_LEFT),
            style=_style(0.036),
            z_order=30,
            max_width=0.96,
        ),
    ]


# ------------------------------------------------------------------ catalogue


LAYOUT_PRESETS: tuple[LayoutPreset, ...] = (
    LayoutPreset(
        "tech", "Tech",
        "Everything for a tech: cue, next cue, fade, show name, clock, date and record time.",
        "The layout a new show file opens on, and what you want while teching. "
        "The cue block with the next cue and a fade bar top left, the show name "
        "top centre, the clock and date top right with the record time under "
        "them, and bottom left the seconds left in the running cue with how "
        "long that cue has been up just above it.",
        _shipped("Tech"),
    ),
    LayoutPreset(
        "minimal", "Minimal",
        "The cue number, the record time and the show name in three corners, and nothing else.",
        "One of the three a new show file starts with, for footage where the "
        "overlay should be as easy to ignore as possible. The cue number bottom "
        "left, the record time bottom right and the show name top left, each on "
        "a backing box dark enough to read over a lit stage.",
        _shipped("Minimal"),
    ),
    LayoutPreset(
        "none", "None",
        "Nothing on the picture at all: the footage on its own.",
        "One of the three a new show file starts with, for a stretch that has "
        "to be recorded clean. Nothing is drawn, so what is written to the file "
        "is exactly what the camera sent. Because it ships, it has a hotkey "
        "from the first run, and taking the overlay off for a few minutes does "
        "not mean a trip through the Overlay tab.",
        # Through _shipped like the other two, even though what comes back is
        # always empty: one definition of a shipped layout, so an empty one
        # cannot quietly stop being empty in only one of the two places.
        _shipped("None"),
    ),
    LayoutPreset(
        "archive", "Archive",
        "Just the cue and the clock, for a recording that will be watched.",
        "For a recording people will watch rather than work from, where a "
        "command line flickering through every keystroke is a distraction. The "
        "cue number and label top left and the clock top right, and nothing else.",
        _archive,
    ),
    LayoutPreset(
        "programming", "Programming",
        "The desk up front: a big command line, selected channels and the cue stack.",
        "For a programming or notes session where what is being typed on the "
        "desk matters more than the stage. The command line runs large along "
        "the bottom with the selected channels just above it, the cue stack "
        "(last, live and next, with the fade bar) sits top left, and the clock "
        "and a console lamp that turns red if the desk stops talking sit top right.",
        _programming,
    ),
    LayoutPreset(
        "performance", "Performance",
        "A big cue number, the standby and a fade countdown, for watching back the calling.",
        "For reviewing a run or a performance for cue timing and calling. The "
        "live cue large top left with its label and the cue standing by, a "
        "countdown under it only while a fade runs, act and scene across the "
        "top once you have typed them, the clock and record time top right, and "
        "a thin bar along the bottom that fills as each fade runs.",
        _performance,
    ),
    LayoutPreset(
        "lower_third", "Lower third",
        "A broadcast-style strip along the bottom, for sending to a director or producer.",
        "For a recording going to a director, a producer or anyone outside the "
        "lighting department. One dark strip across the bottom of the picture "
        "with the cue number and label on the left and the clock and show title "
        "on the right, and the stage left clear above it.",
        _lower_third,
    ),
    LayoutPreset(
        "notes_session", "Notes session",
        "Your typed note large and amber, with the cue, REC lamp and take, for a notes review.",
        "For a rehearsal or a notes review. Whatever you type as the note on "
        "the Manual tab appears large and amber at the bottom of the picture, "
        "and only while there is one. The cue block sits top left, the REC lamp "
        "with the record time and take number top centre, and the clock with "
        "act and scene top right.",
        _notes_session,
    ),
    LayoutPreset(
        "documentation", "Documentation",
        "Show title, date, act, scene, cue and time, quietly, for the archive.",
        "For archiving a production so it can be remounted years later, when "
        "nobody remembers which night this was. The show title with the date "
        "written out, and act and scene under it, top left; the cue number and "
        "label bottom left; the clock bottom right. Nothing that flickers.",
        _documentation,
    ),
    LayoutPreset(
        "system_check", "System check",
        "Every feed at once -- console, recording, clock, cue, channels, command line.",
        "For checking everything is arriving before the house opens. The "
        "console lamp, REC lamp and take number top left, the clock and date "
        "top right, and the console's own cue line, the selected channels and "
        "the command line along the bottom. Nothing waits for a value before "
        "it appears, so a feed that has not arrived shows as -- rather than as "
        "an empty corner.",
        _system_check,
    ),
)


def get_layout_preset(key: str) -> LayoutPreset | None:
    """The preset with this key, or None."""
    return next((preset for preset in LAYOUT_PRESETS if preset.key == key), None)


def layout_preset_named(name: str) -> LayoutPreset | None:
    """The preset whose layout is called ``name``, or None.

    For "Reset to default": a layout still carrying a preset's name can be put
    back the way that preset builds it. Exact match only -- "Programming 2" is
    somebody's own copy, and resetting it to Programming would throw away
    whatever made it different.
    """
    return next((preset for preset in LAYOUT_PRESETS if preset.name == name), None)


def unique_layout_name(base: str, existing: Iterable[str]) -> str:
    """``base``, or the first of "base 2", "base 3"... not already in the show.

    A space before the number, unlike widget ids ("clock2"): a layout name is
    read on the switcher and beside its hotkey, and "Programming2" reads as a
    typo. It matters that the name is new at all because LayoutSet.add
    replaces a layout of the same name, silently, and that would be somebody's
    afternoon of work gone.
    """
    taken = set(existing)
    if base not in taken:
        return base
    index = 2
    while f"{base} {index}" in taken:
        index += 1
    return f"{base} {index}"
