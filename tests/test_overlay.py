"""Overlay geometry, widgets and compositing.

The geometry tests are Qt-free and exhaustive, because placement has to be right
the first time and the failure mode -- a layout that drifts when recorded at a
different resolution than it was built at -- is invisible until someone opens the
file afterwards.
"""

from __future__ import annotations

import numpy as np
import pytest

from wer.core.databus import MISSING, DataBus
from wer.overlay.geometry import Anchor, Placement, Rect, scale_to_height
from wer.overlay.style import Colour, TextStyle

HD = (1920, 1080)
UHD = (3840, 2160)


# ------------------------------------------------------------------- geometry


@pytest.mark.parametrize("anchor", list(Anchor))
def test_every_anchor_keeps_the_widget_inside_the_frame(anchor: Anchor) -> None:
    """A widget must never be placed outside the picture by its anchor alone."""
    rect = Placement(anchor).resolve(1920, 1080, 300, 120)
    assert rect.x >= 0 and rect.y >= 0
    assert rect.right <= 1920 and rect.bottom <= 1080


def test_top_left_anchor_puts_the_widget_top_left() -> None:
    rect = Placement(Anchor.TOP_LEFT, margin=0.0).resolve(1920, 1080, 300, 120)
    assert (rect.x, rect.y) == (0, 0)


def test_bottom_right_anchor_puts_the_widget_bottom_right() -> None:
    rect = Placement(Anchor.BOTTOM_RIGHT, margin=0.0).resolve(1920, 1080, 300, 120)
    assert (rect.right, rect.bottom) == (1920, 1080)


def test_centre_anchor_centres_the_widget() -> None:
    rect = Placement(Anchor.CENTER).resolve(1920, 1080, 300, 120)
    assert rect.x == (1920 - 300) // 2
    assert rect.y == (1080 - 120) // 2


def test_a_right_anchored_widget_stays_in_its_corner_as_it_grows() -> None:
    """The reason the anchor also picks the widget's own reference point.

    A command line anchored bottom-right must not slide off frame when the
    operator types something longer -- which is exactly when you want to read it.
    """
    placement = Placement(Anchor.BOTTOM_RIGHT)
    narrow = placement.resolve(1920, 1080, 200, 60)
    wide = placement.resolve(1920, 1080, 900, 60)
    assert narrow.right == wide.right, "right edge moved as the widget grew"
    assert wide.x < narrow.x, "the widget should extend leftward"


def test_margin_pushes_inward_from_whichever_edge() -> None:
    left = Placement(Anchor.TOP_LEFT, margin=0.05).resolve(1000, 1000, 100, 100)
    right = Placement(Anchor.TOP_RIGHT, margin=0.05).resolve(1000, 1000, 100, 100)
    assert left.x == 50
    assert right.right == 950


def test_margin_does_nothing_for_a_centred_anchor() -> None:
    """A centred widget has no edge to be pushed away from."""
    a = Placement(Anchor.CENTER, margin=0.0).resolve(1000, 1000, 100, 100)
    b = Placement(Anchor.CENTER, margin=0.2).resolve(1000, 1000, 100, 100)
    assert a == b


def test_offset_direction_is_the_same_for_every_anchor() -> None:
    """"Move it 2% right" must mean the same thing regardless of anchor."""
    for anchor in Anchor:
        base = Placement(anchor).resolve(1000, 1000, 100, 100)
        moved = Placement(anchor, offset_x=0.02, offset_y=0.03).resolve(
            1000, 1000, 100, 100
        )
        assert moved.x - base.x == 20, f"{anchor} moved the wrong way in x"
        assert moved.y - base.y == 30, f"{anchor} moved the wrong way in y"


@pytest.mark.parametrize("anchor", list(Anchor))
def test_placement_is_proportional_across_resolutions(anchor: Anchor) -> None:
    """A layout built at 1080p must survive 4K unchanged.

    The widget itself scales with height, so the whole arrangement should be
    proportionally identical -- that is what makes the layout resolution-free.
    """
    placement = Placement(anchor, offset_x=0.03, offset_y=-0.02)
    hd = placement.resolve(*HD, 300, 120)
    uhd = placement.resolve(*UHD, 600, 240)  # widget doubles with the frame

    assert hd.x / HD[0] == pytest.approx(uhd.x / UHD[0], abs=0.001)
    assert hd.y / HD[1] == pytest.approx(uhd.y / UHD[1], abs=0.001)


def test_font_size_scales_with_output_height() -> None:
    """The rule: "Font sizes scale with output height too"."""
    style = TextStyle(size=0.045)
    assert style.scaled_size(1080) == 49
    assert style.scaled_size(2160) == 97
    assert style.scaled_size(720) == 32


def test_tiny_font_fractions_do_not_round_to_nothing() -> None:
    """A widget that silently vanishes is worse than one that looks wrong."""
    assert scale_to_height(0.0001, 1080) >= 8


# ----------------------------------------------------------------------- rect


def test_rect_clipping_keeps_indices_inside_the_frame() -> None:
    """A widget dragged off-frame must not index outside the numpy array."""
    clipped = Rect(-50, -20, 200, 100).clipped_to(1920, 1080)
    assert clipped.x == 0 and clipped.y == 0
    assert clipped.width == 150 and clipped.height == 80

    off = Rect(5000, 5000, 100, 100).clipped_to(1920, 1080)
    assert off.is_empty


# --------------------------------------------------------------------- colour


def test_colour_from_hex() -> None:
    assert Colour.from_hex("#ff8800").rgba == (255, 136, 0, 255)
    assert Colour.from_hex("f80").rgba == (255, 136, 0, 255)
    assert Colour.from_hex("#ff880080").rgba == (255, 136, 0, 128)


def test_colour_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        Colour.from_hex("nope")


# -------------------------------------------------------- widgets (needs Qt)


@pytest.fixture()
def bus() -> DataBus:
    bus = DataBus()
    bus.publish("eos.cue.active.list", "1", source_connection_id="eos")
    bus.publish("eos.cue.active.number", "82", source_connection_id="eos")
    bus.publish("eos.cue.active.label", "Triangle transition", source_connection_id="eos")
    bus.publish("eos.cue.pending.number", "86", source_connection_id="eos")
    bus.publish("eos.cmdline.text", "LIVE: Cue 82 : ", source_connection_id="eos")
    return bus


def test_text_widget_resolves_its_template(qt_app, bus: DataBus) -> None:
    from wer.overlay.widgets import TextWidget

    widget = TextWidget("t", "Cue {eos.cue.active.number}")
    assert widget.text(bus) == "Cue 82"
    assert widget.bus_keys == ["eos.cue.active.number"]


def test_text_widget_marks_missing_data_rather_than_blanking(qt_app) -> None:
    from wer.overlay.widgets import TextWidget

    widget = TextWidget("t", "Cue {eos.cue.active.number}")
    assert widget.text(DataBus()) == f"Cue {MISSING}"


def test_widget_only_rerenders_when_a_bound_key_changes(qt_app, bus: DataBus) -> None:
    """Do not redraw every widget every frame."""
    from wer.overlay.widgets import TextWidget

    widget = TextWidget("t", "{eos.cue.active.number}")
    first = widget.image(bus, 1080)
    second = widget.image(bus, 1080)
    assert first is second, "re-rendered without any change"

    widget.notify_changed()
    third = widget.image(bus, 1080)
    assert third is not first, "did not re-render after a change"


def test_widget_rerenders_when_the_output_height_changes(qt_app, bus: DataBus) -> None:
    """Switching from a 1080p preview to a 4K record must resize the text."""
    from wer.overlay.widgets import TextWidget

    widget = TextWidget("t", "{eos.cue.active.number}")
    hd = widget.image(bus, 1080)
    uhd = widget.image(bus, 2160)
    assert uhd.height() > hd.height()


