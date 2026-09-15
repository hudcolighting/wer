"""A/V offsets kept for each camera and audio input, and sticking once set.

Hudson, 12 Sep 2026: Wer proposes an offset, and once he changes it, it sticks
to what he said -- for the camera and audio input it was set for, since the
offset belongs to the devices. These pin that, the upgrade from a show file
that had one offset for everything, and the Recording tab showing it.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from wer.core.avsync import (
    AUTOMATIC,
    AUTOMATIC_OFFSET_MS,
    CLAP,
    NO_SOUND,
    TYPED,
    describe_offset,
    offset_for,
    set_offset,
    sync_legacy_offset,
    use_automatic,
)
from wer.core.showfile import AvOffset, RecordingConfig, ShowFile, load_show, migrate, save_show

BLACKMAGIC = "Blackmagic WDM Capture"
LINE_IN = "Line In (Blackmagic UltraStudio Recorder 3G Audio)"
WEBCAM = "Integrated Camera"
MIC = "Microphone Array (Realtek(R) Audio)"


# ----------------------------------------------------------------- the model


def test_a_pairing_with_nothing_set_uses_the_automatic_value() -> None:
    offset = offset_for(RecordingConfig(audio_device=LINE_IN), BLACKMAGIC)
    assert (offset.value_ms, offset.source) == (AUTOMATIC_OFFSET_MS, AUTOMATIC)


def test_a_set_offset_sticks_to_its_own_camera_and_audio_input_only() -> None:
    config = RecordingConfig(audio_device=LINE_IN)
    set_offset(config, BLACKMAGIC, -60)
    assert offset_for(config, BLACKMAGIC).value_ms == -60
    assert offset_for(config, BLACKMAGIC).source == TYPED
    assert offset_for(config, WEBCAM).source == AUTOMATIC
    config.audio_device = MIC
    assert offset_for(config, BLACKMAGIC).source == AUTOMATIC
    config.audio_device = LINE_IN
    assert offset_for(config, BLACKMAGIC).value_ms == -60


def test_setting_again_replaces_rather_than_adding_a_second_entry() -> None:
    config = RecordingConfig(audio_device=LINE_IN)
    set_offset(config, BLACKMAGIC, -60)
    set_offset(config, BLACKMAGIC, -40)
    assert len(config.av_offsets) == 1 and config.av_offsets[0].offset_ms == -40


def test_with_no_sound_there_is_nothing_to_line_up() -> None:
    offset = offset_for(RecordingConfig(audio_device=None), BLACKMAGIC)
    assert (offset.value_ms, offset.source) == (0, NO_SOUND)
    assert describe_offset(offset) == "No sound is recorded, so there is nothing to line up."


def test_going_back_to_automatic_forgets_only_that_pairing() -> None:
    config = RecordingConfig(audio_device=LINE_IN)
    set_offset(config, BLACKMAGIC, -60)
    set_offset(config, WEBCAM, 30)
    assert use_automatic(config, BLACKMAGIC) is True
    assert offset_for(config, BLACKMAGIC).source == AUTOMATIC
    assert offset_for(config, WEBCAM).value_ms == 30
    assert use_automatic(config, BLACKMAGIC) is False, "nothing left to forget"


def test_values_are_kept_inside_the_recording_tabs_range() -> None:
    config = RecordingConfig(audio_device=LINE_IN)
    set_offset(config, BLACKMAGIC, 5000)
    assert offset_for(config, BLACKMAGIC).value_ms == 2000


def test_a_clap_measurement_remembers_what_it_measured_and_says_so() -> None:
    config = RecordingConfig(audio_device=LINE_IN)
    set_offset(
        config, BLACKMAGIC, -62, source=CLAP, measured_lag_ms=62.4, recorded_offset_ms=0,
        claps=6, frame_period_ms=33.3, now=datetime(2026, 9, 13, 10, 30),
    )
    offset = offset_for(config, BLACKMAGIC)
    assert offset.source == CLAP and offset.entry.measured_lag_ms == 62.4
    assert describe_offset(offset) == "Measured by a clap test on 13 Sep 2026, from 6 claps."
    set_offset(config, BLACKMAGIC, -50, now=datetime(2026, 9, 14, 9, 0))
    offset = offset_for(config, BLACKMAGIC)
    assert offset.source == TYPED and offset.entry.measured_lag_ms is None, (
        "a typed value kept the clap test's reading"
    )
    assert describe_offset(offset) == "Set by you for this camera and audio input on 14 Sep 2026."


def test_the_value_an_older_wer_would_read_is_never_the_automatic_one() -> None:
    """An older Wer reads and saves only av_offset_ms, and an upgrade reads a
    non-zero one as the operator's own. An automatic estimate written there
    would come back as a choice nobody made."""
    config = RecordingConfig(audio_device=LINE_IN)
    set_offset(config, BLACKMAGIC, -60)
    assert config.av_offset_ms == -60
    sync_legacy_offset(config, WEBCAM)
    assert config.av_offset_ms == 0


# --------------------------------------------------------------- the show file


def test_offsets_round_trip(tmp_path: Path) -> None:
    show = ShowFile()
    show.recording.audio_device = LINE_IN
    set_offset(show.recording, BLACKMAGIC, -62, source=CLAP, measured_lag_ms=62.0, claps=6)
    set_offset(show.recording, WEBCAM, 20)
    path = tmp_path / "show.wer"
    save_show(show, path)
    loaded = load_show(path).recording
    assert [(e.camera, e.audio, e.offset_ms, e.source) for e in loaded.av_offsets] == [
        (BLACKMAGIC, LINE_IN, -62, CLAP),
        (WEBCAM, LINE_IN, 20, TYPED),
    ]
    assert all(isinstance(entry, AvOffset) for entry in loaded.av_offsets)
    assert loaded.av_offsets[0].claps == 6


def _v2(recording: dict, camera: dict | None = None) -> dict:
    return {"schema_version": 2, "recording": recording, "camera": camera or {}}


def test_a_v2_file_with_an_offset_keeps_it_as_typed_for_the_devices_it_was_saved_with() -> None:
    """The spin box was the only thing that ever wrote a v2 offset, so a
    non-zero one was the operator's."""
    data = migrate(_v2({"audio_device": LINE_IN, "av_offset_ms": -80}, {"device_name": BLACKMAGIC}))
    assert data["recording"]["av_offsets"] == [
        {"camera": BLACKMAGIC, "audio": LINE_IN, "offset_ms": -80, "source": "typed", "set_at": ""}
    ]


