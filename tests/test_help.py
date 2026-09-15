"""The in-app help.

Some sections are generated from code -- the widget catalogue, the layout
catalogue and the remote control command table -- and others are built from
lists kept in help_content.py, so these tests mostly check that those parts
really do track the code, and that the hand-written parts do not contradict it.

Documentation nobody tests is documentation that quietly goes wrong.
"""

from __future__ import annotations

import re

import pytest

from wer.ui.help_content import SHORTCUTS, topics


@pytest.fixture()
def all_topics(qt_app):
    return topics()


def body_of(all_topics, key: str) -> str:
    return next(t for t in all_topics if t.key == key).body


# --------------------------------------------------------------- completeness


def test_every_major_feature_has_a_topic(all_topics) -> None:
    keys = {topic.key for topic in all_topics}
    assert {
        "start", "eos", "camera", "consoles", "recording", "widgets", "editing",
        "layouts", "markers", "monitor", "keys", "shortcuts", "files",
        "trouble", "osccontrol", "sacn",
    } <= keys


def test_topics_have_unique_keys(all_topics) -> None:
    keys = [topic.key for topic in all_topics]
    assert len(keys) == len(set(keys))


def test_every_topic_has_a_title_and_a_body(all_topics) -> None:
    for topic in all_topics:
        assert topic.title.strip()
        assert len(topic.body.strip()) > 200, f"{topic.key} is too thin to help"


def test_every_topic_is_in_a_section(all_topics) -> None:
    for topic in all_topics:
        assert topic.section, f"{topic.key} has no section"


# ------------------------------------------------- generated, so cannot rot


def test_the_widget_catalogue_lists_every_preset(all_topics) -> None:
    """Generated from wer.overlay.presets, so a new widget documents itself."""
    from wer.overlay.presets import PRESETS

    body = body_of(all_topics, "widgets")
    for preset in PRESETS:
        assert preset.label in body, f"{preset.label} is missing from the help"


def test_the_widgets_the_user_asked_for_are_documented(all_topics) -> None:
    body = body_of(all_topics, "widgets")
    assert "Cue duration" in body
    assert "Date" in body
    assert "Record elapsed" in body


def test_the_shortcut_table_matches_the_real_shortcuts(all_topics) -> None:
    body = body_of(all_topics, "shortcuts")
    for keys, _ in SHORTCUTS:
        assert keys in body


def test_documented_shortcuts_are_the_ones_the_window_binds() -> None:
    """Help that lists a shortcut the program does not have is worse than none."""
    from pathlib import Path

    source = Path("src/wer/ui/main_window.py").read_text(encoding="utf-8")
    bound = set(re.findall(r'QKeySequence\("([^"]+)"\)', source))
    bound |= set(re.findall(r'setShortcut\("([^"]+)"\)', source))

    documented = {keys for keys, _ in SHORTCUTS}
    # Ctrl+1..9 is documented as a range and bound in a loop.
    documented.discard("Ctrl+1 … Ctrl+9")

    missing = documented - bound
    assert not missing, f"documented but not bound: {sorted(missing)}"


# ------------------------------------------------- agrees with real behaviour


def test_the_eos_topic_gives_the_port_that_actually_works(all_topics) -> None:
    """3032 was established against a real console, not read off a datasheet."""
    body = body_of(all_topics, "eos")
    assert "3032" in body


def test_the_eos_topic_leads_with_the_actual_common_failure(all_topics) -> None:
    """OSC TX off is by far the most likely reason it looks broken."""
    body = body_of(all_topics, "eos").lower()
    assert "osc tx" in body
    assert "waiting for data" in body


def test_the_status_names_in_help_match_the_code(all_topics) -> None:
    from wer.connections.base import ConnectionState

    body = body_of(all_topics, "eos")
    for state in ConnectionState:
        assert state.value in body, f"status {state.value!r} is not explained"


def test_quality_presets_in_help_match_the_code(all_topics) -> None:
    from wer.video.encoder import QUALITY_PRESETS

    body = body_of(all_topics, "recording")
    for preset in QUALITY_PRESETS:
        assert preset.label in body