def test_a_widget_that_draws_nothing_is_not_redrawn_every_frame(qt_app) -> None:
    """"Nothing to draw" used to read as "never drawn", so a render that
    returned None was repeated on every frame: a label over an unlabelled cue,
    an idle fade bar, a lamp hidden while off -- thirty renders a second on the
    capture thread, each arriving at nothing again."""
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    class Counted(TextWidget):
        renders = 0

        def _render(self, bus, frame_height):
            self.renders += 1
            return super()._render(bus, frame_height)

    bus = DataBus()
    bus.publish("eos.cue.active.label", "", source_connection_id="eos")
    comp = Compositor(bus)
    widget = comp.add(Counted("label", "{eos.cue.active.label}"))

    for _ in range(3):
        assert widget.image(bus, 1080, 1920) is None
    assert widget.renders == 1, f"drew nothing {widget.renders} times over"

    bus.publish("eos.cue.active.label", "Storm builds", source_connection_id="eos")
    assert widget.image(bus, 1080, 1920) is not None, "a label arriving was not drawn"


def test_a_change_that_lands_while_a_widget_is_drawing_is_not_lost(qt_app) -> None:
    """Keys change on connection threads while the capture thread draws. The
    dirty flag was cleared after the render, which wiped out a notification
    that arrived during it: the widget kept what the render had read -- a
    command line one change behind -- until the next change, which once a
    command has been entered can be the next time anyone touches the desk."""
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    bus.publish("eos.cmdline.text", "LIVE: Chan 5 At", source_connection_id="eos")

    class Interrupted(TextWidget):
        interrupted = False

        def _render(self, bus, frame_height):
            image = super()._render(bus, frame_height)
            if not self.interrupted:
                # The desk's next message, between the read and the return.
                self.interrupted = True
                bus.publish("eos.cmdline.text", "LIVE: Chan 5 At Full Enter",
                            source_connection_id="eos")
            return image

    comp = Compositor(bus)
    widget = comp.add(Interrupted("cmd", "{eos.cmdline.text}"))
    widget.image(bus, 1080, 1920)

    settled = TextWidget("cmd", "{eos.cmdline.text}").image(bus, 1080, 1920)
    assert _pixels(widget.image(bus, 1080, 1920)) == _pixels(settled), (
        "still drawing the command line from before the change"
    )


def test_cue_widget_renders_number_label_and_pending(qt_app, bus: DataBus) -> None:
    from wer.overlay.widgets import CueWidget

    widget = CueWidget("cue")
    image = widget.image(bus, 1080)
    assert image is not None and not image.isNull()
    assert image.width() > 0


def test_cue_widget_says_so_when_there_is_no_console(qt_app) -> None:
    """A widget with no data must say so, not draw an empty box."""
    from wer.overlay.widgets import CueWidget

    widget = CueWidget("cue")
    image = widget.image(DataBus(), 1080)
    assert image is not None, "should still render a 'no data' state"


def _pixels(image) -> bytes:
    # The copy is held in a name until the bytes are out of it. Read straight
    # off the temporary, the QImage could be collected while the memoryview
    # into it was still being read, and two renders of the same text came back
    # different about one run in twenty -- a failure that looked like whatever
    # the test happened to be about.
    held = image.copy()
    return held.constBits().tobytes()


def test_a_finished_cue_list_reads_the_same_as_no_console(qt_app) -> None:
    """Eos says "there is nothing here" by sending an EMPTY cue text, and the
    parser publishes "" rather than None so it can be told apart from silence.

    The overlay does not care about that distinction -- it has no number to
    draw either way. Testing only for None rendered the empty case as the word
    "Cue" with nothing after it, which reads as a widget that has broken
    rather than a cue list that has run out, and it burns into the recording
    as fact.
    """
    from wer.overlay.widgets import CueWidget

    empty = DataBus()
    empty.publish("eos.cue.active.number", "", source_connection_id="eos")
    empty.publish("eos.cue.active.list", "", source_connection_id="eos")
    empty.publish("eos.cue.active.label", "", source_connection_id="eos")

    widget = CueWidget("cue")
    assert _pixels(widget.image(empty, 1080)) == _pixels(
        CueWidget("cue").image(DataBus(), 1080)
    )


def test_nothing_pending_drops_the_next_line(qt_app, bus: DataBus) -> None:
    """Same empty string, on the line that says what is coming. "Next" with a
    blank after it is worse than no Next line at all."""
    from wer.overlay.widgets import CueWidget

    with_next = CueWidget("cue").image(bus, 1080)
    bus.publish("eos.cue.pending.number", "", source_connection_id="eos")
    without = CueWidget("cue").image(bus, 1080)
    assert without.height() < with_next.height(), "the Next line was still drawn"


def test_cue_zero_is_a_real_cue(qt_app) -> None:
    """Guards the obvious way to write the blank check. Cue 0 is falsy as a
    number and empty-ish to a careless test, but it is a cue."""
    from wer.overlay.widgets import CueWidget

    bus = DataBus()
    bus.publish("eos.cue.active.number", "0", source_connection_id="eos")
    assert _pixels(CueWidget("cue").image(bus, 1080)) != _pixels(
        CueWidget("cue").image(DataBus(), 1080)
    )


# ------------------------------------------------------------- compositing


def test_compositing_changes_the_frame(qt_app, bus: DataBus) -> None:
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import CueWidget

    comp = Compositor(bus)
    comp.add(CueWidget("cue"))
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    before = frame.copy()
    comp.composite(frame, in_place=True)
    assert not np.array_equal(frame, before), "nothing was drawn"


def test_panic_hide_leaves_the_frame_untouched(qt_app, bus: DataBus) -> None:
    """Panic-hide must be total, and must not lose the layout."""
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import CueWidget

    comp = Compositor(bus)
    comp.add(CueWidget("cue"))
    comp.panic_hide(True)

    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    before = frame.copy()
    comp.composite(frame, in_place=True)
    assert np.array_equal(frame, before)
    assert len(comp.widgets) == 1, "panic-hide must not delete widgets"

    comp.panic_hide(False)
    comp.composite(frame, in_place=True)
    assert not np.array_equal(frame, before)


def test_composite_not_in_place_leaves_the_original_clean(qt_app, bus: DataBus) -> None:
    """The preview must not burn the overlay into the encoder's frame."""
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import CueWidget

    comp = Compositor(bus)
    comp.add(CueWidget("cue"))
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    result = comp.composite(frame, in_place=False)
    assert np.array_equal(frame, np.zeros_like(frame)), "original was modified"
    assert not np.array_equal(result, frame)


def test_a_widget_off_frame_does_not_crash(qt_app, bus: DataBus) -> None:
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    comp = Compositor(bus)
    comp.add(
        TextWidget("t", "{eos.cue.active.number}",
                   placement=Placement(Anchor.TOP_LEFT, offset_x=5.0, offset_y=5.0))
    )
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    comp.composite(frame, in_place=True)  # must not raise or index out of bounds


def test_a_throwing_widget_does_not_lose_the_frame(qt_app, bus: DataBus) -> None:
    """One bad widget must not cost a recorded frame."""
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import CueWidget, TextWidget

    class Broken(TextWidget):
        def _render(self, bus, frame_height):
            raise RuntimeError("bad widget")

    comp = Compositor(bus)
    comp.add(Broken("bad", "{eos.cue.active.number}", z_order=1))
    comp.add(CueWidget("good", z_order=2))

    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    before = frame.copy()
    comp.composite(frame, in_place=True)
    assert not np.array_equal(frame, before), "the good widget should still draw"


