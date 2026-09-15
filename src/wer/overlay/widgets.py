"""Overlay widgets: the things that draw on the video.

A widget takes its information from the DataBus and renders it. It never imports
connection code and never knows a console exists -- it knows keys.

Qt lives here (QPainter does the drawing), which is why ``wer.overlay`` is one
of the two packages allowed to import it.

Caching
-------
The rule: re-render a widget only when its bound bus keys change or it's
mid-animation -- do not redraw every widget every frame. Each widget therefore
holds a rendered RGBA image and a dirty flag. The compositor asks
:meth:`OverlayWidget.image` for it, which re-renders only when something has
actually changed. At 30 fps with an Eos command line updating per keystroke,
that is the difference between redrawing text a few times a second and doing it
thirty times a second for no visible difference.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
)

from wer.core.databus import MISSING, DataBus
from wer.overlay.geometry import Placement, Rect, scale_to_height
from wer.overlay.sample import SAMPLE_SOURCE
from wer.overlay.style import GREEN, RED, Align, BoxStyle, Colour, TextStyle

log = logging.getLogger(__name__)

__all__ = [
    "OverlayWidget",
    "TextWidget",
    "ImageWidget",
    "CueWidget",
    "FadeBarWidget",
    "CueTimerWidget",
    "CUE_TIMER_STARTS",
    "PanelWidget",
    "StatusWidget",
    "VisibilityRule",
]


def _blank(value: object) -> bool:
    """Nothing to show: never published, or published as "there is none".

    Eos distinguishes these and so does the bus -- None is silence, "" is the
    desk stating that the cue list has run out. The overlay does not: both mean
    it has no number to draw. Note that a cue numbered 0 is not blank, which is
    why this is not just ``not value``.
    """
    return value is None or (isinstance(value, str) and not value.strip())


def _qcolour(colour: Colour) -> QColor:
    return QColor(colour.r, colour.g, colour.b, colour.a)


#: A running fade's time-left key. A text widget showing one counts it down
#: between the desk's reports; see TextWidget._countdown_values.
_REMAINING = ".cue.active.remaining"


def _fading_key(remaining_key: str) -> str:
    """The "is it fading" key beside a time-left key, in the same namespace."""
    return remaining_key[: -len(_REMAINING)] + ".cue.active.fading"


@dataclass
class VisibilityRule:
    """When a widget should be on screen: its visibility conditions."""

    #: Show only while this bus key is truthy. e.g. "eos.cue.active.fading".
    only_when: str | None = None
    #: Show only while this key is non-empty.
    only_when_present: str | None = None
    #: Hide this many seconds after the last change to any bound key. 0 = never.
    hide_after: float = 0.0
    #: Fade in/out duration in seconds.
    fade: float = 0.25

    def evaluate(self, bus: DataBus, last_change: float) -> bool:
        if self.only_when is not None and not bus.value(self.only_when):
            return False
        if self.only_when_present is not None:
            value = bus.value(self.only_when_present)
            if value is None or str(value).strip() == "":
                return False
        if self.hide_after > 0 and last_change > 0:
            if time.perf_counter() - last_change > self.hide_after:
                return False
        return True


class OverlayWidget(ABC):
    """Base class: placement, styling, caching, visibility."""

    def __init__(
        self,
        widget_id: str,
        *,
        placement: Placement | None = None,
        style: TextStyle | None = None,
        z_order: int = 0,
        visible: bool = True,
        locked: bool = False,
        visibility: VisibilityRule | None = None,
        max_width: float = 0.0,
    ) -> None:
        self.id = widget_id
        self.placement = placement or Placement()
        self.style = style or TextStyle()
        self.z_order = z_order
        self.visible = visible
        self.locked = locked
        self.visibility = visibility or VisibilityRule()
        #: The widest this widget may draw, as a fraction of the frame width.
        #: 0 means as wide as the frame margins allow. See _width_budget.
        self.max_width = max_width

        self._image: QImage | None = None
        self._dirty = True
        #: Width of the frame this widget is being drawn onto, when the caller
        #: knows it. Text bounds itself against this; see _width_budget.
        self._frame_width: int | None = None
        #: When the soonest-expiring key this widget reads stops being
        #: trustworthy, so the cache can invalidate itself without waiting for
        #: a change notification that will never arrive.
        self._expires_at: float | None = None
        #: The (height, width) the cached image was drawn for, or None before
        #: the first render. Tested instead of the image itself, because a
        #: render that drew nothing is as cached as one that drew something.
        self._rendered_for: tuple[int, int | None] | None = None
        self._last_change = 0.0
        #: None until a frame has decided it; see effective_opacity.
        self._opacity: float | None = None

    # ---------------------------------------------------------------- binding

    @property
    @abstractmethod
    def content_keys(self) -> list[str]:
        """Keys this widget's *content* reads. Subclasses implement this."""

    @property
    def bus_keys(self) -> list[str]:
        """Every key this widget reads, content and visibility together.

        Visibility conditions count. A widget shown "only when
        eos.cue.active.fading" depends on that key exactly as much as one that
        prints it, and if the compositor does not subscribe to it the widget is
        never marked dirty when it changes. Combining them here means a new
        widget type cannot forget.
        """
        keys = list(self.content_keys)
        for key in (self.visibility.only_when, self.visibility.only_when_present):
            if key:
                keys.append(key)
        # Order-preserving deduplication: a widget conditioned on the same key
        # it displays would otherwise subscribe twice and re-render twice.
        return list(dict.fromkeys(keys))

    def notify_changed(self) -> None:
        """Called when a bound key changes. Marks the cache stale."""
        self._dirty = True
        self._last_change = time.perf_counter()

    def mark_dirty(self) -> None:
        self._dirty = True

    # --------------------------------------------------------------- geometry

    @abstractmethod
    def _render(self, bus: DataBus, frame_height: int) -> QImage | None:
        """Draw the widget. Return None when there is nothing to show."""

    def _soonest_expiry(self, bus: DataBus, rendered_at: float) -> float | None:
        """When the first key the last render could have read as live stops
        being trustworthy.

        None if there is no such key, which is most of the bus.

        ``rendered_at`` is when that render began. A key already expired by
        then is left out: the render read it later still, and bus.value and
        bus.render judge staleness at the moment of reading, so it was drawn as
        missing and there is nothing left to wait for. Left in, its deadline
        stayed in the past for good -- every frame found it due, redrew the
        widget, rebuilt its blend layers and put the same deadline back. A desk
        that went quiet mid-fade did that thirty times a second, on the capture
        thread, until it spoke again.

        Judged from the start of the render and not the end, because a key can
        expire part-way through, after it was read as live. Its deadline stays
        in, the next frame draws it as missing, and only then is it left out.
        Judged from the end, its last live value would have stayed on the
        picture. The test is the entry's own is_stale, the one the reads used,
        so the two cannot disagree at the boundary.

        A key coming back from expiry is announced by the bus instead:
        DataBus.publish notifies subscribers when an expired value is
        republished, even unchanged.
        """
        soonest: float | None = None
        for key in self.bus_keys:
            entry = bus.get(key)
            if entry is None or entry.stale_after is None:
                continue
            if entry.is_stale(rendered_at):
                continue
            deadline = entry.updated_at + entry.stale_after
            if soonest is None or deadline < soonest:
                soonest = deadline
        return soonest

    def describe(self) -> str:
        """One sentence on what this widget puts on the recording.

        Shown in the editor beside its settings. Written for someone who did
        not build it and is looking at a list of widget names wondering which
        is which -- so it says what appears on screen, not what class it is.
        """
        return "A widget on the overlay."

    def wants_repaint(self, bus: DataBus) -> bool:
        """True when this widget has something to redraw that no bus change
        will announce.

        Almost nothing does: the cache exists because re-rendering text at 4K
        every frame is what made compositing expensive in the first place, and
        a widget is normally redrawn only when a key it reads changes. The
        exception is anything that moves continuously between updates -- a bar
        interpolating across a fade the console only reports once a second.
        """
        return False

    def _width_budget(self) -> int | None:
        """How wide this widget may draw, or None when nobody has said.

        Text was sized from the text alone, so a long Eos command line drew an
        image wider than the picture: the end ran off frame with nothing to say
        it had been cut, and the render cost -- which is quadratic in the
        character count, on the capture thread -- was unbounded with it.

        The budget is the frame less the placement margin at each side, so a
        widget that bounds itself to it lands fully inside the picture.

        ``max_width`` narrows it further. The margins keep a widget on the
        picture but know nothing about its neighbours: a command line anchored
        bottom-left and a show name anchored bottom-right both grow towards the
        middle, and a long enough command runs straight through the show name,
        one drawn over the other. Capping each at part of the frame keeps them
        apart however much is typed.
        """
        if not self._frame_width:
            return None
        margin = max(0.0, min(0.45, self.placement.margin))
        budget = max(1, int(self._frame_width * (1.0 - 2.0 * margin)))
        if self.max_width > 0:
            budget = min(budget, max(1, int(self.max_width * self._frame_width)))
        return budget

    def _elide_mode(self) -> Qt.TextElideMode:
        """Which end of over-long text to cut: the one furthest from the anchor.

        Bounding the width decides not only how much text survives but WHICH
        half, and getting that backwards throws away the part the operator was
        watching. Before the bound existed, an over-wide image simply hung off
        the frame at the end it grew towards: a left-anchored widget lost its
        tail off the right edge, a right-anchored one lost its head off the
        left. Eliding right everywhere kept the head in both cases -- so for a
        right-anchored widget (the shipped show-name caption) the ellipsis
        replaced exactly the part that used to be readable.

        Cutting at the far end preserves what was visible and keeps the anchored
        edge stable, which is what makes a growing command line readable at all.
        A centre-anchored widget grows both ways and lost both ends before;
        nothing keeps a floating middle, and the two ends carry more than the
        middle does, so that one is cut in the middle.
        """
        side = self.placement.anchor.horizontal
        if side >= 1.0:
            return Qt.TextElideMode.ElideLeft
        if side <= 0.0:
            return Qt.TextElideMode.ElideRight
        return Qt.TextElideMode.ElideMiddle

    def image(
        self, bus: DataBus, frame_height: int, frame_width: int | None = None
    ) -> QImage | None:
        """Cached render. Re-renders when dirty, resized, newly stale, or when
        the widget says it has moved on its own.

        The staleness half is not an optimisation detail. A widget is marked
        dirty when a key CHANGES, and a key quietly passing its expiry is not
        a change -- no notification fires. So the cache went on returning the
        last fresh render for ever: the bus would say "Cue --" while the
        overlay still drew "Cue 58", pixel for pixel, into every frame of the
        recording. The desk going quiet on a live show is precisely when that
        happens, and it is the failure that leaves no trace of itself.

        A render that drew nothing is cached like any other. "Nothing to draw"
        used to read as "never drawn", so it was repeated on every frame: a
        watermark whose file was missing went back to the disk thirty times a
        second inside the capture thread's read loop, and a label over an
        unlabelled cue, an idle fade bar or a lamp hidden while off redrew
        itself only to arrive at nothing again.
        """
        if self._expires_at is not None and time.perf_counter() >= self._expires_at:
            self._dirty = True
        if not self._dirty and self.wants_repaint(bus):
            self._dirty = True
        if self._dirty or (frame_height, frame_width) != self._rendered_for:
            self._frame_width = frame_width
            # Cleared before the render, not after. Keys change on connection
            # threads while this one draws, and a change landing mid-render
            # sets the flag again so the next frame picks it up. Cleared
            # afterwards, it wiped that notification out and the widget kept
            # what the render had read: a command line one change behind until
            # the next change, which once a command has been entered can be
            # the next time anyone touches the desk.
            self._dirty = False
            began = time.perf_counter()
            try:
                self._image = self._render(bus, frame_height)
            except Exception:  # noqa: BLE001 - a bad widget must not kill the frame
                log.exception("Widget %s failed to render", self.id)
                self._image = None
                # Tried again next frame, as a failed render always has been:
                # a fault that clears should not leave the widget off the
                # picture until one of its keys happens to change.
                self._dirty = True
            self._rendered_for = (frame_height, frame_width)
            # Recomputed from what the render just read, so the next expiry
            # schedules the next repaint.
            self._expires_at = self._soonest_expiry(bus, began)
        return self._image

    def rect(self, bus: DataBus, frame_width: int, frame_height: int) -> Rect | None:
        image = self.image(bus, frame_height, frame_width)
        if image is None or image.isNull():
            return None
        return self.placement.resolve(
            frame_width, frame_height, image.width(), image.height()
        )

    # ------------------------------------------------------------- visibility

    def effective_opacity(self, bus: DataBus) -> float:
        """Opacity right now, including any fade in or out.

        Returns 0.0 when the widget should not be drawn at all, which lets the
        compositor skip it without a separate visibility call.
        """
        if not self.visible:
            return 0.0
        wanted = self.visibility.evaluate(bus, self._last_change)
        fade = max(0.0, self.visibility.fade)

        if fade <= 0.0 or self._opacity is None:
            # No fade to run, or nothing on screen yet to fade from. A widget
            # that has not been drawn since its layout went up starts where
            # its condition says it should be. Every widget used to start
            # fully visible, so one whose condition was false faded OUT over
            # its first seven frames: a note widget with no note burned a
            # fading "--" into the recording every time its layout was
            # switched to, duplicated or reset.
            self._opacity = 1.0 if wanted else 0.0
        else:
            # A frame-rate-independent approach: step toward the target by the
            # elapsed fraction of the fade. Simpler than tracking start times
            # and correct enough for a quarter-second fade.
            step = 1.0 / max(1.0, fade * 30.0)
            target = 1.0 if wanted else 0.0
            if self._opacity < target:
                self._opacity = min(target, self._opacity + step)
            elif self._opacity > target:
                self._opacity = max(target, self._opacity - step)

        return self._opacity * max(0.0, min(1.0, self.style.opacity))

    @property
    def is_animating(self) -> bool:
        """True while mid-fade, so the compositor keeps blending it."""
        return self._opacity is not None and 0.0 < self._opacity < 1.0

    def settle_fade(self) -> None:
        """Forget any fade in progress; the next frame starts it afresh.

        Called when a layout goes up. Widget objects live on in the layout
        between switches, holding whatever opacity they had when it last
        left the screen, and a note cleared since then would otherwise fade
        out all over again, from full, on the first frames back.
        """
        self._opacity = None