def test_filename_tokens_in_help_match_the_code(all_topics) -> None:
    from wer.core.showfile import render_filename

    body = body_of(all_topics, "recording")
    for token in ("{show}", "{date}", "{time}", "{take}"):
        assert token in body
        # And the token really is substituted, not just documented.
        assert "{" not in render_filename(token, show_name="X", take=1)


def test_bus_keys_in_help_are_real_keys(all_topics) -> None:
    """A help page that invents key names sends people chasing nothing.

    Checked by replaying the real console captures rather than hand-made
    messages, so "this key exists" means a real ETCnomad actually produced it.
    """
    import json
    from pathlib import Path

    from wer.connections.eos_parser import EosParser
    from wer.connections.osc import OscMessage
    from wer.core.databus import DataBus

    fixtures = Path(__file__).parent / "fixtures" / "eos"
    bus = DataBus()
    parser = EosParser()
    for path in sorted(fixtures.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            message = OscMessage(
                address=record["address"],
                args=tuple(
                    a for a in record["args"] if not isinstance(a, dict)
                ),
                typetags=record.get("typetags", ""),
            )
            for update in parser.handle(message).updates:
                bus.publish(update.key, update.value, source_connection_id="eos")

    body = body_of(all_topics, "keys")
    documented = set(re.findall(r"<code>(eos\.[a-z0-9._]+)</code>", body))
    documented = {k for k in documented if "&lt;" not in k}

    # eos.connected is published by the connection rather than the parser.
    real = set(bus.keys()) | {"eos.connected"}
    invented = documented - real
    assert not invented, (
        f"help documents keys no real console produced: {sorted(invented)}"
    )


def test_clock_keys_in_help_are_real_keys(all_topics) -> None:
    """The same check for the clock, which publishes its own set."""
    import time

    from wer.connections.builtin import SystemConnection
    from wer.core.databus import DataBus

    bus = DataBus()
    connection = SystemConnection("system", bus)
    connection.start()
    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline and "clock.wall" not in bus:
        time.sleep(0.02)
    connection.stop()

    body = body_of(all_topics, "keys")
    documented = set(re.findall(r"<code>(clock\.[a-z0-9._]+)</code>", body))
    invented = documented - set(bus.keys())
    assert not invented, f"help documents clock keys that do not exist: {sorted(invented)}"


def test_the_help_quotes_the_silence_warning_the_recorder_really_says(
    all_topics,
) -> None:
    """Two topics quote the mid-take warning word for word, and the length in
    it is a number the recorder owns. A quote that has drifted from the message
    is worse than no quote: whoever is reading the help is looking for those
    words on their own status bar."""
    from wer.video.recorder import SOUND_SILENT_SECONDS

    tail = f"only digital silence for {SOUND_SILENT_SECONDS:.0f} s"
    for key in ("recording", "trouble"):
        body = " ".join(body_of(all_topics, key).replace("&nbsp;", " ").split())
        assert "No sound from" in body, key
        assert tail in body, key


def test_troubleshooting_covers_the_failures_we_know_happen(all_topics) -> None:
    body = body_of(all_topics, "trouble").lower()
    for symptom in ("osc tx", "encoder drops", "mjpg", "command line", "log"):
        assert symptom in body


# ------------------------------------------------------------------- window


def test_the_help_window_opens_at_a_topic(qt_app) -> None:
    from wer.ui.help_window import HelpWindow

    window = HelpWindow()
    window.goto("markers")
    assert "Markers" in window._body.toPlainText()
    window.deleteLater()


def test_search_narrows_the_list(qt_app) -> None:
    from wer.ui.help_window import HelpWindow

    window = HelpWindow()
    before = window._list.count()
    window._search.setText("mjpg")
    after = window._list.count()
    assert 0 < after < before
    window.deleteLater()


def test_search_for_something_absent_says_so(qt_app) -> None:
    from wer.ui.help_window import HelpWindow

    window = HelpWindow()
    window._search.setText("xyzzy-not-a-real-thing")
    assert "Nothing found" in window._list.item(0).text()
    window.deleteLater()


def test_every_topic_renders_without_losing_its_markup(qt_app, all_topics) -> None:
    """Malformed HTML shows up as visible tags or a blank pane."""
    from wer.ui.help_window import HelpWindow

    window = HelpWindow()
    for topic in all_topics:
        window.goto(topic.key)
        text = window._body.toPlainText()
        assert len(text) > 100, f"{topic.key} rendered almost nothing"
        assert "<p>" not in text, f"{topic.key} has markup showing as text"
        assert "<table" not in text
    window.deleteLater()


def test_snapshots_are_documented(all_topics) -> None:
    keys = {topic.key for topic in all_topics}
    assert "snapshots" in keys

    body = body_of(all_topics, "snapshots")
    assert "F12" in body
    # The point that most needs stating: it does not depend on recording.
    assert "whether or not you are recording" in body.lower()


def test_snapshot_tokens_in_help_match_the_code(all_topics) -> None:
    from wer.core.showfile import RecordingConfig, render_filename

    body = body_of(all_topics, "snapshots")
    assert RecordingConfig().snapshot_template in body
    assert "{cue}" in body
    assert "{" not in render_filename("{cue}", cue="58")


def test_remote_control_is_documented(all_topics) -> None:
    keys = {topic.key for topic in all_topics}
    assert "osccontrol" in keys


def test_the_osc_command_table_lists_every_command(all_topics) -> None:
    """Generated from the code, so a new command documents itself."""
    from wer.connections.osc_control import COMMANDS

    body = body_of(all_topics, "osccontrol")
    for command in COMMANDS:
        assert command.address in body, f"{command.address} missing from the help"


def test_the_remote_control_help_says_there_is_no_port_to_set(all_topics) -> None:
    """The separate listener this replaced is the one a desk never reached,
    and help that still has someone matching a port repeats that."""
    body = body_of(all_topics, "osccontrol").lower()
    assert "eos connection" in body
    assert "no port to choose" in body


def test_the_remote_control_help_says_a_burst_of_taps_is_one_press(all_topics) -> None:
    body = body_of(all_topics, "osccontrol").lower()
    assert "within a second" in body
    assert "stop and then start does both" in body


def test_the_help_does_not_promise_macros_over_udp(all_topics) -> None:
    """Only TCP 3032, from Eos on the laptop, was measured. The help once
    said commands reach Wer whenever cues do, and the desk the project knows
    best sends its cues over UDP."""
    for key in ("eos", "osccontrol"):
        body = body_of(all_topics, key).lower()
        assert "udp" in body and "not been tested" in body, key


def test_the_disk_stop_behaviour_is_documented(all_topics) -> None:
    """The user must know the recording stops itself, and at what level."""
    from wer.core.showfile import RecordingConfig

    body = body_of(all_topics, "recording")
    config = RecordingConfig()
    assert f"{config.low_disk_stop_gb:.0f} GB" in body
    assert "stops itself" in body.lower() or "stops automatically" in body.lower()


def test_the_continuation_behaviour_is_documented(all_topics) -> None:
    body = body_of(all_topics, "recording").lower()
    assert "part2" in body
    assert "carries on" in body or "continues" in body


def test_saved_consoles_are_documented(all_topics) -> None:
    body = body_of(all_topics, "consoles").lower()
    for word in ("duplicate", "rename", "delete", "notes"):
        assert word in body


def test_the_help_explains_why_a_silent_console_does_not_count(all_topics) -> None:
    """The one thing that makes the search worth having, so it must be said."""
    body = body_of(all_topics, "consoles").lower()
    assert "silent" in body
    assert "osc" in body
    assert "does <b>not</b> count as found" in body


def test_the_help_says_the_most_recent_console_is_tried_first(all_topics) -> None:
    body = body_of(all_topics, "consoles").lower()
    assert "most recently" in body


def test_the_startup_switches_in_help_exist_in_the_settings(all_topics) -> None:
    """Help that describes a checkbox nobody can find is worse than none."""
    from wer.core.showfile import EosConfig

    body = body_of(all_topics, "consoles").lower()
    assert "try every saved console on startup" in body
    config = EosConfig()
    assert hasattr(config, "try_all_on_startup")
    assert hasattr(config, "auto_connect")


def test_the_eos_topic_points_at_the_saved_consoles_topic(all_topics) -> None:
    assert "saved consoles" in body_of(all_topics, "eos").lower()


def test_the_help_warns_that_the_port_does_not_imply_the_framing(all_topics) -> None:
    """Learned the hard way on a real Ion XE: UDP 8123 carried the traffic
    while both TCP defaults sat silent, and 3032 answered in 1.1 not 1.0."""
    body = body_of(all_topics, "consoles").lower()
    assert "8123" in body
    assert "not in this framing" in body


def test_the_console_setup_names_the_settings_that_actually_matter(all_topics) -> None:
    """Every one of these cost real time to discover on a live Ion XE: the TCP
    port is settable and not always 3032, and the framing is not implied by the
    port. Macros once needed a socket of their own as well; they share the link
    now."""
    body = body_of(all_topics, "eos")
    assert "OSC TCP Server Port" in body
    assert "OSC TCP Format" in body
    assert "answered, but not in this framing" in body
    assert "share one link" in body


def test_the_help_does_not_send_macros_to_a_port_of_their_own(all_topics) -> None:
    body = body_of(all_topics, "eos").lower()
    assert "separate socket" not in body
    assert "no second port" in body


def test_the_command_line_user_picker_is_documented(all_topics) -> None:
    """It is a display choice, and it is easy to confuse with the OSC user
    setting that decides what Wer receives at all."""
    body = body_of(all_topics, "eos")
    assert "Command line\nfrom" in body or "Command line" in body
    assert "Any user" in body
    assert "display" in body.lower()


def test_the_picture_flips_in_help_are_the_boxes_on_the_preview_tab(
    qt_app, all_topics
) -> None:
    """Help that names the boxes in other words than the tab does sends
    someone with an upside-down camera looking for controls that are not there."""
    from wer.ui.preview import PreviewPanel

    panel = PreviewPanel()
    try:
        labels = [panel._mirror_box.text(), panel._flip_box.text()]
    finally:
        panel.deleteLater()

    def flattened(key: str) -> str:
        return " ".join(body_of(all_topics, key).split())

    camera, trouble = flattened("camera"), flattened("trouble")
    for label in labels:
        assert f"<b>{label}</b>" in camera, f"{label!r} is not in Camera and picture"
        assert f"<b>{label}</b>" in trouble, f"{label!r} is not in Troubleshooting"
    assert "tick both for a camera mounted upside down" in camera.lower()
    assert "overlay" in camera.lower()


def test_the_troubleshooting_topic_says_where_a_problem_goes(all_topics) -> None:
    """There is no issue tracker, so the address is the whole of the route.

    Taken from wer.licensing rather than typed here, because an address that
    appears in two places is an address that will eventually differ in one.
    """
    from wer.licensing import CONTACT_EMAIL

    body = body_of(all_topics, "trouble")

    assert CONTACT_EMAIL in body
    assert "Open Log Folder" in body, "a report without the log is a guess"


def test_the_sacn_help_names_the_boxes_on_its_tab(qt_app, all_topics) -> None:
    """Help that names the boxes in other words than the tab sends someone
    looking for controls that are not there."""
    from wer.connections.base import ConnectionStatus
    from wer.core.showfile import SacnConfig
    from wer.ui.sacn_panel import SacnPanel

    class Idle:
        status = ConnectionStatus()
        is_running = False

        def refresh_staleness(self) -> None:
            pass

    panel = SacnPanel(
        SacnConfig(), Idle(), list_adapters=lambda: [], route_source_ip=lambda: None
    )
    try:
        labels = [panel._enabled.text(), panel._per_address.text()]
    finally:
        panel._timer.stop()
        panel.deleteLater()
    body = " ".join(body_of(all_topics, "sacn").split())
    for label in labels:
        assert f"<b>{label}</b>" in body, f"{label!r} is not in sACN status output"


def test_the_sacn_help_says_what_a_universe_priority_can_do(all_topics) -> None:
    body = " ".join(body_of(all_topics, "sacn").split()).lower()
    assert "all 512 addresses" in body
    assert "a universe nothing else is sent on" in body
    assert "automatic" in body and "wi-fi" in body