def test_a_widget_whose_render_failed_is_drawn_on_the_next_frame(qt_app, bus: DataBus) -> None:
    """A render that raised is tried again next frame, not when a key next changes.

    That used to happen by accident: a failed render left no picture, and no
    picture meant draw again. Once a render that drew nothing was cached like
    any other, it rested on one line that nothing checked. Without it, a widget
    that failed once, for a reason that then cleared, stayed off the recording
    until something it reads changed -- for a show name, never.
    """
    from wer.overlay.widgets import TextWidget

    class FailsOnce(TextWidget):
        failures = 1

        def _render(self, bus, frame_height):
            if self.failures:
                self.failures -= 1
                raise RuntimeError("a fault that clears")
            return super()._render(bus, frame_height)

    widget = FailsOnce("cue", "Cue {eos.cue.active.number}")    # no expiry on it
    assert widget.image(bus, 1080) is None
    assert widget.image(bus, 1080) is not None, (
        "a widget that failed once stayed off the picture"
    )


def test_z_order_is_respected(qt_app, bus: DataBus) -> None:
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    comp = Compositor(bus)
    comp.add(TextWidget("b", "x", z_order=50))
    comp.add(TextWidget("a", "y", z_order=10))
    assert [w.id for w in comp.widgets] == ["a", "b"]


def test_the_blend_reads_the_widget_image_not_a_freed_conversion(qt_app) -> None:
    """The array handed to the blender must own its bytes.

    _qimage_to_rgba converts anything that is not RGBA8888, and a converted
    QImage is a temporary: the moment the function returns, its buffer is
    freed. A view over it then reads whatever the allocator has since put
    there. Every watermark takes that path -- ImageWidget renders ARGB32 --
    which is why only pictures came out shredded and text never did.
    """
    from PySide6.QtGui import QImage

    from wer.overlay.compositor import _qimage_to_rgba

    source = QImage(64, 64, QImage.Format.Format_ARGB32)
    source.fill(0xFF0000FF)
    array = _qimage_to_rgba(source)
    assert array.base is None, "the blender was handed a view over a dead buffer"


def test_a_watermark_blends_every_pixel_of_itself(qt_app, tmp_path) -> None:
    """A solid logo must land solid.

    Measured on the shipped 'Watermark / logo' preset, roughly half the
    watermark's pixels were never blended -- a comb-stripe of missing columns
    burned into every frame, with no error anywhere. That is the visible
    symptom of blending from the freed conversion buffer above; over about a
    megabyte of scaled pixels the same read takes the process down instead.
    """
    from wer.core.databus import DataBus
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import ImageWidget

    widget = ImageWidget(
        "wm", str(_png(tmp_path, size=(220, 220))), height=0.2,
        placement=Placement(Anchor.TOP_LEFT, margin=0.0),
    )
    comp = Compositor(DataBus())
    comp.add(widget)

    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    comp.composite(frame, in_place=True)

    drawn = widget.image(DataBus(), 1080)
    patch = frame[: drawn.height(), : drawn.width()]
    assert np.array_equal(
        patch, np.broadcast_to(np.array([0, 0, 255], np.uint8), patch.shape)
    ), "the watermark did not blend as the solid colour it is"


def test_adding_a_widget_twice_does_not_put_it_in_the_layout_twice(
    qt_app, bus: DataBus
) -> None:
    """The editor re-adds a widget to force a z-order re-sort.

    Every "Bring forward", "Send back" or Layer change therefore appended
    another copy: blended again (a translucent box drawn twice is twice as
    dark), counted again by the cost indicator, and saved into the show file
    as a second widget.
    """
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    comp = Compositor(bus)
    widget = comp.add(TextWidget("t", "{eos.cue.active.number}", z_order=10))

    widget.z_order = 40
    comp.add(widget)
    comp.add(widget)

    assert [w.id for w in comp.widgets] == ["t"]


def test_a_layout_with_two_widgets_of_one_id_keeps_only_one(
    qt_app, bus: DataBus
) -> None:
    """A show file autosaved before the duplicate fix holds two of the same id.

    Everything in the compositor is keyed by widget id, so both cannot be
    tracked: only the last to subscribe stays bound to the bus. The other is
    never marked dirty again and blends whatever it rendered first under the
    live copy for the rest of the take -- an hour-old cue number, in every
    frame, with nothing on screen to say so.
    """
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    comp = Compositor(bus)
    first = TextWidget("clock", "{clock.wall}")
    second = TextWidget("clock", "{clock.wall}")
    comp.apply_layout([first, second])

    assert [w.id for w in comp.widgets] == ["clock"]

    kept = comp.widgets[0]
    bus.publish("clock.wall", "19:00:00", source_connection_id="test")
    comp.composite(np.zeros((720, 1280, 3), np.uint8))
    kept._dirty = False

    bus.publish("clock.wall", "19:00:01", source_connection_id="test")
    assert kept._dirty, "the widget left in the layout is not bound to the bus"


def test_overlay_stays_a_small_fraction_of_the_frame(qt_app, bus: DataBus) -> None:
    """The cost model behind per-rect blending: coverage must stay low.

    If a default layout ever covered most of the frame, per-rectangle blending
    would stop being cheaper than a full-frame blend and 4K would suffer.
    """
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import CueWidget, TextWidget

    comp = Compositor(bus)
    comp.add(CueWidget("cue", placement=Placement(Anchor.TOP_LEFT)))
    comp.add(TextWidget("cmd", "{eos.cmdline.text}",
                        placement=Placement(Anchor.BOTTOM_LEFT)))
    for width, height in (HD, UHD):
        coverage = comp.dirty_area_fraction(width, height)
        assert coverage < 0.15, f"overlay covers {coverage:.1%} at {width}x{height}"


# ------------------------------------------- text is bounded by the picture


def test_a_long_command_line_is_kept_inside_the_frame(qt_app) -> None:
    """The image was sized from the text alone, with no reference to the frame.

    A long Eos command line therefore rendered wider than the picture: the end
    of it ran off frame with nothing to say it had been cut, and the cost of
    drawing it -- three stroked QPainterPaths per line, inside the capture
    thread's read loop -- grew with the square of the character count.
    """
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    widget = TextWidget("cmd", "{eos.cmdline.text}",
                        placement=Placement(Anchor.BOTTOM_LEFT))
    comp = Compositor(bus)
    comp.add(widget)

    line = "LIVE: Cue 102.5 : Group 12 Thru 18 + Chan 401 Thru 428 At 65 Sneak Time 4 "
    widths = []
    for repeats in (6, 12):
        bus.publish("eos.cmdline.text", line * repeats, source_connection_id="eos")
        comp.composite(np.zeros((1080, 1920, 3), dtype=np.uint8), in_place=True)
        widths.append(comp.widget_size("cmd")[0])

    assert widths[0] <= 1920, f"drew {widths[0]} px of text across a 1920 px frame"
    assert widths[0] == widths[1], (
        "cost still grows with the text: twice the characters, a wider image"
    )