def test_a_v2_file_with_no_offset_is_left_on_automatic() -> None:
    """Zero in a v2 file cannot say whether anyone chose it, and never having
    chosen is what the automatic value now stands for."""
    data = migrate(_v2({"audio_device": LINE_IN, "av_offset_ms": 0}, {"device_name": BLACKMAGIC}))
    assert not data["recording"].get("av_offsets")


def test_nulls_and_junk_in_the_offsets_do_not_stop_a_show_loading(tmp_path: Path) -> None:
    path = tmp_path / "show.wer"
    path.write_text(json.dumps({
        "schema_version": 3,
        "recording": {
            "audio_device": LINE_IN,
            "av_offset_ms": None,
            "av_offsets": [
                None,
                "not an entry",
                {"camera": BLACKMAGIC, "audio": LINE_IN, "offset_ms": "not a number"},
                {"camera": WEBCAM, "audio": LINE_IN, "offset_ms": -30, "source": None, "made_up": 1},
            ],
        },
    }), encoding="utf-8")
    recording = load_show(path).recording
    assert recording.av_offset_ms == 0
    assert [(e.camera, e.offset_ms, e.source) for e in recording.av_offsets] == [(WEBCAM, -30, TYPED)]


def test_a_null_offsets_list_loads_as_none_set(tmp_path: Path) -> None:
    path = tmp_path / "show.wer"
    path.write_text(json.dumps({"schema_version": 3, "recording": {"av_offsets": None}}), encoding="utf-8")
    assert load_show(path).recording.av_offsets == []


