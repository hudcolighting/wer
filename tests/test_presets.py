"""The widget catalogue: every preset builds, renders and round-trips."""

from __future__ import annotations

import pytest

from wer.core.databus import DataBus
from wer.overlay.layout import widget_from_dict, widget_to_dict
from wer.overlay.presets import PRESETS, get_preset, make_widget, preset_groups, unique_id


@pytest.fixture()
def bus() -> DataBus:
    """A bus carrying everything the presets might read."""
    bus = DataBus()
    values = {
        "eos.cue.active.list": "1",
        "eos.cue.active.number": "58",
        "eos.cue.active.label": "Adriana Xs Center",
        "eos.cue.active.text": "1/58 Adriana Xs Center 3.0 100%",
        "eos.cue.active.duration": 3.0,
        "eos.cue.active.remaining": 1.4,
        "eos.cue.active.fading": True,
        "eos.cue.pending.number": "62",
        "eos.cue.pending.label": "Enter Dromio",
        "eos.cue.previous.number": "54",
        "eos.cue.previous.label": "Adriana Xs SL",
        "eos.cmdline.text": "LIVE: Cue 58 : ",
        "eos.show.name": "Comedy EOS",
        "eos.chan.active": "1 THRU 12",
        "eos.connected": True,
        "clock.wall": "21:57:31",
        "clock.wall12": "9:57:31 PM",
        "clock.date": "2026-09-08",
        "clock.date_long": "Tuesday 08 September 2026",
        "clock.elapsed": "0:12:04",
        "clock.recording": True,
        "clock.take": 3,
        "manual.show": "The Comedy of Errors",
        "manual.act": "Two",
        "manual.scene": "One",
        "manual.note": "followspot late",
    }
    for key, value in values.items():
        bus.publish(key, value, source_connection_id="test")
    return bus


def test_the_catalogue_is_not_empty() -> None:
    assert len(PRESETS) >= 20


def test_preset_keys_are_unique() -> None:
    keys = [preset.key for preset in PRESETS]
    assert len(keys) == len(set(keys))


def test_every_preset_is_grouped() -> None:
    groups = preset_groups()
    assert set(groups) == {"Cue", "Console", "Time", "Typed", "Picture", "Shapes"}
    assert sum(len(v) for v in groups.values()) == len(PRESETS)


def test_the_widgets_the_user_asked_for_exist() -> None:
    """Cue duration, date and record elapsed were requested by name."""
    assert get_preset("cue_duration") is not None
    assert get_preset("date") is not None
    assert get_preset("date_long") is not None
    assert get_preset("elapsed") is not None


@pytest.mark.parametrize("preset", PRESETS, ids=lambda p: p.key)
def test_every_preset_builds_and_renders(qt_app, bus: DataBus, preset) -> None:
    widget = preset.build(preset.key)
    assert widget.id == preset.key
    if getattr(widget, "path", None) == "":
        # A picture widget with no picture yet. It draws nothing on purpose --
        # a placeholder would end up burned into a recording -- and the editor
        # shows the reason beside its settings instead.
        pytest.skip("needs an image file chosen first")
    image = widget.image(bus, 1080)
    assert image is not None and not image.isNull(), (
        f"{preset.key} rendered nothing with a fully populated bus"
    )
    assert image.width() > 0 and image.height() > 0


@pytest.mark.parametrize("preset", PRESETS, ids=lambda p: p.key)
def test_every_preset_round_trips_through_a_show_file(qt_app, preset) -> None:
    """A layout is only useful if it survives being saved."""
    original = preset.build(preset.key)
    restored = widget_from_dict(widget_to_dict(original))
    assert restored is not None, f"{preset.key} could not be rebuilt"
    assert restored.id == original.id
    assert restored.placement == original.placement
    assert restored.style.size == pytest.approx(original.style.size)


@pytest.mark.parametrize("preset", PRESETS, ids=lambda p: p.key)
def test_every_preset_copes_with_an_empty_bus(qt_app, preset) -> None:
    """Before the console connects, nothing may crash or draw nonsense."""
    widget = preset.build(preset.key)
    widget.image(DataBus(), 1080)  # must not raise


@pytest.mark.parametrize("preset", PRESETS, ids=lambda p: p.key)
def test_declared_reads_match_what_the_widget_subscribes_to(qt_app, preset) -> None:
    """The UI shows preset.reads; it must not be a lie."""
    if not preset.reads:
        return
    widget = preset.build(preset.key)
    subscribed = set(widget.bus_keys)
    for key in preset.reads:
        assert key in subscribed, (
            f"{preset.key} claims to read {key} but does not subscribe to it"
        )


def test_unique_id_avoids_collisions() -> None:
    assert unique_id("clock", []) == "clock"
    assert unique_id("clock", ["clock"]) == "clock2"
    assert unique_id("clock", ["clock", "clock2"]) == "clock3"


def test_make_widget_gives_a_fresh_id(qt_app) -> None:
    first = make_widget("clock_24", [])
    second = make_widget("clock_24", [first.id])
    assert first.id != second.id


def test_make_widget_of_nothing_is_none(qt_app) -> None:
    assert make_widget("no_such_preset", []) is None


# ------------------------------------------------------------- status lamp