def test_over_long_text_is_cut_at_the_end_furthest_from_its_anchor(qt_app) -> None:
    """Bounding the width also decides WHICH half of the text survives.

    Before the bound, an over-wide image simply hung off the frame at the end
    it grew towards: a left-anchored widget lost its tail off the right edge, a
    right-anchored one lost its head off the left. Eliding right for everything
    kept the head in both cases -- so the shipped show-name caption, which
    anchors bottom-right, had the ellipsis land on exactly the part that used
    to be readable.

    Measured by hand on the real platform, 1920 px frame, 300-char line:
    bottom-right kept the head with ElideRight, and the tail once the anchor
    chose ElideLeft. The pixels are font-dependent, so what is pinned here is
    the choice and the bound, not a rendering.
    """
    from wer.overlay.widgets import TextWidget

    left = TextWidget("l", "{k}", placement=Placement(Anchor.BOTTOM_LEFT))
    right = TextWidget("r", "{k}", placement=Placement(Anchor.MIDDLE_RIGHT))
    middle = TextWidget("m", "{k}", placement=Placement(Anchor.BOTTOM_CENTER))

    from PySide6.QtCore import Qt

    assert left._elide_mode() is Qt.TextElideMode.ElideRight
    assert right._elide_mode() is Qt.TextElideMode.ElideLeft
    assert middle._elide_mode() is Qt.TextElideMode.ElideMiddle

    bus = DataBus()
    bus.publish("k", "Twelfth Night, LX file rev 14 " * 10, source_connection_id="eos")
    for widget in (left, right, middle):
        image = widget.image(bus, 1080, 1920)
        assert image.width() <= 1920, (
            f"{widget.placement.anchor} drew {image.width()} px across 1920"
        )


def test_a_command_line_keeps_the_end_being_typed_wherever_it_is_anchored(qt_app) -> None:
    """The end of a command line is the part being typed. Cut like any other
    text at the end away from its anchor, a long command in its usual
    bottom-left corner lost exactly that part to the ellipsis."""
    from PySide6.QtCore import Qt

    from wer.overlay.widgets import TextWidget

    for anchor in (Anchor.BOTTOM_LEFT, Anchor.BOTTOM_CENTER, Anchor.BOTTOM_RIGHT):
        widget = TextWidget("c", "{eos.cmdline.text}", placement=Placement(anchor))
        assert widget._elide_mode() is Qt.TextElideMode.ElideLeft, anchor
    one_user = TextWidget(
        "u", "> {eos.cmdline.user.2.text}", placement=Placement(Anchor.BOTTOM_LEFT)
    )
    assert one_user._elide_mode() is Qt.TextElideMode.ElideLeft

    bus = DataBus()
    bus.publish(
        "eos.cmdline.text",
        "LIVE: Chan 1 Thru 20 At Full " * 12 + "Sneak Time 5 Enter",
        source_connection_id="eos",
    )
    bounded = TextWidget(
        "c", "{eos.cmdline.text}", placement=Placement(Anchor.BOTTOM_LEFT), max_width=0.5
    )
    assert bounded.image(bus, 1080, 1920).width() <= 960


def test_text_that_fits_is_not_touched(qt_app, bus: DataBus) -> None:
    """Bounding the width must not start eliding ordinary cue readouts."""
    from wer.overlay.widgets import render_text_block

    from wer.overlay.style import TextStyle

    style = TextStyle(size=0.030)
    unbounded = render_text_block("LIVE: Cue 82 : Chan 1 Thru 12 At Full", style, 1080)
    bounded = render_text_block(
        "LIVE: Cue 82 : Chan 1 Thru 12 At Full", style, 1080, max_width=1920
    )
    assert bounded.width() == unbounded.width()
    assert _pixels(bounded) == _pixels(unbounded)


# ---------------------------- a widget must not burn in data the bus dropped


def _alpha(image) -> int:
    return -1 if image is None else sum(image.constBits().tobytes()[3::4])


def test_a_widget_repaints_when_its_value_expires(qt_app) -> None:
    """The worst instance of the stale-data class, because it leaves no trace.

    A widget is marked dirty when a key CHANGES. A key passing its expiry is
    not a change, so no notification fires -- and the cache went on returning
    the last fresh render for ever. The bus said "Cue --" while the overlay
    drew "Cue 58" into every frame of the recording.
    """
    import time

    from wer.core.databus import DataBus
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    widget = TextWidget("w", "Cue {eos.cue.active.number}")
    bus.publish("eos.cue.active.number", "58",
                source_connection_id="eos", stale_after=0.25)
    widget.notify_changed()

    fresh = _alpha(widget.image(bus, 1080))
    assert bus.render(widget.template) == "Cue 58"

    time.sleep(0.6)
    assert bus.render(widget.template) != "Cue 58", "the bus should have expired it"
    assert _alpha(widget.image(bus, 1080)) != fresh, (
        "the widget kept drawing a value the bus had already given up on"
    )


def test_a_widget_with_no_expiring_keys_still_caches(qt_app) -> None:
    """The cache is what keeps 4K compositing affordable; most of the bus has
    no expiry and must not be re-rendered every frame."""
    import time

    from wer.core.databus import DataBus
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    widget = TextWidget("w", "{show.name}")
    bus.publish("show.name", "The Comedy of Errors", source_connection_id="manual")
    widget.notify_changed()

    first = widget.image(bus, 1080)
    time.sleep(0.3)
    assert widget.image(bus, 1080) is first, "re-rendered a value that cannot expire"


def test_a_widget_stops_redrawing_once_an_expired_key_is_drawn_as_missing(qt_app) -> None:
    """The expiry deadline stayed in the past for good. Every frame found it
    due, redrew the cue block, rebuilt its float32 blend layers and put the
    same deadline back -- thirty times a second on the capture thread, from the
    moment a desk went quiet mid-fade until it spoke again."""
    import time

    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import CueWidget

    bus = DataBus()
    bus.publish("eos.cue.active.number", "58", source_connection_id="eos")
    for key, value in (("progress", 0.35), ("fading", True), ("duration", 40.0)):
        bus.publish(f"eos.cue.active.{key}", value,
                    source_connection_id="eos", stale_after=0.25)
    comp = Compositor(bus)
    widget = comp.add(CueWidget("cue"))
    frame = np.zeros((720, 1280, 3), np.uint8)

    comp.composite(frame)
    time.sleep(0.6)                  # the desk has gone quiet mid-fade
    comp.composite(frame)            # drawn once more, without the bar
    drawn = widget.image(bus, 720, 1280)
    layers = comp._layers["cue"]

    for _ in range(3):
        comp.composite(frame)
    assert widget.image(bus, 720, 1280) is drawn, "redrew keys already drawn as expired"
    assert comp._layers["cue"] is layers, "rebuilt the blend layers every frame"


def test_a_key_back_from_stale_with_the_same_value_is_drawn_again(qt_app) -> None:
    """The other half of not redrawing an expired key every frame. A desk that
    drops and comes back re-sends the values it had, and the bus did not count
    an unchanged value as a change -- so nothing told the widget, and "--"
    stayed in the recording over a value the bus believed again."""
    import time

    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    comp = Compositor(bus)
    widget = comp.add(TextWidget("time", "Time {eos.cue.active.duration}s"))

    bus.publish("eos.cue.active.duration", 40.0,
                source_connection_id="eos", stale_after=0.25)
    live = _pixels(widget.image(bus, 1080, 1920))
    time.sleep(0.6)
    assert _pixels(widget.image(bus, 1080, 1920)) != live, (
        "the expired fade time was never drawn as missing"
    )
    widget.image(bus, 1080, 1920)

    bus.publish("eos.cue.active.duration", 40.0,
                source_connection_id="eos", stale_after=0.25)
    assert _pixels(widget.image(bus, 1080, 1920)) == live, (
        "the desk came back with the same fade time and the overlay still says --"
    )


def test_a_key_that_expires_while_the_widget_is_drawing_is_still_redrawn(qt_app) -> None:
    """Expiry is judged from when the render began, not when it ended. A key
    can expire part-way through drawing, after it was read as live; judged from
    the end, it would count as already drawn as missing, and its last live
    value would stay on the picture for good."""
    import time

    from wer.overlay.widgets import TextWidget

    class Slow(TextWidget):
        def _render(self, bus, frame_height):
            image = super()._render(bus, frame_height)
            time.sleep(0.6)          # the key expires before this returns
            return image

    bus = DataBus()
    widget = Slow("cue", "Cue {eos.cue.active.number}")
    bus.publish("eos.cue.active.number", "58",
                source_connection_id="eos", stale_after=0.3)
    live = _pixels(widget.image(bus, 1080))
    assert _pixels(widget.image(bus, 1080)) != live, (
        "Cue 58 stayed on the picture after the bus expired it"
    )