class TextWidget(OverlayWidget):
    """The workhorse: a format string over bus keys.

    ``template`` is resolved against the bus, so ``Cue {eos.cue.active.number}``
    becomes ``Cue 98``. A key that is missing or stale renders as ``--`` rather
    than blanking, because a widget has to say when it has no data instead of
    quietly showing the last value it saw.

    ``emphasise_first_line`` draws every line after the first in the smaller
    supporting face. It is what lets one widget be "21:57:31" large with
    "Tuesday 08 September 2026" small beneath it. Two widgets stacked by
    offset do the same job badly: they overlap the moment either one's size
    is changed, and sit in two ragged boxes of different widths even when they
    do not. One widget with two lines is one box, and cannot drift apart.
    """

    def describe(self) -> str:
        keys = self.content_keys
        if not keys:
            return "Fixed text. Nothing in it changes while you record."
        if len(keys) == 1:
            return f"Live text showing {keys[0]}."
        return "Live text showing " + ", ".join(keys) + "."

    def __init__(
        self,
        widget_id: str,
        template: str,
        *,
        emphasise_first_line: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(widget_id, **kwargs)
        self.template = template
        self.emphasise_first_line = emphasise_first_line
        #: The text drawn last time, so a running fade countdown is redrawn
        #: when its figure moves and left alone when it has not.
        self._drawn_text: str | None = None
        #: (template, the time-left keys in it), so they are looked for once
        #: per template rather than once per frame.
        self._countdown_cache: tuple[str, list[str]] = ("", [])

    @property
    def content_keys(self) -> list[str]:
        keys = DataBus.template_keys(self.template)
        # A countdown also reads whether its fade is still running, and has to
        # hear the moment it stops; see _countdown_values.
        keys += [_fading_key(key) for key in self._countdown_keys()]
        return list(dict.fromkeys(keys))

    def _countdown_keys(self) -> list[str]:
        """The time-left keys in this template, which count down between reports."""
        template, keys = self._countdown_cache
        if template != self.template:
            keys = [
                key for key in DataBus.template_keys(self.template)
                if key.endswith(_REMAINING)
            ]
            self._countdown_cache = (self.template, keys)
        return keys

    def _countdown_values(self, bus: DataBus) -> dict[str, str]:
        """Time left in each running fade, counted down since the desk's last report.

        The desk reports the time left about once a second -- 5.0, 4.0, 3.0,
        a second apart, on a captured five-second fade -- and a text widget
        redraws only when a key it reads changes. So a fade countdown sat on
        each figure for a second while the video ran at thirty, the stepping
        the cue block's bar had before _FadeClock.

        It now counts down from the desk's own last figure by the time since
        that report arrived, never below zero, and starts again from every new
        report, so it can never be further from the desk than one report's
        worth. Only while the fade is running and the figure is fresh: a stale
        or missing figure still renders as missing, through the bus.
        """
        keys = self._countdown_keys()
        if not keys:
            return {}
        values: dict[str, str] = {}
        now = time.perf_counter()
        for key in keys:
            entry = bus.get(key)
            if entry is None or entry.value is None or entry.is_stale(now):
                continue
            if not bus.value(_fading_key(key)):
                continue
            try:
                reported = float(entry.value)
            except (TypeError, ValueError):
                continue
            left = max(0.0, reported - (now - entry.updated_at))
            # One decimal, as the desk sends it, so the figure keeps its look.
            values[key] = f"{left:.1f}"
        return values

    def text(self, bus: DataBus) -> str:
        template = self.template
        for key, value in self._countdown_values(bus).items():
            template = template.replace("{" + key + "}", value)
        return bus.render(template)

    def wants_repaint(self, bus: DataBus) -> bool:
        """Redraw a running fade countdown whenever the figure it shows moves.

        About ten times a second during a fade, for a figure in tenths; never
        for text with no countdown in it, which stays cached as before.
        """
        if not self._countdown_keys() or self._drawn_text is None:
            return False
        return self.text(bus) != self._drawn_text

    def _elide_mode(self) -> Qt.TextElideMode:
        """A command line keeps its end, however it is anchored.

        The end of an Eos command line is the part being typed, which is the
        reason text is bounded at all (see render_text_block). The general
        rule cuts the end furthest from the anchor, and for a command line in
        its usual bottom-left corner that is exactly the typed end: a long
        command kept its beginning and put the ellipsis where the keystrokes
        were landing.
        """
        if ".cmdline." in self.template:
            return Qt.TextElideMode.ElideLeft
        return super()._elide_mode()

    def _render(self, bus: DataBus, frame_height: int) -> QImage | None:
        text = self.text(bus)
        self._drawn_text = text
        if not text.strip():
            return None
        return render_text_block(
            text, self.style, frame_height, max_width=self._width_budget(),
            elide=self._elide_mode(),
            # On a single line there is nothing after the first, so this draws
            # exactly as it would without the setting.
            secondary_from_line=1 if self.emphasise_first_line else None,
        )


class _FadeClock:
    """Fade progress, advanced locally between the console's reports.

    Eos reports fade progress at about 1 Hz (the capture is
    tests/fixtures/eos/fade-progress.jsonl), which is far too coarse to drive a
    bar at 30 fps -- it would step visibly once a second while the video ran
    smoothly, and a 2.9-second cue would get three updates in total. So the bar
    advances locally from the elapsed time and resyncs on every console update.

    Kept apart from any one widget because two of them draw a fade: the cue
    block's bar and the fade bar on its own. Two copies of this logic would
    sooner or later get a fix in one and not the other, and show up as two
    bars in one layout disagreeing about the same fade -- burned into a
    recording, where nobody can tell afterwards which of them was right.
    """

    #: How far the bar must move before redrawing is worth it. A cue block's
    #: bar is a few hundred pixels wide at 1080p, so a thousandth of its length
    #: is well under one pixel -- below this the redraw would change nothing a
    #: viewer could see, and every frame of a six-second fade would pay for it.
    REPAINT_STEP = 0.0015

    def __init__(self) -> None:
        self._sync_progress = 0.0
        self._sync_at = 0.0
        self._sync_duration = 0.0
        #: The progress actually drawn last time, so the bar can be redrawn
        #: when it has moved and left alone when it has not. None when the
        #: last render drew no bar.
        self.drawn: float | None = None

    def resync(self) -> None:
        """Start again from the next report. Called when a bound key changes."""
        self._sync_at = 0.0

    def progress(self, bus: DataBus, namespace: str) -> float:
        """Fade progress, advanced locally between the console's ~1 Hz updates."""
        reported = bus.value(f"{namespace}.cue.active.progress")
        if reported is None:
            return 1.0
        reported = float(reported)
        duration = bus.value(f"{namespace}.cue.active.duration") or 0.0

        now = time.perf_counter()
        if self._sync_at == 0.0 or reported != self._sync_progress:
            self._sync_progress = reported
            self._sync_duration = float(duration)
            self._sync_at = now
            return reported

        if reported >= 1.0 or self._sync_duration <= 0:
            return reported

        # Advance by however much of the total fade has elapsed since the last
        # report, and never overshoot: running ahead of the console and then
        # snapping back would look worse than lagging slightly.
        elapsed = now - self._sync_at
        return min(1.0, reported + elapsed / self._sync_duration)

    def wants_repaint(self, bus: DataBus, namespace: str) -> bool:
        """True while a fade runs and the bar has moved far enough to see."""
        if not bus.value(f"{namespace}.cue.active.fading"):
            return False
        if self.drawn is None:
            return True
        return abs(self.progress(bus, namespace) - self.drawn) >= self.REPAINT_STEP


class CueWidget(OverlayWidget):
    """Cue number, label, pending cue, and a fade-progress bar.

    The progress bar interpolates between the console's roughly once-a-second
    reports rather than stepping with them; see _FadeClock, which the fade bar
    on its own uses as well.

    ``show_previous`` puts the cue you came from above the heading, so the
    block reads top to bottom in the order the cues run: Last, Cue, Next.
    ``show_list`` drops the "1/" from the heading -- on a show with one cue
    list it sits in front of every number in the recording and says nothing.
    """

    def describe(self) -> str:
        parts = ["the live cue number and its label"]
        if self.show_previous:
            parts.insert(0, "the last cue")
        if self.show_pending:
            parts.append("the next cue")
        if self.show_progress:
            parts.append("a bar that fills as the fade runs")
        return "The cue block: " + ", ".join(parts) + "."

    def __init__(
        self,
        widget_id: str,
        *,
        namespace: str = "eos",
        show_pending: bool = True,
        show_progress: bool = True,
        show_label: bool = True,
        show_previous: bool = False,
        show_list: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(widget_id, **kwargs)
        self.ns = namespace
        self.show_pending = show_pending
        self.show_progress = show_progress
        self.show_label = show_label
        self.show_previous = show_previous
        self.show_list = show_list
        self._fade = _FadeClock()

    @property
    def content_keys(self) -> list[str]:
        keys = [
            f"{self.ns}.cue.active.number",
            f"{self.ns}.cue.active.list",
            f"{self.ns}.cue.active.label",
            f"{self.ns}.cue.active.progress",
            f"{self.ns}.cue.active.duration",
            f"{self.ns}.cue.active.fading",
        ]
        if self.show_previous:
            # Only when drawn, so the editor's "Reads:" line tells the truth.
            keys += [
                f"{self.ns}.cue.previous.number",
                f"{self.ns}.cue.previous.label",
            ]
        if self.show_pending:
            keys += [
                f"{self.ns}.cue.pending.number",
                f"{self.ns}.cue.pending.label",
            ]
        return keys

    def notify_changed(self) -> None:
        super().notify_changed()
        self._fade.resync()

    def wants_repaint(self, bus: DataBus) -> bool:
        """Redraw while a fade is actually moving.

        The console reports fade progress about once a second. The bar has
        always interpolated between those reports, but nothing ever asked it
        to redraw, so it stepped once a second while the video ran at thirty --
        which is what "jittery" looks like. It now repaints when the
        interpolated value has moved far enough to shift a pixel, and not
        otherwise.
        """
        if not self.show_progress:
            return False
        return self._fade.wants_repaint(bus, self.ns)

    def interpolated_progress(self, bus: DataBus) -> float:
        """Fade progress, advanced locally between the console's ~1 Hz updates."""
        return self._fade.progress(bus, self.ns)

    def _render(self, bus: DataBus, frame_height: int) -> QImage | None:
        ns = self.ns
        number = bus.value(f"{ns}.cue.active.number")
        if _blank(number):
            # Never connected, the console has gone, or the desk has said in
            # so many words that there is no active cue -- it sends an empty
            # cue text to mean that, and the parser publishes the empty string
            # rather than None so it can be told apart from silence. Both end
            # up here. Checking only for None rendered the empty case as the
            # word "Cue" with nothing after it, which reads as a widget that
            # has broken rather than a cue list that has finished.
            return render_text_block(
                f"Cue {MISSING}", self.style, frame_height,
                max_width=self._width_budget(), elide=self._elide_mode(),
            )

        cue_list = bus.value(f"{ns}.cue.active.list") or ""
        label = bus.value(f"{ns}.cue.active.label") or ""

        lines: list[str] = []
        if self.show_previous:
            previous = bus.value(f"{ns}.cue.previous.number")
            if not _blank(previous):
                previous_label = bus.value(f"{ns}.cue.previous.label") or ""
                lines.append(f"Last  {previous}  {previous_label}".rstrip())
        heading_line = len(lines)
        if self.show_list and cue_list:
            lines.append(f"Cue {cue_list}/{number}")
        else:
            lines.append(f"Cue {number}")
        if self.show_label and label:
            lines.append(str(label))
        if self.show_pending:
            pending = bus.value(f"{ns}.cue.pending.number")
            if not _blank(pending):
                pending_label = bus.value(f"{ns}.cue.pending.label") or ""
                lines.append(f"Next  {pending}  {pending_label}".rstrip())

        progress = None
        if self.show_progress:
            fading = bus.value(f"{ns}.cue.active.fading")
            if fading:
                progress = self._fade.progress(bus, ns)
        self._fade.drawn = progress

        return render_text_block(
            "\n".join(lines),
            self.style,
            frame_height,
            progress=progress,
            # The live cue is the large line wherever it lands. Under a Last
            # line, "everything after the first line is small" would shrink it
            # and leave the cue you have already left as the biggest thing on
            # screen. With no Last line, the heading is first and this is the
            # rule as it always was.
            primary_line=heading_line if heading_line else None,
            secondary_from_line=1,
            max_width=self._width_budget(),
            elide=self._elide_mode(),
        )


class FadeBarWidget(OverlayWidget):
    """The cue block's fade bar on its own, for a layout that wants to see a
    fade run without another block of cue text on the picture.

    Length and thickness are separate fractions of the frame's width and
    height: a bar is a shape with no font size to scale from, and one meant to
    run half the frame has to do so at any aspect ratio. It interpolates with
    the same _FadeClock as the cue block, so a layout carrying both cannot
    show them disagreeing about one fade.

    ``hide_when_idle`` draws nothing between fades. Turned off, the empty track
    stays up, which makes the bar easier to line up in the editor.
    """

    def describe(self) -> str:
        between = "gone" if self.hide_when_idle else "an empty track"
        return f"A bar that fills as the cue fade runs, and is {between} between fades."

    def __init__(
        self,
        widget_id: str,
        *,
        namespace: str = "eos",
        width: float = 0.30,
        height: float = 0.012,
        hide_when_idle: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(widget_id, **kwargs)
        self.ns = namespace
        #: Length as a fraction of the frame width.
        self.width = width
        #: Thickness as a fraction of the frame height. Never drawn under 2 px,
        #: so a thumbnail-sized preview still shows a bar.
        self.height = height
        self.hide_when_idle = hide_when_idle
        self._fade = _FadeClock()

    @property
    def content_keys(self) -> list[str]:
        return [
            f"{self.ns}.cue.active.progress",
            f"{self.ns}.cue.active.duration",
            f"{self.ns}.cue.active.fading",
        ]

    def notify_changed(self) -> None:
        super().notify_changed()
        self._fade.resync()

    def wants_repaint(self, bus: DataBus) -> bool:
        """Redraw while the fade is moving: the cue block's rule, from the same clock."""
        return self._fade.wants_repaint(bus, self.ns)

    def _render(self, bus: DataBus, frame_height: int) -> QImage | None:
        if bus.value(f"{self.ns}.cue.active.fading"):
            progress = self._fade.progress(bus, self.ns)
            self._fade.drawn = progress
        else:
            self._fade.drawn = None
            if self.hide_when_idle:
                return None
            progress = 0.0

        width, height = _shape_size(
            self.width, self.height, frame_height, self._frame_width, min_height=2
        )
        image = QImage(width, height, QImage.Format.Format_RGBA8888)
        image.fill(0)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        try:
            _draw_progress(
                painter, progress, self.style, x=0, y=0, width=width, height=height
            )
        finally:
            painter.end()
        return image


#: Where a time-in-cue widget starts counting: (setting, what the editor calls it).
CUE_TIMER_STARTS: tuple[tuple[str, str], ...] = (
    ("fade_start", "The start of the fade into the cue"),
    ("fade_end", "The end of the fade into the cue"),
)

#: How long after a cue's number arrives a "not fading" report still belongs to
#: that cue. The desk sends a cue and its fade state in one message, which the
#: parser publishes key by key, microseconds apart.
_SAME_REPORT = 0.25

#: The longest a time-in-cue widget may go without looking and still trust the
#: next change of cue it sees as the moment that cue fired. It looks every
#: frame while its layout is on screen. After a longer gap it was off screen or
#: had no picture, and the desk may have re-sent the cue since, which moves the
#: timestamp the start would be read from.
_WATCH_GAP = 0.5


def _format_elapsed(seconds: float) -> str:
    """M:SS, or H:MM:SS from an hour on: a tech can sit in one cue that long."""
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class CueTimerWidget(OverlayWidget):
    """How long the live cue has been up.

    ``count_from`` is "fade_start", the moment the cue was fired, or
    "fade_end", the moment its fade into the cue finished. Counting from the
    end holds at 0:00 while that fade is still running.

    The desk sends no time a cue was fired, so the start is the moment Wer saw
    the live cue change: the bus stamps every value as it arrives. That is only
    true of a change seen as it happened. A cue already up when Wer connected,
    or one that changed while this widget was off screen, had no picture, or
    could not reach the desk, shows "--" until the next cue fires. Counting
    from whenever Wer happened to notice would burn a confident wrong time into
    the recording. The end of the fade is the first "not fading" report for the
    cue, which the desk sends as the fade lands.
    """

    #: What a preview shows. A sample bus never fires a cue.
    PREVIEW_SECONDS = 84.0

    def describe(self) -> str:
        since = "its fade finished" if self.count_from == "fade_end" else "it was fired"
        return f"How long the live cue has been up, counted from when {since}."

    def __init__(
        self,
        widget_id: str,
        *,
        namespace: str = "eos",
        count_from: str = "fade_start",
        prefix: str = "In cue ",
        **kwargs,
    ) -> None:
        super().__init__(widget_id, **kwargs)
        self.ns = namespace
        self.count_from = count_from
        #: Text in front of the time. Empty for the time alone.
        self.prefix = prefix
        self._cue: tuple[object, object] | None = None
        self._started_at: float | None = None
        self._ended_at: float | None = None
        #: When the widget last looked at the desk, and whether it saw a live
        #: cue then. A change of cue is trusted only straight after a clear look.
        self._looked_at: float | None = None
        self._saw_cue = False
        self._drawn_text: str | None = None

    @property
    def count_from(self) -> str:
        return self._count_from

    @count_from.setter
    def count_from(self, value: str) -> None:
        # Anything unrecognised, such as a hand-edited show file, counts from
        # the start of the fade rather than failing to load.
        self._count_from = value if value in dict(CUE_TIMER_STARTS) else "fade_start"

    @property
    def content_keys(self) -> list[str]:
        return [
            f"{self.ns}.cue.active.number",
            f"{self.ns}.cue.active.list",
            f"{self.ns}.cue.active.fading",
        ]

    def _follow(self, bus: DataBus, now: float) -> bool:
        """Keep up with the desk. True when the live cue's start is known."""
        watched = self._looked_at is not None and now - self._looked_at <= _WATCH_GAP
        self._looked_at = now
        entry = bus.get(f"{self.ns}.cue.active.number")
        # "is False", not falsy: a bus that has never carried the connected
        # flag says nothing either way about the desk.
        disconnected = bus.value(f"{self.ns}.connected") is False
        if entry is None or entry.is_stale(now) or _blank(entry.value) or disconnected:
            self._saw_cue = False
            return False
        cue = (bus.value(f"{self.ns}.cue.active.list"), entry.value)
        if cue != self._cue:
            trusted = self._cue is not None and self._saw_cue and watched
            self._cue = cue
            self._started_at = entry.updated_at if trusted else None
            self._ended_at = None
        self._saw_cue = True
        if self._started_at is None:
            return False
        if self._ended_at is None:
            fading = bus.get(f"{self.ns}.cue.active.fading")
            if (
                fading is not None
                and fading.value is not None
                and not fading.value
                and not fading.is_stale(now)
                and fading.updated_at >= self._started_at - _SAME_REPORT
            ):
                self._ended_at = max(fading.updated_at, self._started_at)
        return True

    def _timer_text(self, bus: DataBus) -> str:
        now = time.perf_counter()
        number = bus.get(f"{self.ns}.cue.active.number")
        if number is not None and number.source_connection_id == SAMPLE_SOURCE:
            return self.prefix + _format_elapsed(self.PREVIEW_SECONDS)
        if not self._follow(bus, now):
            return self.prefix + MISSING
        if self.count_from == "fade_end":
            if self._ended_at is None:
                if bus.value(f"{self.ns}.cue.active.fading") is None:
                    return self.prefix + MISSING
                return self.prefix + _format_elapsed(0.0)
            return self.prefix + _format_elapsed(now - self._ended_at)
        return self.prefix + _format_elapsed(now - self._started_at)

    def wants_repaint(self, bus: DataBus) -> bool:
        """Redraw when the time shown has moved on: once a second, not every frame."""
        return self._drawn_text is not None and self._timer_text(bus) != self._drawn_text

    def _render(self, bus: DataBus, frame_height: int) -> QImage | None:
        text = self._timer_text(bus)
        self._drawn_text = text
        if not text.strip():
            return None
        return render_text_block(
            text, self.style, frame_height, max_width=self._width_budget(),
            elide=self._elide_mode(),
        )


class ImageWidget(OverlayWidget):
    """A picture over the video: a logo, a watermark, a production mark.

    Sized as a fraction of frame height like every other widget, so the same
    layout looks right whether the camera is 1080p or 4K. Aspect ratio is kept.

    The file is read once and cached at the size it is drawn. A missing or
    unreadable file draws nothing and says so in the log rather than putting a
    broken-image box in the recording -- but ``problem`` carries the reason so
    the editor can show it, because a watermark that silently fails to appear
    is exactly the kind of thing nobody notices until afterwards.
    """

    #: Seconds a render waits before trying again a file that would not load,
    #: doubling after each failure up to RETRY_LONGEST. See _load.
    RETRY_FIRST = 5.0
    RETRY_LONGEST = 60.0

    def describe(self) -> str:
        if not self.path:
            return "A picture over the video. Choose an image file to show one."
        return (
            f"A picture over the video: {Path(self.path).name}, drawn at "
            f"{self.height:.0%} of the frame height."
        )

    def __init__(
        self,
        widget_id: str,
        path: str = "",
        *,
        height: float = 0.10,
        opacity: float = 1.0,
        **kwargs,
    ) -> None:
        super().__init__(widget_id, **kwargs)
        self.path = path
        #: Drawn height as a fraction of the frame height.
        self.height = height
        #: 0..1, multiplied with the widget's own opacity.
        self.image_opacity = opacity
        self._source: QImage | None = None
        self._source_path = ""
        self.problem = ""
        #: (path, problem) already written to the log, so a bad file is
        #: reported once rather than once per glance. See _note_problem.
        self._reported: tuple[str, str] = ("", "")
        #: When a render may next try a file that would not load, or None when
        #: nothing is waiting on one. See _load.
        self._retry_at: float | None = None
        self._retry_delay = self.RETRY_FIRST
        #: Overridable so a test can move time on rather than sleep through a
        #: retry delay.
        self._clock = time.perf_counter

    @property
    def content_keys(self) -> list[str]:
        return []

    def _note_problem(self, problem: str) -> None:
        """Record why the file cannot be used, logging it once per path.

        A failed load is only remembered until its next retry -- a logo
        restored on the show server should start working without anyone
        retyping the path -- so this runs again on each retry and every time
        the editor asks. Logging inside it wrote a fresh
        warning for every click on a broken watermark, into the log that gets
        attached to the recording. It needs saying once.
        """
        self.problem = problem
        if self._reported != (self.path, problem):
            self._reported = (self.path, problem)
            log.warning("Watermark %s: %s", self.id, problem)

    def _load(self, *, retry_now: bool = False) -> QImage | None:
        """Read the file, once per path.

        A file that would not load is tried again, but not on every call.
        Renders run on the capture thread, once a frame, and a failure used to
        go straight back to the disk each time: a stat thirty times a second
        for a missing file, a whole decode attempt for one that is there but
        will not read, and for a share that has gone away, however long
        Windows takes to say so. How long that is, and whether every attempt
        waits as long as the first, is not established here -- so the wait
        between attempts starts at RETRY_FIRST and doubles up to
        RETRY_LONGEST. That bounds what an unreachable path can cost the
        capture thread over a long take, and a logo put back on the show
        server is still on the picture within a minute.

        ``retry_now`` skips the wait. It is for check(): someone in the editor
        is looking at this widget and asking, on the main thread.
        """
        if self._source_path == self.path:
            if self._source is not None:
                return self._source
            if (
                not retry_now
                and self._retry_at is not None
                and self._clock() < self._retry_at
            ):
                return None
        else:
            # A different file. How often the last one failed says nothing
            # about this one.
            self._retry_delay = self.RETRY_FIRST
        self._source = None
        self._source_path = self.path
        self.problem = ""
        self._retry_at = None
        if not self.path:
            # Not a fault: a watermark is added before it is pointed anywhere.
            self.problem = "No image chosen"
            return None
        file = Path(self.path)
        try:
            present = file.is_file()
        except OSError as exc:
            # is_file() swallows only the "not there" errors and re-raises the
            # rest -- a permissions refusal, for one. Raised out of the render,
            # that was a traceback in the log every frame as well as a trip to
            # the disk, and the editor's caption had nothing to say.
            self._failed(f"Cannot open {self.path}: {exc.strerror or exc}")
            return None
        if not present:
            self._failed(f"No file at {self.path}")
            return None
        image = QImage(str(file))
        if image.isNull():
            self._failed(f"{file.name} is not an image Wer can read")
            return None
        self._retry_delay = self.RETRY_FIRST
        self._source = image.convertToFormat(QImage.Format.Format_ARGB32)
        return self._source

    def _failed(self, problem: str) -> None:
        """Say why the file cannot be used, and hold off before trying it again.

        The wait is counted from when the attempt returned, so an attempt that
        blocked for a long time is not followed straight away by another.
        """
        self._note_problem(problem)
        self._retry_at = self._clock() + self._retry_delay
        self._retry_delay = min(self._retry_delay * 2.0, self.RETRY_LONGEST)

    def wants_repaint(self, bus: DataBus) -> bool:
        """True when a file that would not load is due another try.

        Nothing on the bus announces a file coming back, and a render that drew
        nothing is cached like any other (see OverlayWidget.image), so this is
        what brings the render round to try again.
        """
        return (
            self._source is None
            and self._retry_at is not None
            and self._clock() >= self._retry_at
        )

    def check(self) -> str:
        """Read the file now and return what is wrong with it, or "".

        For the editor. ``problem`` is otherwise only written during a render,
        which first happens when the camera starts -- and the overlay is
        normally built before that, so the one caption meant to catch a
        watermark that will not appear had nothing to say.

        This does decode the file, on the main thread: 52 ms measured for a
        4000x4000 PNG, once per path, and it is the same read the first
        composite would do anyway. Dropping the decode and testing only that
        the file exists would not make it safe against a show-server share that
        has gone away -- the file test is what blocks there, not the decode --
        and it would stop the caption saying that a file is present but not
        readable, which is half of what it exists to catch.

        It does not wait out a retry delay (see _load): someone is looking at
        the widget and asking. And when the widget had no picture, the render
        is told to run again, so a file put back and confirmed here reaches the
        recording now rather than at the capture thread's next attempt.
        """
        had_picture = self._source is not None and self._source_path == self.path
        self._load(retry_now=True)
        if not had_picture:
            self.mark_dirty()
        return self.problem

    def reload(self) -> None:
        """Forget the cached file, so a changed image is picked up.

        ``problem`` goes with the cache. It describes the file that was last
        loaded, and leaving it behind is how the editor came to warn about a
        path the widget no longer has -- and, worse, to say nothing at all
        about the one it does. Any wait before a retry goes too: it was earned
        by a file that may not be the one there now.
        """
        self._source = None
        self._source_path = ""
        self.problem = ""
        self._retry_at = None
        self._retry_delay = self.RETRY_FIRST
        self.mark_dirty()

    def _render(self, bus: DataBus, frame_height: int) -> QImage | None:
        source = self._load()
        if source is None or source.isNull():
            return None

        target_height = max(1, int(round(self.height * frame_height)))
        scaled = source.scaledToHeight(
            target_height, Qt.TransformationMode.SmoothTransformation
        )
        if self.image_opacity >= 0.999:
            return scaled

        # Multiply the alpha rather than painting onto a translucent layer:
        # compositing already does per-pixel alpha, so this keeps one path.
        faded = QImage(scaled.size(), QImage.Format.Format_ARGB32)
        faded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(faded)
        painter.setOpacity(max(0.0, min(1.0, self.image_opacity)))
        painter.drawImage(0, 0, scaled)
        painter.end()
        return faded


class PanelWidget(OverlayWidget):
    """A plain shape drawn behind other widgets: a lower-third strip, a box.

    Every text widget brings its own backing box, sized to its own text. That
    suits a lone readout and not a row of them: three captions along the
    bottom of the frame get three boxes of three widths, with the stage
    showing through the gaps between. A panel is one shape of a set size for
    them to sit on. Give it a lower layer than the widgets on it.

    It is drawn with the style's box settings -- fill, corner radius, border --
    whether or not the box is switched on. A panel is nothing but its box, and
    one that vanished when "Box" was unticked would look broken, not plain.

    Width and height are separate fractions of the frame's width and height,
    not the font-size rule text follows, because a strip has to reach both
    edges at any aspect ratio. Worth sizing with some thought: blending cost
    follows area (see wer.overlay.compositor), and at 4K a full-width strip
    11% tall is about 0.9 Mpx a frame -- nearly twice the compositor's figure
    for a typical five-widget layout on its own.
    """

    def describe(self) -> str:
        shape = "strip" if self.width >= 0.995 else "box"
        return (
            f"A plain {shape} {self.width:.0%} of the frame wide and "
            f"{self.height:.0%} tall, for putting other widgets on."
        )

    def __init__(
        self,
        widget_id: str,
        *,
        width: float = 1.0,
        height: float = 0.10,
        **kwargs,
    ) -> None:
        super().__init__(widget_id, **kwargs)
        #: Fraction of the frame width.
        self.width = width
        #: Fraction of the frame height.
        self.height = height

    @property
    def content_keys(self) -> list[str]:
        return []

    def _render(self, bus: DataBus, frame_height: int) -> QImage | None:
        width, height = _shape_size(
            self.width, self.height, frame_height, self._frame_width
        )
        box = self.style.box
        # A border is stroked centred on the shape's edge, so the shape is
        # inset by half the border each side; otherwise the outer half of the
        # border would fall outside the image and never be seen.
        border = (
            scale_to_height(box.border_width, frame_height, minimum=1)
            if box.border_width > 0 else 0
        )
        image = QImage(width, height, QImage.Format.Format_RGBA8888)
        image.fill(0)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        try:
            _draw_box(painter, box, frame_height, border, width, height)
        finally:
            painter.end()
        return image


class StatusWidget(OverlayWidget):
    """A conditional lamp: one appearance when a key is true, another when not.

    A connection health dot and a conditional lamp are the same thing with
    different bindings: a console-lost warning is ``eos.connected``, a REC
    indicator is ``clock.recording``.

    Both texts are templates, so a lamp can say "REC 0:12:04" rather than just
    "REC". A dot is drawn alongside, because at a glance colour reads faster
    than words.
    """

    def describe(self) -> str:
        return (
            f"A lamp that changes colour with {self.key or 'a bus key'} -- "
            "green when it is true, red when it is not."
        )

    def __init__(
        self,
        widget_id: str,
        key: str,
        *,
        on_text: str = "",
        off_text: str = "",
        on_colour: Colour | None = None,
        off_colour: Colour | None = None,
        show_dot: bool = True,
        #: When the condition is false, draw nothing at all rather than a
        #: greyed-out lamp. Right for a REC indicator, wrong for a connection
        #: lamp -- the whole point of that one is being visible when it fails.
        hide_when_off: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(widget_id, **kwargs)
        self.key = key
        self.on_text = on_text
        self.off_text = off_text
        self.on_colour = on_colour or GREEN
        self.off_colour = off_colour or RED
        self.show_dot = show_dot
        self.hide_when_off = hide_when_off

    @property
    def content_keys(self) -> list[str]:
        keys = [self.key]
        for template in (self.on_text, self.off_text):
            keys += DataBus.template_keys(template)
        return list(dict.fromkeys(keys))

    def is_on(self, bus: DataBus) -> bool:
        value = bus.value(self.key)
        if isinstance(value, str):
            # Bus values arriving as strings from a hand-edited show file or a
            # protocol that has no booleans.
            return value.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(value)

    def _render(self, bus: DataBus, frame_height: int) -> QImage | None:
        on = self.is_on(bus)
        if not on and self.hide_when_off:
            return None

        text = bus.render(self.on_text if on else self.off_text)
        colour = self.on_colour if on else self.off_colour
        if self.show_dot:
            text = f"● {text}".strip()
        if not text.strip():
            return None

        style = replace(self.style, colour=colour)
        return render_text_block(
            text, style, frame_height, max_width=self._width_budget(),
            elide=self._elide_mode(),
        )


# --------------------------------------------------------------------- drawing


def render_text_block(
    text: str,
    style: TextStyle,
    frame_height: int,
    *,
    progress: float | None = None,
    secondary_from_line: int | None = None,
    primary_line: int | None = None,
    max_width: int | None = None,
    elide: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
) -> QImage | None:
    """Render text (and an optional progress bar) to a transparent RGBA image.

    Sized to its content, so the compositor can anchor it without knowing
    anything about fonts -- but never wider than ``max_width``, which the
    caller sets from the frame. Text longer than that is elided with an
    ellipsis instead of being drawn off the edge of the picture, at the end
    ``elide`` names -- see OverlayWidget._elide_mode for why the end matters
    as much as the width does.

    Both halves of that matter. A 200-character Eos command line rendered a
    3185 px image onto a 1920 px frame, so the end of the line -- the part
    being typed -- was simply not in the recording, with nothing to show it
    had been cut. And the drawing cost grows with the square of the character
    count (measured at 1080p: 24 ms at 120 characters, 264 ms at 400, 1.4 s at
    800), all of it inside the capture thread's read loop, where no frames are
    being taken while it runs. Bounding the width bounds both.

    Which lines are large: ``secondary_from_line`` draws that line and every
    one after it in a smaller, slightly dimmer supporting face, which suits a
    block whose main line comes first. ``primary_line`` keeps just that one
    line large and makes every other line secondary, for a block whose main
    line is not first -- the cue stack, with the last cue above the live one.
    If both are passed, ``primary_line`` wins.
    """
    lines = text.split("\n")

    def is_secondary(index: int) -> bool:
        if primary_line is not None:
            return index != primary_line
        return secondary_from_line is not None and index >= secondary_from_line
    font_px = style.scaled_size(frame_height)

    font = QFont(style.family)
    font.setPixelSize(font_px)
    font.setWeight(QFont.Weight(style.weight))
    font.setItalic(style.italic)
    if style.letter_spacing:
        font.setLetterSpacing(
            QFont.SpacingType.AbsoluteSpacing, style.letter_spacing * font_px
        )

    # A smaller face for supporting lines, so the cue number stays the thing the
    # eye lands on first.
    secondary = QFont(font)
    secondary.setPixelSize(max(8, int(font_px * 0.62)))
    secondary.setWeight(QFont.Weight(max(400, style.weight - 200)))

    def font_for(index: int) -> QFont:
        return secondary if is_secondary(index) else font

    metrics = [QFontMetrics(font_for(i)) for i in range(len(lines))]
    line_heights = [
        int(m.height() * style.line_spacing) for m in metrics
    ]

    padding = scale_to_height(style.box.padding, frame_height, minimum=0) if style.box.enabled else 0
    outline = max(0.0, style.outline_width) * font_px
    shadow = style.shadow_offset * font_px if style.shadow else 0.0
    bleed = int(outline + shadow) + 2

    if max_width is not None:
        # Everything the image spends on itself before a glyph is drawn. At
        # least a pixel is left for the text: a limit narrower than the box's
        # own padding used to skip the cut altogether and draw the whole line,
        # the opposite of what a limit is for.
        room = max(1, int(max_width) - 2 * padding - 2 * bleed)
        lines = [
            metric.elidedText(line, elide, room)
            for metric, line in zip(metrics, lines)
        ]

    text_width = max(
        (m.horizontalAdvance(line) for m, line in zip(metrics, lines)), default=0
    )
    text_height = sum(line_heights)

    bar_height = int(font_px * 0.16) if progress is not None else 0
    bar_gap = int(font_px * 0.18) if progress is not None else 0

    width = text_width + 2 * padding + 2 * bleed
    height = text_height + 2 * padding + 2 * bleed + bar_height + bar_gap
    if width <= 0 or height <= 0:
        return None

    image = QImage(int(width), int(height), QImage.Format.Format_RGBA8888)
    image.fill(0)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    try:
        if style.box.enabled:
            _draw_box(painter, style.box, frame_height, bleed, width, height)

        y = bleed + padding
        for index, line in enumerate(lines):
            line_font = font_for(index)
            painter.setFont(line_font)
            metric = metrics[index]
            baseline = y + metric.ascent()

            x = bleed + padding
            if style.align is Align.CENTER:
                x = (width - metric.horizontalAdvance(line)) / 2
            elif style.align is Align.RIGHT:
                x = width - bleed - padding - metric.horizontalAdvance(line)

            colour = style.colour
            if is_secondary(index):
                colour = colour.with_alpha(int(colour.a * 0.82))

            _draw_line(painter, line, x, baseline, line_font, style, colour, outline, shadow)
            y += line_heights[index]

        if progress is not None:
            _draw_progress(
                painter, progress, style,
                x=bleed + padding,
                y=height - bleed - padding - bar_height,
                width=width - 2 * (bleed + padding),
                height=bar_height,
            )
    finally:
        painter.end()

    return image


def _draw_box(
    painter: QPainter, box: BoxStyle, frame_height: int,
    bleed: int, width: float, height: float,
) -> None:
    radius = scale_to_height(box.corner_radius, frame_height, minimum=0)
    rect = QRectF(bleed / 2, bleed / 2, width - bleed, height - bleed)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(_qcolour(box.fill))
    painter.drawRoundedRect(rect, radius, radius)
    if box.border_width > 0:
        pen = QPen(_qcolour(box.border))
        pen.setWidthF(scale_to_height(box.border_width, frame_height, minimum=1))
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)


def _draw_line(
    painter: QPainter, text: str, x: float, baseline: float,
    font: QFont, style: TextStyle, colour: Colour,
    outline: float, shadow: float,
) -> None:
    """Draw one line with its shadow and outline.

    A QPainterPath rather than drawText, because an outline needs the glyph
    outlines and drawText cannot stroke them.
    """
    path = QPainterPath()
    path.addText(x, baseline, font, text)

    if shadow > 0:
        shadow_path = QPainterPath()
        shadow_path.addText(x + shadow, baseline + shadow, font, text)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_qcolour(style.shadow_colour))
        painter.drawPath(shadow_path)

    if outline > 0:
        pen = QPen(_qcolour(style.outline))
        pen.setWidthF(outline)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(_qcolour(colour))
    painter.drawPath(path)


def _draw_progress(
    painter: QPainter, progress: float, style: TextStyle,
    x: float, y: float, width: float, height: float,
) -> None:
    progress = max(0.0, min(1.0, progress))
    radius = height / 2
    painter.setPen(Qt.PenStyle.NoPen)

    painter.setBrush(QColor(255, 255, 255, 60))
    painter.drawRoundedRect(QRectF(x, y, width, height), radius, radius)

    if progress > 0:
        painter.setBrush(_qcolour(style.colour))
        painter.drawRoundedRect(
            QRectF(x, y, max(height, width * progress), height), radius, radius
        )


def _shape_size(
    width: float, height: float, frame_height: int, frame_width: int | None,
    *, min_height: int = 1,
) -> tuple[int, int]:
    """Pixel size of a shape given as fractions of the frame's width and height.

    The frame width is not always known -- the cache can be asked for an image
    with only a height -- and 16:9 is the likeliest shape to guess. Both sides
    are capped at the frame: nothing past its edge is ever seen, and a mistyped
    50 in a hand-edited show file would otherwise ask the capture thread for a
    picture fifty frames wide.
    """
    across = frame_width or round(frame_height * 16 / 9)
    pixels_wide = max(1, min(across, round(width * across)))
    pixels_high = max(min_height, min(frame_height, round(height * frame_height)))
    return pixels_wide, pixels_high