# ------------------------------------------------------------ the Recording tab


def _wait_until(qt_app, predicate, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "the panel's worker never answered"
        qt_app.processEvents()
        time.sleep(0.01)


@pytest.fixture
def panel(qt_app, monkeypatch):
    from wer.ui import record_panel as module
    from wer.video.devices import AudioDevice

    monkeypatch.setattr(module, "detect_encoders", lambda **_kwargs: [])
    monkeypatch.setattr(
        module, "enumerate_audio_devices", lambda: [AudioDevice(name=LINE_IN), AudioDevice(name=MIC)]
    )
    monkeypatch.setattr(module, "probe_audio_formats", lambda _device: [])
    built = module.RecordPanel(RecordingConfig(audio_device=LINE_IN))
    built.start_audio_inputs()
    _wait_until(qt_app, lambda: not built.audio_inputs_pending)
    built.set_camera(BLACKMAGIC)
    built.changes = []
    built.settings_changed.connect(lambda: built.changes.append(True))
    return built


def test_the_tab_shows_the_automatic_value_until_the_operator_changes_it(panel) -> None:
    assert panel._offset.value() == AUTOMATIC_OFFSET_MS
    assert panel._offset_source.text().startswith("Automatic")
    assert panel._offset_automatic.isHidden()
    assert panel.changes == [], "showing the offset was reported as a settings change"


def test_an_operator_change_sticks_to_the_devices_and_is_saved(panel) -> None:
    panel._offset.setValue(-60)  # as an arrow press or typing would
    assert panel.changes, "the change was not handed on to be saved"
    assert offset_for(panel.config, BLACKMAGIC).value_ms == -60
    assert panel._offset_source.text().startswith("Set by you")
    assert not panel._offset_automatic.isHidden()

    panel.set_camera(WEBCAM)
    assert panel._offset.value() == AUTOMATIC_OFFSET_MS
    panel.set_camera(BLACKMAGIC)
    assert panel._offset.value() == -60


def test_switching_the_camera_is_not_a_settings_change(panel) -> None:
    panel.set_camera(WEBCAM)
    panel.set_camera(BLACKMAGIC)
    assert panel.changes == []
    assert panel.config.av_offsets == []


def test_changing_the_audio_input_shows_that_pairings_offset(panel) -> None:
    panel._offset.setValue(-60)
    panel._audio_box.setCurrentIndex(panel._audio_box.findData(MIC))
    assert panel._offset.value() == AUTOMATIC_OFFSET_MS
    panel._audio_box.setCurrentIndex(panel._audio_box.findData(LINE_IN))
    assert panel._offset.value() == -60


def test_use_automatic_goes_back_to_the_automatic_value(panel) -> None:
    panel._offset.setValue(-60)
    panel.changes.clear()
    panel._offset_automatic.click()
    assert panel._offset.value() == AUTOMATIC_OFFSET_MS
    assert offset_for(panel.config, BLACKMAGIC).source == AUTOMATIC
    assert panel.changes, "going back to automatic was not saved"


def test_a_take_is_armed_with_the_offset_for_the_devices_in_use(panel) -> None:
    panel._offset.setValue(-60)
    assert panel.output_settings().av_offset_ms == -60
    panel.set_camera(WEBCAM)
    assert panel.output_settings().av_offset_ms == AUTOMATIC_OFFSET_MS


def test_with_no_sound_the_offset_cannot_be_set(panel) -> None:
    panel._audio_box.setCurrentIndex(panel._audio_box.findData(None))
    assert not panel._offset.isEnabled()
    assert panel._offset_source.text() == "No sound is recorded, so there is nothing to line up."
    assert panel.output_settings().av_offset_ms == 0