def test_a_key_back_from_stale_is_drawn_again_when_its_publisher_was_held_up(qt_app) -> None:
    """DataBus.publish took its timestamp before the lock and judged the old
    value's expiry by it. A connection thread held up between the two -- a
    thread switch, another thread holding the lock -- found a value that had
    expired meanwhile still live and, it being unchanged, told nobody. A frame
    drawn in that gap had shown it as missing and, with nothing left to wait
    for, stopped watching the clock: "--" stayed on the picture over a value
    the bus believed again, until the value next changed."""
    import time

    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    comp = Compositor(bus)
    widget = comp.add(TextWidget("time", "Time {eos.cue.active.duration}s"))
    widget.image(bus, 1080, 1920)    # fonts loaded, so the live draw below is quick

    # A second of life, so both live draws land inside it on a machine that is
    # busy: with half a second the test failed once while two 1080p soak
    # recordings were encoding, the final draw finding the re-sent value
    # already expired again. The hold-up only has to outlast it.
    bus.publish("eos.cue.active.duration", 40.0,
                source_connection_id="eos", stale_after=1.0)
    live = _pixels(widget.image(bus, 1080, 1920))

    class HeldUp:
        """The bus lock, with the next thread to reach for it kept waiting
        until the value has expired and a frame has been drawn."""

        def __init__(self, lock) -> None:
            self.lock = lock
            self.waiting = True

        def __enter__(self):
            if self.waiting:
                self.waiting = False
                time.sleep(1.5)
                widget.image(bus, 1080, 1920)
            return self.lock.__enter__()

        def __exit__(self, *exc):
            return self.lock.__exit__(*exc)

    bus._lock = HeldUp(bus._lock)
    bus.publish("eos.cue.active.duration", 40.0,
                source_connection_id="eos", stale_after=1.0)

    assert _pixels(widget.image(bus, 1080, 1920)) == live, (
        "the desk re-sent the fade time while the overlay drew it as missing, "
        "and it still says --"
    )


def test_a_command_typed_while_its_layout_was_off_screen_is_drawn_when_it_comes_back(
    qt_app,
) -> None:
    """Layouts keep their widget objects between switches, and a widget off
    screen is subscribed to nothing. A command line was blank when its layout
    went off; the operator typed while another was up; back on the first it
    stayed blank until the next keystroke, which after an Enter may not come."""
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    bus.publish("eos.cmdline.text", "", source_connection_id="eos")
    comp = Compositor(bus)
    command = TextWidget("cmdline", "{eos.cmdline.text}")

    comp.apply_layout([command])
    comp.composite(np.zeros((720, 1280, 3), np.uint8))
    comp.apply_layout([TextWidget("clock", "{clock.wall}")])
    bus.publish("eos.cmdline.text", "LIVE: Chan 5 At Full", source_connection_id="eos")
    comp.apply_layout([command])

    frame = np.zeros((720, 1280, 3), np.uint8)
    comp.composite(frame)
    assert frame.any(), (
        "the command typed while another layout was up never reached the picture"
    )


def test_a_cue_that_moved_on_while_its_layout_was_off_screen_is_current_when_it_comes_back(
    qt_app,
) -> None:
    """The same gap, for a widget that had drawn something: "Cue 58" came back
    with its layout and stayed on the picture while the desk was on 60."""
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    bus.publish("eos.cue.active.number", "58", source_connection_id="eos")
    comp = Compositor(bus)
    cue = TextWidget("cue", "Cue {eos.cue.active.number}")

    comp.apply_layout([cue])
    comp.composite(np.zeros((720, 1280, 3), np.uint8))
    comp.apply_layout([TextWidget("clock", "{clock.wall}")])
    bus.publish("eos.cue.active.number", "60", source_connection_id="eos")
    comp.apply_layout([cue])
    comp.composite(np.zeros((720, 1280, 3), np.uint8))

    current = TextWidget("cue", "Cue {eos.cue.active.number}").image(bus, 720, 1280)
    assert _pixels(cue.image(bus, 720, 1280)) == _pixels(current), (
        "Cue 58 came back with its layout while the desk was on 60"
    )


def test_a_blank_widget_says_its_data_has_gone_when_the_console_is_switched(qt_app) -> None:
    """Switching console clears the old desk's keys, and a key taken off the
    bus notifies nobody. A label over an unlabelled cue had drawn nothing and,
    told nothing, went on drawing nothing rather than "--". If the new desk
    never answers -- a wrong address, a desk still switched off -- nothing on
    the picture says the data has gone."""
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    bus.publish("eos.cue.active.label", "", source_connection_id="eos")
    comp = Compositor(bus)
    comp.add(TextWidget("label", "{eos.cue.active.label}"))
    comp.composite(np.zeros((720, 1280, 3), np.uint8))

    bus.clear_source("eos")
    frame = np.zeros((720, 1280, 3), np.uint8)
    comp.composite(frame)
    assert frame.any(), "the label lost its data and the picture does not say so"


def test_the_old_consoles_label_does_not_stay_on_the_picture_after_a_switch(qt_app) -> None:
    """The same silence, for a widget that had drawn something: the old desk's
    cue label stayed on the picture, looking current, until the new desk sent
    one of its own."""
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    bus.publish("eos.cue.active.label", "Triangle transition", source_connection_id="eos")
    comp = Compositor(bus)
    label = comp.add(TextWidget("label", "{eos.cue.active.label}"))
    comp.composite(np.zeros((720, 1280, 3), np.uint8))

    bus.clear_source("eos")
    comp.composite(np.zeros((720, 1280, 3), np.uint8))

    gone = TextWidget("label", "{eos.cue.active.label}").image(bus, 720, 1280)
    assert _pixels(label.image(bus, 720, 1280)) == _pixels(gone), (
        "the previous console's cue label is still drawn as if current"
    )


# ------------------------------------------- the fade bar moves smoothly


def test_the_fade_bar_asks_to_be_redrawn_while_a_fade_runs(qt_app) -> None:
    """It always interpolated, but nothing ever asked it to repaint, so it
    stepped once a second while the video ran at thirty. That is the jitter."""
    import time

    from wer.core.databus import DataBus
    from wer.overlay.widgets import CueWidget

    bus = DataBus()
    widget = CueWidget("cue")
    bus.publish("eos.cue.active.number", "58", source_connection_id="eos")
    bus.publish("eos.cue.active.fading", True, source_connection_id="eos")
    bus.publish("eos.cue.active.progress", 0.10, source_connection_id="eos")
    bus.publish("eos.cue.active.duration", 6.0, source_connection_id="eos")

    widget.image(bus, 1080)          # establishes the drawn value
    time.sleep(0.25)                 # the bar has moved, the console has not
    assert widget.wants_repaint(bus) is True


def test_a_finished_cue_does_not_repaint_forever(qt_app) -> None:
    """The cache is what keeps 4K compositing affordable. Only ask for a
    redraw while something is actually moving."""
    from wer.core.databus import DataBus
    from wer.overlay.widgets import CueWidget

    bus = DataBus()
    widget = CueWidget("cue")
    bus.publish("eos.cue.active.number", "58", source_connection_id="eos")
    bus.publish("eos.cue.active.fading", False, source_connection_id="eos")
    widget.image(bus, 1080)
    assert widget.wants_repaint(bus) is False


# --------------------------------------------------- the watermark widget


