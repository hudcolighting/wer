"""DataBus behaviour. No Qt in this process, by design."""

from __future__ import annotations

import threading
import time

import pytest

from wer.core.databus import MISSING, DataBus


@pytest.fixture()
def bus() -> DataBus:
    return DataBus()


def publish(bus: DataBus, key: str, value: object, **kwargs: object) -> None:
    bus.publish(key, value, source_connection_id="test", **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------ publishing


def test_publish_then_read_back(bus: DataBus) -> None:
    publish(bus, "eos.cue.active.number", "58")
    entry = bus.get("eos.cue.active.number")
    assert entry is not None
    assert entry.value == "58"
    assert entry.type_name == "str"
    assert entry.source_connection_id == "test"


def test_missing_key_reads_as_none(bus: DataBus) -> None:
    assert bus.get("nope") is None
    assert bus.value("nope") is None
    assert bus.value("nope", default=7) == 7
    assert "nope" not in bus


def test_unchanged_value_does_not_fire_but_does_refresh_age(bus: DataBus) -> None:
    """Eos re-sends its whole state constantly; re-rendering on every repeat
    would burn the frame budget for no visible change."""
    calls: list[object] = []
    bus.subscribe("k", lambda entry: calls.append(entry.value))

    publish(bus, "k", 1)
    first = bus.get("k")
    assert first is not None
    # Long enough to clear any plausible clock granularity. perf_counter is
    # sub-microsecond on Windows, but the assertion below is about ordering,
    # not resolution, and a tight sleep here made this test flaky.
    time.sleep(0.05)
    publish(bus, "k", 1)
    second = bus.get("k")
    assert second is not None

    assert calls == [1], "repeat of an identical value should not fire subscribers"
    assert second.updated_at > first.updated_at, "repeat should still refresh age"


def test_force_fires_even_when_unchanged(bus: DataBus) -> None:
    calls: list[object] = []
    bus.subscribe("k", lambda entry: calls.append(entry.value))
    publish(bus, "k", 1)
    publish(bus, "k", 1, force=True)
    assert calls == [1, 1]


def test_a_stale_value_coming_back_unchanged_fires_subscribers(bus: DataBus) -> None:
    """An expired value reads as missing everywhere, so its return is a change
    on screen even when the value is the same. The overlay redraws a widget
    only when told, and a desk back from a drop re-sending its last fade time
    left "--" drawn over it until the value next changed."""
    calls: list[object] = []
    bus.subscribe("k", lambda entry: calls.append(entry.value))

    publish(bus, "k", 40.0, stale_after=0.05)
    time.sleep(0.08)
    assert bus.is_stale("k")
    publish(bus, "k", 40.0, stale_after=0.05)
    assert calls == [40.0, 40.0], "a value back from stale told nobody"


def test_a_repeat_inside_its_window_still_does_not_fire(bus: DataBus) -> None:
    """Eos re-sends its state about once a second through a fade, with a
    three-second window on each fade key. Counting those repeats as changes
    would redraw the overlay on every one for no visible difference."""
    calls: list[object] = []
    bus.subscribe("k", lambda entry: calls.append(entry.value))

    publish(bus, "k", True, stale_after=5.0)
    publish(bus, "k", True, stale_after=5.0)
    assert calls == [True]


# ---------------------------------------------------------------- subscribing


def test_exact_subscription_only_fires_for_its_key(bus: DataBus) -> None:
    seen: list[str] = []
    bus.subscribe("a.b", lambda entry: seen.append(entry.key))
    publish(bus, "a.b", 1)
    publish(bus, "a.c", 1)
    assert seen == ["a.b"]


def test_glob_subscription(bus: DataBus) -> None:
    seen: list[str] = []
    bus.subscribe("sacn.1.ch.*", lambda entry: seen.append(entry.key))
    publish(bus, "sacn.1.ch.42", 255)
    publish(bus, "sacn.1.ch.43", 128)
    publish(bus, "sacn.2.ch.1", 1)
    publish(bus, "eos.cue", "58")
    assert seen == ["sacn.1.ch.42", "sacn.1.ch.43"]


def test_cancelling_a_subscription_stops_callbacks(bus: DataBus) -> None:
    seen: list[str] = []
    subscription = bus.subscribe("k", lambda entry: seen.append(entry.key))
    publish(bus, "k", 1)
    subscription.cancel()
    publish(bus, "k", 2)
    assert seen == ["k"]


def test_glob_subscription_can_be_cancelled(bus: DataBus) -> None:
    seen: list[str] = []
    subscription = bus.subscribe("a.*", lambda entry: seen.append(entry.key))
    publish(bus, "a.x", 1)
    subscription.cancel()
    publish(bus, "a.y", 1)
    assert seen == ["a.x"]


def test_subscriber_exception_does_not_break_the_publisher(bus: DataBus) -> None:
    """A widget that throws must not kill the connection thread feeding it."""
    survivors: list[str] = []

    def explode(entry: object) -> None:
        raise RuntimeError("widget bug")

    bus.subscribe("k", explode)
    bus.subscribe("k", lambda entry: survivors.append(entry.key))
    publish(bus, "k", 1)
    assert survivors == ["k"], "a throwing subscriber blocked the next one"


def test_change_listener_sees_everything(bus: DataBus) -> None:
    """Backs the sidecar log and the raw monitor."""
    seen: list[str] = []
    bus.add_change_listener(lambda entry: seen.append(entry.key))
    publish(bus, "a", 1)
    publish(bus, "b.c.d", 2)
    assert seen == ["a", "b.c.d"]


# --------------------------------------------------------------------- staleness


def test_value_without_stale_after_never_goes_stale(bus: DataBus) -> None:
    publish(bus, "eos.show.name", "Comedy EOS")
    entry = bus.get("eos.show.name")
    assert entry is not None
    assert entry.is_stale() is False


def test_value_goes_stale_after_its_window(bus: DataBus) -> None:
    publish(bus, "eos.cue.fade", 0.5, stale_after=0.05)
    assert bus.is_stale("eos.cue.fade") is False
    time.sleep(0.08)
    assert bus.is_stale("eos.cue.fade") is True


def test_missing_key_counts_as_stale(bus: DataBus) -> None:
    assert bus.is_stale("never.published") is True


def test_touch_source_refreshes_without_firing(bus: DataBus) -> None:
    calls: list[object] = []
    bus.subscribe("k", lambda entry: calls.append(entry))
    publish(bus, "k", 1, stale_after=0.05)
    time.sleep(0.08)
    assert bus.is_stale("k") is True

    assert bus.touch_source("test") == 1
    assert bus.is_stale("k") is False
    assert len(calls) == 1, "touch should not re-fire subscribers"


def test_clear_source_removes_only_that_connection(bus: DataBus) -> None:
    bus.publish("eos.a", 1, source_connection_id="eos")
    bus.publish("sacn.a", 2, source_connection_id="sacn")
    removed = bus.clear_source("eos")
    assert removed == ["eos.a"]
    assert bus.keys() == ["sacn.a"]


def test_taking_keys_off_the_bus_is_counted_though_nobody_is_told(bus: DataBus) -> None:
    """Removing a key fires no callback, so the overlay watches this count to
    know it has something to redraw. A removal it missed left the previous
    console's cue label drawn as current after a console switch."""
    calls: list[object] = []
    bus.subscribe("eos.a", lambda entry: calls.append(entry.value))
    bus.publish("eos.a", 1, source_connection_id="eos")
    start = bus.removals

    bus.clear_source("nobody")
    assert bus.removals == start, "a clear that removed nothing was counted"
    bus.clear_source("eos")
    assert bus.removals == start + 1
    bus.publish("sacn.a", 2, source_connection_id="sacn")
    bus.clear()
    assert bus.removals == start + 2, "clearing the whole bus was not counted"
    assert calls == [1], "a removal is not a change to announce"


# --------------------------------------------------------------------- templates


def test_render_substitutes_bus_values(bus: DataBus) -> None:
    publish(bus, "eos.cue.active.number", "58")
    publish(bus, "eos.cue.active.label", "Adriana Xs Center")
    rendered = bus.render(
        "Cue {eos.cue.active.number} - {eos.cue.active.label}"
    )
    assert rendered == "Cue 58 - Adriana Xs Center"


def test_render_marks_missing_keys_rather_than_blanking(bus: DataBus) -> None:
    """A widget must visibly say it has no data."""
    assert bus.render("Cue {eos.cue.active.number}") == f"Cue {MISSING}"


def test_render_marks_stale_keys(bus: DataBus) -> None:
    """A console dropout must not leave the last cue number sitting on screen."""
    publish(bus, "eos.cue.active.number", "58", stale_after=0.05)
    assert bus.render("{eos.cue.active.number}") == "58"
    time.sleep(0.08)
    assert bus.render("{eos.cue.active.number}") == MISSING


def test_render_leaves_unrecognised_braces_alone(bus: DataBus) -> None:
    assert bus.render("100% {not a key}") == "100% {not a key}"


def test_template_keys_are_extracted_in_order_without_duplicates(bus: DataBus) -> None:
    keys = DataBus.template_keys("{a.b} {c.d} {a.b}")
    assert keys == ["a.b", "c.d"]


# --------------------------------------------------------------------- threading


def test_concurrent_publishes_do_not_lose_updates(bus: DataBus) -> None:
    """Every connection runs on its own thread."""
    writers = 8
    per_writer = 200

    def write(index: int) -> None:
        for n in range(per_writer):
            bus.publish(f"t.{index}.{n}", n, source_connection_id=f"c{index}")

    threads = [threading.Thread(target=write, args=(i,)) for i in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(bus) == writers * per_writer


def test_callback_may_publish_without_deadlocking(bus: DataBus) -> None:
    """Callbacks run outside the lock precisely so this is legal."""
    bus.subscribe(
        "in",
        lambda entry: bus.publish(
            "out", entry.value * 2, source_connection_id="derived"
        ),
    )
    publish(bus, "in", 21)
    assert bus.value("out") == 42


def test_value_agrees_with_render_about_staleness() -> None:
    """They used to disagree: render() substituted the missing marker for an
    expired key while value() handed back the expired number, so a widget drew
    as live what the text beside it had already given up on."""
    import time

    bus = DataBus()
    bus.publish("fade", 42, source_connection_id="t", stale_after=0.15)
    assert bus.value("fade") == 42
    assert bus.render("{fade}") == "42"

    time.sleep(0.35)
    assert bus.value("fade") is None
    assert bus.render("{fade}") != "42"


def test_a_key_with_no_expiry_is_never_withheld() -> None:
    """Most of the bus has no TTL, and this change must not touch it."""
    import time

    bus = DataBus()
    bus.publish("show", "The Comedy of Errors", source_connection_id="t")
    time.sleep(0.2)
    assert bus.value("show") == "The Comedy of Errors"


def test_the_last_known_value_is_still_reachable_on_purpose() -> None:
    import time

    bus = DataBus()
    bus.publish("fade", 42, source_connection_id="t", stale_after=0.15)
    time.sleep(0.35)
    assert bus.value("fade") is None
    assert bus.value("fade", allow_stale=True) == 42