def test_status_lamp_switches_appearance(qt_app) -> None:
    from wer.overlay.widgets import StatusWidget

    bus = DataBus()
    lamp = StatusWidget("lamp", "eos.connected", on_text="Eos", off_text="Eos lost")

    bus.publish("eos.connected", True, source_connection_id="t")
    assert lamp.is_on(bus) is True

    bus.publish("eos.connected", False, source_connection_id="t")
    assert lamp.is_on(bus) is False


def test_status_lamp_reads_string_truthiness_sanely(qt_app) -> None:
    """A show file or a protocol without booleans can hand us "false"."""
    from wer.overlay.widgets import StatusWidget

    bus = DataBus()
    lamp = StatusWidget("lamp", "k")
    for value, expected in (("true", True), ("1", True), ("on", True),
                            ("false", False), ("0", False), ("", False)):
        bus.publish("k", value, source_connection_id="t")
        assert lamp.is_on(bus) is expected, f"{value!r} read wrong"


def test_a_lamp_that_hides_when_off_draws_nothing(qt_app) -> None:
    """The REC indicator should be absent, not greyed, when not recording."""
    from wer.overlay.widgets import StatusWidget

    bus = DataBus()
    bus.publish("clock.recording", False, source_connection_id="t")
    lamp = StatusWidget("rec", "clock.recording", on_text="REC",
                        hide_when_off=True)
    assert lamp.image(bus, 1080) is None

    bus.publish("clock.recording", True, source_connection_id="t")
    lamp.notify_changed()
    assert lamp.image(bus, 1080) is not None


def test_visibility_keys_are_subscribed_to(qt_app) -> None:
    """A widget shown "only when X" must be told when X changes.

    Without this the compositor never subscribes to the condition, so the
    widget is not marked dirty and the editor's "Reads:" line is wrong.
    """
    from wer.overlay.widgets import TextWidget, VisibilityRule

    widget = TextWidget(
        "w", "{a.b}",
        visibility=VisibilityRule(only_when="c.d", only_when_present="e.f"),
    )
    assert set(widget.bus_keys) == {"a.b", "c.d", "e.f"}


def test_a_key_used_for_both_content_and_visibility_is_listed_once(qt_app) -> None:
    """Otherwise the widget subscribes twice and re-renders twice per change."""
    from wer.overlay.widgets import TextWidget, VisibilityRule

    widget = TextWidget(
        "w", "{a.b}", visibility=VisibilityRule(only_when_present="a.b")
    )
    assert widget.bus_keys == ["a.b"]


# ------------------------------------------ previewing before a console exists


@pytest.mark.parametrize("preset", PRESETS, ids=lambda p: p.key)
def test_every_preset_draws_on_the_sample_bus(qt_app, preset) -> None:
    """The sample bus is what a layout is previewed against before the console
    is connected. A preset that draws nothing on it previews as a hole exactly
    where the widget will be."""
    from wer.overlay.sample import sample_bus

    widget = preset.build(preset.key)
    if getattr(widget, "path", None) == "":
        pytest.skip("needs an image file chosen first")
    image = widget.image(sample_bus(), 1080, 1920)
    assert image is not None and not image.isNull(), f"{preset.key} drew nothing"


def test_the_sample_bus_has_a_value_for_every_key_a_preset_reads(qt_app) -> None:
    """A missing key previews as "--", in exactly the place a real value would
    have shown how wide the widget really gets."""
    from wer.overlay.sample import sample_bus

    bus = sample_bus()
    for preset in PRESETS:
        for key in preset.build(preset.key).bus_keys:
            assert bus.value(key) is not None, (
                f"{preset.key} reads {key}, and the sample bus has nothing for it"
            )


def test_nothing_on_the_sample_bus_expires() -> None:
    """A preview left open while a layout is fiddled with must not decay into
    "--" a few seconds in, which would read as the layout breaking."""
    from wer.overlay.sample import sample_bus

    assert all(entry.stale_after is None for entry in sample_bus().snapshot())


def test_the_sample_frame_has_dark_and_bright_areas() -> None:
    """Text over flat black flatters every style. Legibility over a bright
    stage is the whole game, so the stand-in picture needs a dark area and a
    lit one for a preview to show anything at all."""
    import numpy as np

    from wer.overlay.sample import sample_frame

    frame = sample_frame(960, 540)
    assert frame.shape == (540, 960, 3)
    assert frame.dtype == np.uint8
    assert frame.min() < 40, "nothing dark in it"
    assert frame.max() > 200, "nothing bright in it"
    assert np.array_equal(frame, sample_frame(960, 540)), (
        "two previews of the same layout drew different pictures"
    )


def test_the_sample_module_does_not_pull_in_qt() -> None:
    """Believable values and a stand-in picture need no Qt, and a show-file
    tool that only wants them should not have to start it. Run in a subprocess
    for the reason test_architecture gives: once any test has imported Qt, an
    in-process check passes whatever happens."""
    import subprocess
    import sys
    from pathlib import Path

    script = (
        "import sys, wer.overlay.sample\n"
        "print(';'.join(sorted(m for m in sys.modules "
        "if m.split('.')[0] in {'PySide6', 'shiboken6'})))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout.strip(), f"pulled Qt in: {result.stdout.strip()}"