def _png(tmp_path, name="mark.png", size=(40, 20)):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    image = QImage(size[0], size[1], QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.red)
    path = tmp_path / name
    assert image.save(str(path))
    return path


def test_a_watermark_draws_at_the_requested_fraction_of_the_frame(qt_app, tmp_path) -> None:
    """Sized against frame height so one layout works at 1080p and 4K."""
    from wer.core.databus import DataBus
    from wer.overlay.widgets import ImageWidget

    widget = ImageWidget("wm", str(_png(tmp_path)), height=0.10)
    image = widget.image(DataBus(), 1080)
    assert image is not None
    assert image.height() == 108
    assert image.width() == 216           # 2:1 source, aspect kept


def test_a_missing_watermark_draws_nothing_and_says_why(qt_app) -> None:
    """A broken-image box in a recording is worse than no watermark, but
    failing silently is worse still -- nobody notices until afterwards."""
    from wer.core.databus import DataBus
    from wer.overlay.widgets import ImageWidget

    widget = ImageWidget("wm", "C:/definitely/not/here.png")
    assert widget.image(DataBus(), 1080) is None
    assert "not/here.png" in widget.problem


def test_a_file_that_is_not_an_image_is_reported(qt_app, tmp_path) -> None:
    from wer.core.databus import DataBus
    from wer.overlay.widgets import ImageWidget

    victim = tmp_path / "notreally.png"
    victim.write_text("this is text", encoding="utf-8")
    widget = ImageWidget("wm", str(victim))
    assert widget.image(DataBus(), 1080) is None
    assert "not an image" in widget.problem


def test_a_watermark_reports_its_problem_before_anything_is_rendered(
    qt_app, tmp_path
) -> None:
    """The overlay is normally set up before the camera is started.

    Until then nothing renders, so nothing wrote `problem`, and the editor's
    warning had nothing to show -- for exactly the widget whose docstring says
    it must not fail silently.
    """
    from wer.overlay.widgets import ImageWidget

    assert ImageWidget("wm", str(tmp_path / "nothing.png")).check(), (
        "a missing file was not reported until a frame was drawn"
    )
    assert ImageWidget("wm", str(_png(tmp_path))).check() == ""


def test_repointing_a_watermark_forgets_the_old_files_problem(qt_app, tmp_path) -> None:
    """The reason the editor warned about a path that had already been fixed."""
    from wer.core.databus import DataBus
    from wer.overlay.widgets import ImageWidget

    widget = ImageWidget("wm", "C:/definitely/not/here.png")
    assert widget.image(DataBus(), 1080) is None
    assert widget.problem

    widget.path = str(_png(tmp_path))
    widget.reload()
    assert widget.problem == "", "kept a complaint about a file it no longer uses"


def test_a_missing_watermark_is_not_looked_for_again_every_frame(qt_app, tmp_path) -> None:
    """A render that drew nothing was repeated every frame, and for a watermark
    that meant a trip to the disk each time, inside the capture thread's read
    loop: a stat for a missing file, a whole decode for one that will not read,
    and for a share that has gone away, however long Windows takes to say so.
    A logo put back must still come back without anyone retyping the path."""
    from wer.overlay.widgets import ImageWidget

    now = [1000.0]
    widget = ImageWidget("wm", str(tmp_path / "logo.png"))
    widget._clock = lambda: now[0]
    bus = DataBus()

    assert widget.image(bus, 1080) is None
    _png(tmp_path, name="logo.png")            # the show server is back
    now[0] += 1.0
    assert widget.image(bus, 1080) is None, "went back to the disk on the next frame"

    now[0] += ImageWidget.RETRY_FIRST
    assert widget.image(bus, 1080) is not None, "a restored logo never came back"


def test_a_watermark_that_stays_missing_is_looked_for_less_often(qt_app, tmp_path) -> None:
    """How long Windows blocks on a share that has gone away, and whether it
    blocks as long every time, is not established. A wait that doubles bounds
    what those attempts can cost the capture thread over a long take -- but it
    stops doubling, so a logo gone for an hour still comes back within a
    minute of being put back."""
    from wer.overlay.widgets import ImageWidget

    now = [1000.0]
    growing = ImageWidget("wm", str(tmp_path / "logo.png"))
    growing._clock = lambda: now[0]
    bus = DataBus()

    assert growing.image(bus, 1080) is None
    now[0] += ImageWidget.RETRY_FIRST
    assert growing.image(bus, 1080) is None       # the retry fails as well
    _png(tmp_path, name="logo.png")
    now[0] += ImageWidget.RETRY_FIRST
    assert growing.image(bus, 1080) is None, "the wait did not grow after a second failure"
    now[0] += ImageWidget.RETRY_FIRST
    assert growing.image(bus, 1080) is not None

    long_gone = ImageWidget("wm2", str(tmp_path / "long-gone.png"))
    long_gone._clock = lambda: now[0]
    for _ in range(12):
        now[0] += ImageWidget.RETRY_LONGEST
        assert long_gone.image(bus, 1080) is None
    _png(tmp_path, name="long-gone.png")
    now[0] += ImageWidget.RETRY_LONGEST
    assert long_gone.image(bus, 1080) is not None, "stopped looking for a long-lost logo"


def test_a_watermark_confirmed_in_the_editor_is_drawn_straight_away(qt_app, tmp_path) -> None:
    """The editor's check reads the file at once, whatever the wait before the
    capture thread's next attempt. A file it has just confirmed must reach the
    picture then too, not up to a minute later."""
    from wer.overlay.widgets import ImageWidget

    now = [1000.0]
    widget = ImageWidget("wm", str(tmp_path / "logo.png"))
    widget._clock = lambda: now[0]
    bus = DataBus()

    assert widget.image(bus, 1080) is None
    _png(tmp_path, name="logo.png")
    assert widget.check() == ""
    assert widget.image(bus, 1080) is not None, "confirmed in the editor, still not drawn"


def test_a_watermark_it_is_refused_access_to_is_reported_not_retried_every_frame(
    qt_app, monkeypatch
) -> None:
    """Path.is_file() answers False for a file that is not there but re-raises
    other errors, a permissions refusal among them. Raised out of the render,
    that was a traceback in the log every frame as well as a trip to the disk,
    and the editor's caption had nothing to say."""
    from pathlib import Path

    from wer.overlay import widgets
    from wer.overlay.widgets import ImageWidget

    asked: list[str] = []

    class Refused(type(Path())):
        def is_file(self):
            asked.append(str(self))
            raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(widgets, "Path", Refused)
    widget = ImageWidget("wm", r"\\showserver\graphics\logo.png")
    bus = DataBus()

    for _ in range(3):
        assert widget.image(bus, 1080) is None
    assert "Access is denied" in widget.problem
    assert len(asked) == 1, f"asked the file system {len(asked)} times in three frames"


def test_a_watermark_pointed_at_another_file_does_not_inherit_the_old_ones_wait(
    qt_app, tmp_path
) -> None:
    """How often one file failed says nothing about the next. The editor goes
    through reload(), which starts the wait afresh; anything that repoints a
    widget without it relies on _load noticing the path has changed, and
    nothing checked that it did. Missed, a logo chosen after one that had been
    missing for minutes would take a minute to appear once put in place, not
    five seconds."""
    from wer.overlay.widgets import ImageWidget

    now = [1000.0]
    widget = ImageWidget("wm", str(tmp_path / "old.png"))
    widget._clock = lambda: now[0]
    bus = DataBus()
    for _ in range(6):                          # long enough for the wait to reach its cap
        assert widget.image(bus, 1080) is None
        now[0] += ImageWidget.RETRY_LONGEST

    widget.path = str(tmp_path / "new.png")
    assert widget.image(bus, 1080) is None      # tried once, before it is in place
    _png(tmp_path, name="new.png")
    now[0] += ImageWidget.RETRY_FIRST
    assert widget.image(bus, 1080) is not None, (
        "the new file waited out the back-off the old one had earned"
    )


def test_a_watermark_round_trips_through_a_show_file(qt_app, tmp_path) -> None:
    from wer.overlay.layout import widget_from_dict, widget_to_dict
    from wer.overlay.widgets import ImageWidget

    original = ImageWidget("wm", str(_png(tmp_path)), height=0.14, opacity=0.6)
    restored = widget_from_dict(widget_to_dict(original))
    assert isinstance(restored, ImageWidget)
    assert (restored.path, restored.height, restored.image_opacity) == (
        original.path, 0.14, 0.6
    )


def test_the_watermark_reads_no_bus_keys(qt_app, tmp_path) -> None:
    """It is a picture. It must not make the compositor subscribe to anything."""
    from wer.overlay.widgets import ImageWidget

    assert ImageWidget("wm", str(_png(tmp_path))).bus_keys == []


# --------------------------------------------- every widget explains itself


def test_every_widget_type_describes_itself(qt_app) -> None:
    from wer.overlay.widgets import (
        CueWidget, FadeBarWidget, ImageWidget, PanelWidget, StatusWidget, TextWidget,
    )

    for widget in (TextWidget("t", "{eos.cue.active.number}"), CueWidget("c"),
                   CueWidget("stack", show_previous=True),
                   StatusWidget("s", key="eos.connected"), ImageWidget("i"),
                   PanelWidget("p"), PanelWidget("b", width=0.3, height=0.15),
                   FadeBarWidget("f"), FadeBarWidget("f", hide_when_idle=False)):
        text = widget.describe()
        assert text and text[0].isupper() and text.endswith(".")
        assert len(text) < 200


def test_the_description_says_what_appears_not_what_class_it_is(qt_app) -> None:
    from wer.overlay.widgets import CueWidget, TextWidget

    assert "Widget" not in CueWidget("c").describe()
    assert "eos.cue.active.number" in TextWidget("t", "{eos.cue.active.number}").describe()


# ------------------------------------------------ widgets that share an edge


def test_max_width_keeps_a_widget_out_of_its_neighbours_way(qt_app) -> None:
    """The margins keep a widget on the picture, not off the one beside it.

    A command line anchored bottom-left and a show name anchored bottom-right
    both grow towards the middle, and with only the margins to stop it a long
    enough command runs straight through the show name.
    """
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    bus.publish("eos.cmdline.text", "LIVE: Chan 1 Thru 12 At 75 Enter " * 6,
                source_connection_id="eos")
    placement = Placement(Anchor.BOTTOM_LEFT)
    free = TextWidget("cmd", "{eos.cmdline.text}", placement=placement)
    capped = TextWidget("cmd", "{eos.cmdline.text}", placement=placement,
                        max_width=0.4)
    roomy = TextWidget("cmd", "{eos.cmdline.text}", placement=placement,
                       max_width=1.0)

    free_width = free.image(bus, 1080, 1920).width()
    assert free_width > 0.4 * 1920, "the command line was never long enough to test"
    assert capped.image(bus, 1080, 1920).width() <= int(0.4 * 1920)
    assert roomy.image(bus, 1080, 1920).width() == free_width, (
        "a cap wider than the margins let the text run past them"
    )


# -------------------------------------------------- lines of different sizes


def test_a_clock_can_carry_the_date_in_smaller_type_beneath_it(qt_app) -> None:
    """One widget, two sizes: the time large and the date small under it, in
    one box, so the two cannot overlap or drift apart as either changes."""
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    bus.publish("clock.wall", "21:57:31", source_connection_id="system")
    bus.publish("clock.date_long", "Tuesday 08 September 2026",
                source_connection_id="system")
    template = "{clock.wall}\n{clock.date_long}"

    even = TextWidget("c", template).image(bus, 1080)
    emphasised = TextWidget("c", template, emphasise_first_line=True).image(bus, 1080)
    time_alone = TextWidget("c", "{clock.wall}").image(bus, 1080)

    assert emphasised.height() < even.height(), "the date was drawn as large as the time"
    assert emphasised.width() < even.width(), "the date was drawn as wide as ever"
    assert emphasised.height() > time_alone.height(), "the date line went missing"


def test_emphasis_leaves_a_single_line_exactly_as_it_was(qt_app, bus: DataBus) -> None:
    from wer.overlay.widgets import TextWidget

    template = "Cue {eos.cue.active.number}"
    plain = TextWidget("t", template).image(bus, 1080)
    emphasised = TextWidget("t", template, emphasise_first_line=True).image(bus, 1080)
    assert _pixels(emphasised) == _pixels(plain)


def test_a_primary_line_stays_large_and_every_other_line_is_secondary(qt_app) -> None:
    """For a block whose main line is not the first -- the cue stack, with the
    last cue above the live one."""
    from wer.overlay.widgets import render_text_block

    style = TextStyle(size=0.040)
    text = "Last  54  Adriana Xs SL\nCue 58\nNext  62"
    even = render_text_block(text, style, 1080)
    middle = render_text_block(text, style, 1080, primary_line=1)
    first = render_text_block(text, style, 1080, secondary_from_line=1)

    assert middle.height() < even.height(), "the other lines were not made smaller"
    assert middle.height() == first.height(), "not one large line and two small ones"
    assert middle.width() < first.width(), (
        "the long Last line was drawn large: the large line is not the middle one"
    )


def test_primary_line_wins_when_both_are_given(qt_app) -> None:
    from wer.overlay.widgets import render_text_block

    style = TextStyle(size=0.040)
    text = "Last  54  Adriana Xs SL\nCue 58\nNext  62"
    both = render_text_block(text, style, 1080, primary_line=1, secondary_from_line=0)
    assert _pixels(both) == _pixels(render_text_block(text, style, 1080, primary_line=1))


# ---------------------------------------------------------- the cue stack


def _previous(bus: DataBus, number: str = "78", label: str = "Storm builds") -> None:
    bus.publish("eos.cue.previous.number", number, source_connection_id="eos")
    bus.publish("eos.cue.previous.label", label, source_connection_id="eos")


def test_a_cue_block_can_show_the_cue_it_came_from(qt_app, bus: DataBus) -> None:
    """Last, Cue, Next, in the order they run: scrubbing a recording back to a
    bad transition shows where it came from as well as where it was going."""
    from wer.overlay.widgets import CueWidget

    _previous(bus)
    without = CueWidget("cue").image(bus, 1080)
    stack = CueWidget("cue", show_previous=True)
    assert stack.image(bus, 1080).height() > without.height(), "no Last line was drawn"
    assert "last cue" in stack.describe()
    assert "last cue" not in CueWidget("cue").describe()


def test_the_live_cue_stays_the_large_line_under_the_last_one(qt_app, bus: DataBus) -> None:
    """With a Last line on top, "everything after the first line is small"
    shrinks the live cue and leaves the one already gone as the biggest thing
    on screen. Heights cannot tell the two apart -- one large line and three
    small either way -- so this compares against both renderings."""
    from wer.overlay.widgets import CueWidget, render_text_block

    _previous(bus)
    widget = CueWidget("cue", show_previous=True)
    lines = "Last  78  Storm builds\nCue 1/82\nTriangle transition\nNext  86"
    live_large = render_text_block(lines, widget.style, 1080, primary_line=1)
    last_large = render_text_block(lines, widget.style, 1080, secondary_from_line=1)

    drawn = _pixels(widget.image(bus, 1080))
    assert drawn != _pixels(last_large), "the Last line was drawn as the large one"
    assert drawn == _pixels(live_large)


def test_no_previous_cue_means_no_last_line(qt_app, bus: DataBus) -> None:
    """The top of the show has nothing before it. The same rule as the Next
    line: "Last" with a blank after it reads as a broken widget."""
    from wer.overlay.widgets import CueWidget

    ordinary = _pixels(CueWidget("cue").image(bus, 1080))
    assert _pixels(CueWidget("cue", show_previous=True).image(bus, 1080)) == ordinary, (
        "drew a Last line when nothing had been published"
    )
    _previous(bus, number="", label="")
    assert _pixels(CueWidget("cue", show_previous=True).image(bus, 1080)) == ordinary, (
        "drew a Last line when the desk said there was none"
    )


def test_a_single_list_show_can_drop_the_list_from_the_heading(
    qt_app, bus: DataBus
) -> None:
    """On a show with one cue list, "1/" sits in front of every cue number in
    the recording and says nothing."""
    from wer.overlay.widgets import CueWidget, render_text_block

    bare = dict(show_label=False, show_pending=False)
    listed = CueWidget("cue", **bare).image(bus, 1080)
    unlisted = CueWidget("cue", show_list=False, **bare).image(bus, 1080)

    assert unlisted.width() < listed.width()
    assert _pixels(unlisted) == _pixels(render_text_block("Cue 82", TextStyle(), 1080))


def test_the_cue_block_reads_the_previous_cue_only_when_it_shows_it(qt_app) -> None:
    """Every key read is a subscription, and the editor lists them under
    "Reads:". A block not showing the last cue has no business with it."""
    from wer.overlay.widgets import CueWidget

    previous = {"eos.cue.previous.number", "eos.cue.previous.label"}
    assert not previous & set(CueWidget("cue").content_keys)
    assert previous <= set(CueWidget("cue", show_previous=True).content_keys)


# ----------------------------------------------------------------- panels


def test_a_panel_is_drawn_at_its_fractions_of_the_frame(qt_app) -> None:
    """Width follows the frame's width and height its height -- not the
    font-size rule -- because a strip has to reach both edges at any aspect
    ratio and keep its proportions at 4K."""
    from wer.overlay.widgets import PanelWidget

    panel = PanelWidget("p", width=0.5, height=0.1)
    hd = panel.image(DataBus(), 1080, 1920)
    assert (hd.width(), hd.height()) == (960, 108)
    uhd = panel.image(DataBus(), 2160, 3840)
    assert (uhd.width(), uhd.height()) == (1920, 216)


def test_a_panel_is_filled_with_its_box_colour_even_with_the_box_off(qt_app) -> None:
    """A panel is nothing but its box. One that vanished when "Box" was
    unticked would look broken rather than plain."""
    from wer.overlay.style import BoxStyle
    from wer.overlay.widgets import PanelWidget

    fill = Colour(200, 40, 10, 180)
    panel = PanelWidget(
        "p", width=0.2, height=0.1,
        style=TextStyle(box=BoxStyle(enabled=False, fill=fill, corner_radius=0.0)),
    )
    image = panel.image(DataBus(), 1080, 1920)
    centre = image.pixelColor(image.width() // 2, image.height() // 2)
    drawn = (centre.red(), centre.green(), centre.blue(), centre.alpha())
    assert all(abs(a - b) <= 2 for a, b in zip(drawn, fill.rgba)), (
        f"drew {drawn}, not {fill.rgba}"
    )


def test_a_panel_reads_nothing_from_the_bus(qt_app) -> None:
    """It is a shape. It must not make the compositor subscribe to anything."""
    from wer.overlay.widgets import PanelWidget

    assert PanelWidget("p").bus_keys == []


def test_a_panel_composites_behind_the_text_on_it(qt_app, bus: DataBus) -> None:
    from wer.overlay.compositor import Compositor
    from wer.overlay.style import BoxStyle
    from wer.overlay.widgets import PanelWidget, TextWidget

    strip = PanelWidget(
        "strip", width=1.0, height=0.11,
        placement=Placement(Anchor.BOTTOM_CENTER, margin=0.0),
        style=TextStyle(box=BoxStyle(fill=Colour(255, 0, 0), corner_radius=0.0)),
        z_order=1,
    )
    caption = TextWidget("cmd", "{eos.cmdline.text}",
                         placement=Placement(Anchor.BOTTOM_LEFT), z_order=20)
    comp = Compositor(bus)
    comp.add(caption)
    comp.add(strip)

    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    comp.composite(frame, in_place=True)

    # Right of the short caption, the strip is all there is.
    top = 1080 - round(0.11 * 1080)
    clear = frame[:, 1200:]
    assert not clear[:top].any(), "the strip drew above its 11%"
    assert (clear[top:] == (0, 0, 255)).all(), "the strip did not fill its band"

    rect = caption.rect(bus, 1920, 1080)
    over_strip = frame[top:rect.bottom, rect.x:rect.right]
    assert over_strip.size and not (over_strip == (0, 0, 255)).all(), (
        "the caption is underneath the strip, not on it"
    )


# ---------------------------------------------------- the fade bar on its own


def _fading(bus: DataBus, progress: float, duration: float = 6.0) -> None:
    bus.publish("eos.cue.active.fading", True, source_connection_id="eos")
    bus.publish("eos.cue.active.progress", progress, source_connection_id="eos")
    bus.publish("eos.cue.active.duration", duration, source_connection_id="eos")


def _solid_columns(image) -> int:
    """Columns along the bar's middle row drawn in the solid fill rather than
    the translucent track."""
    row = image.height() // 2
    return sum(1 for x in range(image.width()) if image.pixelColor(x, row).alpha() > 200)


def test_a_fade_bar_draws_nothing_between_fades(qt_app) -> None:
    """An empty track sitting in every frame between cues is clutter on a
    recording. With hiding turned off the track stays, and stays empty."""
    from wer.overlay.widgets import FadeBarWidget

    idle = DataBus()
    idle.publish("eos.cue.active.fading", False, source_connection_id="eos")
    assert FadeBarWidget("fade").image(idle, 1080, 1920) is None
    assert FadeBarWidget("fade").image(DataBus(), 1080, 1920) is None, (
        "drew a fade with no console connected"
    )

    track = FadeBarWidget("fade", hide_when_idle=False).image(idle, 1080, 1920)
    assert track is not None
    assert _solid_columns(track) == 0, "an idle track was drawn partly filled"


def test_a_fade_bar_fills_in_proportion_to_the_fade(qt_app) -> None:
    from wer.overlay.widgets import FadeBarWidget

    for progress in (0.25, 0.5, 0.75):
        bus = DataBus()
        _fading(bus, progress)
        image = FadeBarWidget("fade", width=0.5).image(bus, 1080, 1920)
        assert image.width() == 960
        assert _solid_columns(image) / image.width() == pytest.approx(progress, abs=0.02)


def test_a_fade_bar_moves_between_the_consoles_reports(qt_app) -> None:
    """The cue block's rule, from the same clock: redraw while the fade runs,
    advancing between the console's once-a-second reports, and stop asking
    once the fade is done."""
    import time

    from wer.overlay.widgets import FadeBarWidget

    bus = DataBus()
    widget = FadeBarWidget("fade")
    _fading(bus, 0.10)
    first = _solid_columns(widget.image(bus, 1080, 1920))

    time.sleep(0.25)                 # the bar has moved, the console has not
    assert widget.wants_repaint(bus) is True
    assert _solid_columns(widget.image(bus, 1080, 1920)) > first, (
        "the bar stood still between reports"
    )

    bus.publish("eos.cue.active.fading", False, source_connection_id="eos")
    widget.notify_changed()
    widget.image(bus, 1080, 1920)
    assert widget.wants_repaint(bus) is False
