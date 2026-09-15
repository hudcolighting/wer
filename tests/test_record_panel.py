"""The encoder dropdown must say what is actually going to be used.

This exists because for a long time it did not. "auto" is the default setting
and there was no row for it, so ``findData("auto")`` returned -1, ``max(0, -1)``
selected row zero, and the panel displayed "Software (x264)" while recordings
went out on Quick Sync. Nothing crashed and nothing was logged; it surfaced
only when a three-hour stress run's saved settings disagreed with the ffmpeg
command the recorder had built, and it took an afternoon to believe.
"""

from __future__ import annotations

import pytest

from wer.core.showfile import RecordingConfig
from wer.video.encoder import (
    AUTO_ENCODER,
    EncoderAvailability,
    forget_detected_encoders,
    remember_detected_encoders,
    resolve_encoder_name,
)


def _wait_until(qt_app, predicate, seconds: float = 5.0) -> None:
    """Run the event loop until ``predicate`` holds.

    Encoder detection and the audio check answer by queued signal from their
    own threads, so what they find lands on a later pass of the loop, never
    inside the call that started them.
    """
    import time

    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "the panel's worker never answered"
        qt_app.processEvents()
        time.sleep(0.01)


@pytest.fixture
def panel(qt_app, monkeypatch):
    """A panel whose encoder probe returns a known machine, not this one."""
    from wer.video.encoder import ALL_ENCODERS
    from wer.ui import record_panel as module

    def fake_detection(**_kwargs):
        return [
            EncoderAvailability(e, e.name in ("libx264", "h264_mf"),
                                "" if e.name in ("libx264", "h264_mf") else "no",
                                "NVIDIA" if e.name == "h264_mf" else "")
            for e in ALL_ENCODERS
        ]

    monkeypatch.setattr(module, "detect_encoders", fake_detection)
    monkeypatch.setattr(module, "probe_audio_formats", lambda _device: [])
    forget_detected_encoders()
    built = module.RecordPanel(RecordingConfig())
    _wait_until(qt_app, lambda: not built.encoders_pending)
    yield built
    forget_detected_encoders()


def test_automatic_is_an_option_the_dropdown_can_show(panel) -> None:
    assert panel._encoder_box.findData(AUTO_ENCODER) >= 0


def test_the_default_setting_is_what_the_dropdown_displays(panel) -> None:
    """The specific bug: config says auto, the box says software."""
    assert panel.config.encoder == AUTO_ENCODER
    assert panel._encoder_box.currentData() == AUTO_ENCODER


def test_automatic_names_the_encoder_it_picked(panel) -> None:
    """"Automatic" on its own is not enough to trust. Someone about to record
    a four-hour tech should be able to see that it found the dedicated GPU
    without starting a take to find out."""
    label = panel._encoder_box.itemText(panel._encoder_box.findData(AUTO_ENCODER))
    assert "Media Foundation" in label
    assert "NVIDIA" in label


def test_what_the_panel_shows_is_what_the_command_will_use(panel) -> None:
    """The two must not be able to drift apart again."""
    assert panel.output_settings().resolve_encoder().name == resolve_encoder_name(
        AUTO_ENCODER
    )


def test_an_explicit_choice_still_shows_itself(panel, qt_app) -> None:
    from wer.core.showfile import RecordingConfig
    from wer.ui.record_panel import RecordPanel

    explicit = RecordPanel(RecordingConfig(encoder="libx264"))
    _wait_until(qt_app, lambda: not explicit.encoders_pending)
    assert explicit._encoder_box.currentData() == "libx264"


def test_unavailable_encoders_are_still_listed(panel) -> None:
    """An encoder that vanishes from a dropdown teaches the user nothing.
    One that says why it cannot be used is a ten-minute fix."""
    names = [panel._encoder_box.itemData(i) for i in range(panel._encoder_box.count())]
    assert "h264_nvenc" in names
    row = names.index("h264_nvenc")
    assert not panel._encoder_box.model().item(row).isEnabled()


# ------------------------------------------------- the disk floor, mid-take
#
# Same shape of bug as the encoder dropdown above: a control that displays one
# thing while the recording is doing another. These are the disk half of it.


class _FakeStats:
    elapsed = 12.0
    frames_written = 360
    write_fps = 30.0
    file_size_mb = 40.0
    size_sampled_at = 0.0
    frames_dropped = 0
    output_path = None


class _FakeRecorder:
    """Just enough recorder for RecordPanel._refresh.

    The two byte thresholds are the ones the real recorder re-reads every ten
    seconds on its writer thread while a take runs, so they are the values that
    decide whether a recording stops.
    """

    def __init__(self, recording: bool = True) -> None:
        from wer.video.recorder import RecorderState

        self.is_recording = recording
        self.state = RecorderState.RECORDING if recording else RecorderState.IDLE
        self.detail = ""
        self.queue_depth = 0
        self.queue_capacity = 90
        self.stats = _FakeStats()
        self.low_disk_bytes = 20_000_000_000
        self.stop_below_bytes = 10_000_000_000


def _pump(panel, until, seconds: float = 2.0) -> None:
    """Drive the panel the way its own 500 ms QTimer does, until `until`.

    The free-space measurement happens on a worker thread now, so the answer
    lands on a later tick rather than inside the call that asked for it. Tests
    wait for the state instead of assuming a tick count.
    """
    import time

    deadline = time.monotonic() + seconds
    while True:
        panel._refresh()
        if until() or time.monotonic() > deadline:
            return
        time.sleep(0.02)


@pytest.fixture
def quiet_panel(qt_app, monkeypatch):
    """A panel that probes nothing: no ffmpeg encoder test, no device scan."""
    from wer.ui import record_panel as module

    monkeypatch.setattr(module, "detect_encoders", lambda **_kwargs: [])
    monkeypatch.setattr(module, "enumerate_audio_devices", lambda: [])
    return module.RecordPanel(RecordingConfig())


def test_the_stop_floor_on_screen_is_the_one_being_enforced(quiet_panel) -> None:
    """The spinbox stays live mid-take and its tooltip says "recording stops
    automatically when free space reaches this level". It did not: the floor
    was copied into the recorder once, when the take was armed, so afterwards
    the panel showed one number and the recorder enforced another -- in both
    directions. Raising it to protect a take that was filling the disk did
    nothing at all."""
    recorder = _FakeRecorder(recording=True)
    quiet_panel.attach(recorder)

    quiet_panel._stop_below.setValue(2)

    assert recorder.stop_below_bytes == 2_000_000_000
    assert recorder.low_disk_bytes == 20_000_000_000


def test_the_av_offset_is_locked_once_ffmpeg_has_been_launched(quiet_panel) -> None:
    """Unlike the disk floor, this one really is committed at arm time: the
    offset is an argument in the running ffmpeg command. Leaving it editable
    invited nudging it to fix the sync of a take it cannot reach."""
    quiet_panel.attach(_FakeRecorder(recording=True))
    quiet_panel._refresh()
    assert not quiet_panel._offset.isEnabled()


def test_the_stop_floor_stays_editable_mid_take(quiet_panel) -> None:
    """Deliberately not in the disabled list: it is live policy, not part of
    the ffmpeg command, and an operator watching a disk fill should be able to
    move it."""
    quiet_panel.attach(_FakeRecorder(recording=True))
    quiet_panel._refresh()
    assert quiet_panel._stop_below.isEnabled()


def test_the_disk_readout_keeps_up_during_a_take(quiet_panel, monkeypatch) -> None:
    """It used to freeze at the instant Record was pressed and stay there for
    the whole tech, so the Recording tab confidently reported the headroom of
    four hours ago -- and the amber and red warnings could never appear on the
    one screen someone might be watching."""
    from wer.ui import record_panel as module
    from wer.video.recorder import DiskSpace

    free = {"bytes": 500_000_000_000}
    monkeypatch.setattr(
        module, "check_disk", lambda _path: DiskSpace(free["bytes"], 1_000_000_000_000)
    )
    quiet_panel.attach(_FakeRecorder(recording=True))

    _pump(quiet_panel, lambda: "500.0 GB free" in quiet_panel._disk.text())
    assert "500.0 GB free" in quiet_panel._disk.text()

    free["bytes"] = 3_000_000_000
    # A couple of seconds, not a couple of ticks: the measurement is taken off
    # the GUI thread and at most once a second (see _DiskProbe). What the take
    # needs is that the number moves at all, and that the red warning can
    # appear, which it could not before.
    _pump(quiet_panel, lambda: "3.0 GB free" in quiet_panel._disk.text(), seconds=3.0)
    assert "3.0 GB free" in quiet_panel._disk.text()
    assert "at or below the stop level" in quiet_panel._disk.text()


# ---------------------------------------- the volume that stops answering
#
# The disk readout above is now live for the whole take, which is the point --
# but it means check_disk is reachable from a 500 ms GUI timer slot during the
# four hours that matter. It is not safe there: its own try covers only
# shutil.disk_usage, while the Path.exists() walk in front of it is what
# blocks, measured at 28.7 s on a recording folder whose share had dropped,
# and it then raises OSError. Inline, that froze the window -- preview,
# transport, the Stop Recording button -- in half-minute blocks with a
# traceback per tick, for the rest of the take.


def _blocking_check_disk(release, calls):
    def check(_path):
        calls.append(_path)
        release.wait(30.0)
        raise OSError(22, "The specified network name is no longer available")

    return check


def test_a_dead_volume_cannot_block_or_break_the_timer_slot(
    quiet_panel, monkeypatch
) -> None:
    """The share drops mid-take. The panel must keep answering."""
    import threading
    import time

    from wer.ui import record_panel as module

    release = threading.Event()
    calls = []
    quiet_panel.attach(_FakeRecorder(recording=True))
    _pump(quiet_panel, lambda: "GB free" in quiet_panel._disk.text())

    monkeypatch.setattr(module, "check_disk", _blocking_check_disk(release, calls))
    try:
        worst = 0.0
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            start = time.monotonic()
            quiet_panel._refresh()          # must not raise OSError either
            worst = max(worst, time.monotonic() - start)
            time.sleep(0.05)
        assert worst < 0.5, f"a tick blocked for {worst:.2f} s on a dead volume"
        assert len(calls) == 1, "one measurement in flight, not one per tick"
    finally:
        release.set()


def test_a_drive_that_stops_answering_stops_claiming_a_number(
    quiet_panel, monkeypatch
) -> None:
    """The other half: not blocking is not enough if the panel then shows the
    last good figure forever. A frozen "528 GB free" on a share that has gone
    is the readout lying, which is what this line exists to stop."""
    import threading

    from wer.ui import record_panel as module

    release = threading.Event()
    quiet_panel.attach(_FakeRecorder(recording=True))
    _pump(quiet_panel, lambda: "GB free" in quiet_panel._disk.text())

    monkeypatch.setattr(module, "_DISK_STALE_SECONDS", 0.2)
    monkeypatch.setattr(module, "check_disk", _blocking_check_disk(release, []))
    try:
        _pump(quiet_panel, lambda: "not answering" in quiet_panel._disk.text(),
              seconds=4.0)
        assert "not answering" in quiet_panel._disk.text()
        assert "GB free" not in quiet_panel._disk.text()
    finally:
        release.set()


def test_the_recording_tab_never_touches_the_take_file(quiet_panel, tmp_path) -> None:
    """The stats line is repainted every 500 ms, and it read the take's size
    by statting the file right there in the timer slot -- on a recordings
    folder whose share has dropped, the kind of call measured at half a minute
    before it raises, and it carried on doing it after the take ended.
    _DiskProbe got the free-space measurement off this thread; the size
    readout had been left behind on it. And when the stat failed it said
    "0 MB", which is a number, not a warning."""
    import threading
    import time
    from pathlib import Path

    from wer.video.recorder import Recorder, RecorderState

    touched: list[str] = []

    class _FileOnADroppedShare(type(Path())):
        def stat(self, *args, **kwargs):
            touched.append(threading.current_thread().name)
            raise OSError(64, "The specified network name is no longer available")

    recorder = Recorder()
    recorder.state = RecorderState.RECORDING
    recorder.stats.started_at = time.perf_counter() - 30.0
    recorder.stats.frames_written = 900
    recorder.stats.output_path = _FileOnADroppedShare(tmp_path / "take.mkv")
    quiet_panel.attach(recorder)

    quiet_panel._refresh()

    assert not touched, f"the take file was statted on the {touched[0]} thread"
    assert "0 MB" not in quiet_panel._stats_label.text(), (
        f"a size nobody measured is shown as one: {quiet_panel._stats_label.text()!r}"
    )
    assert "size not measured yet" in quiet_panel._stats_label.text(), (
        f"the readout says nothing about the size: {quiet_panel._stats_label.text()!r}"
    )


def test_a_size_nobody_is_measuring_any_more_is_not_shown_as_current(
    quiet_panel,
) -> None:
    """The other half, as with free space. With the measuring moved to the
    recorder's threads, a writer that is stuck or a drive that has stopped
    answering freezes the figure, and a size that has quietly stopped moving
    on a take that is still running is the readout lying."""
    import time

    from wer.video.recorder import Recorder, RecorderState

    recorder = Recorder()
    recorder.state = RecorderState.RECORDING
    recorder.stats.started_at = time.perf_counter() - 120.0
    recorder.stats.frames_written = 3600
    recorder.stats.note_size(time.perf_counter() - 60.0, 250_000_000)
    quiet_panel.attach(recorder)

    quiet_panel._refresh()
    assert "250 MB" not in quiet_panel._stats_label.text()
    assert "not measured" in quiet_panel._stats_label.text()

    recorder.stats.note_size(time.perf_counter(), 260_000_000)
    quiet_panel._refresh()
    assert "260 MB" in quiet_panel._stats_label.text()


def test_the_red_disk_line_makes_sense_during_a_take(quiet_panel, monkeypatch) -> None:
    """It only became reachable mid-take when the readout went live, and its
    wording had never had to survive that: "recording will not start", printed
    under a red Stop Recording button beside a state label reading Recording."""
    from wer.ui import record_panel as module
    from wer.video.recorder import DiskSpace

    monkeypatch.setattr(
        module, "check_disk", lambda _path: DiskSpace(5_000_000_000, 10 ** 12)
    )

    quiet_panel.attach(_FakeRecorder(recording=True))
    _pump(quiet_panel, lambda: "stop level" in quiet_panel._disk.text())
    assert "recording will not start" not in quiet_panel._disk.text()
    assert "this take is being stopped" in quiet_panel._disk.text()

    quiet_panel.attach(_FakeRecorder(recording=False))
    _pump(quiet_panel, lambda: "will not start" in quiet_panel._disk.text())
    assert "recording will not start" in quiet_panel._disk.text()


def test_the_low_disk_warning_comes_back_down_with_the_stop_level(quiet_panel) -> None:
    """It only ever ratcheted up, and no control anywhere could lower it. A
    spinbox emits on every keystroke, so typing 150 and correcting it to 15
    left the warning pinned at 300 GB in the show file, permanently, and every
    take then opened with a "Record anyway?" modal nobody was there to answer."""
    config = quiet_panel.config

    quiet_panel._stop_below.setValue(150)
    assert config.low_disk_warning_gb == 300.0

    quiet_panel._stop_below.setValue(15)
    assert config.low_disk_warning_gb == 30.0

    quiet_panel._stop_below.setValue(1)
    assert config.low_disk_warning_gb == 20.0, "never below the shipped default"


def test_a_hand_set_low_disk_warning_survives_a_stop_level_nudge(
    qt_app, monkeypatch
) -> None:
    """Curing the ratchet must not go so far as to overwrite a level somebody
    chose. No widget sets the warning, so hand-editing the show file is the
    only way to have one -- and the format invites exactly that. Recomputing it
    on every nudge threw it away silently, in the direction of less warning."""
    from wer.ui import record_panel as module

    monkeypatch.setattr(module, "detect_encoders", lambda **_kwargs: [])
    monkeypatch.setattr(module, "enumerate_audio_devices", lambda: [])
    config = RecordingConfig(low_disk_stop_gb=10.0, low_disk_warning_gb=120.0)
    panel = module.RecordPanel(config)
    panel.load_settings()

    panel._stop_below.setValue(11)

    assert config.low_disk_warning_gb == 120.0


# ------------------------------------------- an audio device that is not there


@pytest.fixture
def unplugged_audio_panel(qt_app, monkeypatch):
    """The booth case: the interface the show file names is not plugged in."""
    from wer.ui import record_panel as module
    from wer.video.devices import AudioDevice

    monkeypatch.setattr(module, "detect_encoders", lambda **_kwargs: [])
    monkeypatch.setattr(
        module,
        "enumerate_audio_devices",
        lambda: [AudioDevice(name="Microphone Array (Realtek(R) Audio)")],
    )
    monkeypatch.setattr(module, "probe_audio_formats", lambda _device: [])
    built = module.RecordPanel(
        RecordingConfig(audio_device="Focusrite Scarlett 2i2 USB (WDM)")
    )
    built.start_audio_inputs()
    _wait_for_audio_probe(built)
    return built


def test_an_absent_audio_device_is_not_displayed_as_none(unplugged_audio_panel) -> None:
    """findData returned -1, max(0, -1) selected row zero, and the box read
    "None - video only" while the recorder was still being told to use the
    missing interface -- and then refused to start, pointing at a setting the
    panel was already showing."""
    panel = unplugged_audio_panel
    assert panel._audio_box.currentData() == panel.config.audio_device
    assert "None" not in panel._audio_box.currentText()
    assert "Focusrite" in panel._audio_box.currentText()


def test_an_absent_audio_device_can_be_cleared(unplugged_audio_panel) -> None:
    """The dead end: "None" was already the current row, so choosing it fired
    no signal and changed nothing. On a booth PC whose only input was the
    unplugged interface there was no other row to go via, so the setting could
    not be cleared from the UI at all."""
    panel = unplugged_audio_panel
    panel._audio_box.setCurrentIndex(panel._audio_box.findData(None))
    assert panel.config.audio_device is None
    assert panel.output_settings().audio_device is None


def test_a_present_audio_device_is_still_selected(qt_app, monkeypatch) -> None:
    from wer.ui import record_panel as module
    from wer.video.devices import AudioDevice

    monkeypatch.setattr(module, "detect_encoders", lambda **_kwargs: [])
    monkeypatch.setattr(
        module, "enumerate_audio_devices", lambda: [AudioDevice(name="Scarlett 2i2")]
    )
    monkeypatch.setattr(module, "probe_audio_formats", lambda _device: [])
    panel = module.RecordPanel(RecordingConfig(audio_device="Scarlett 2i2"))
    panel.start_audio_inputs()
    _wait_for_audio_probe(panel)

    assert panel._audio_box.currentData() == "Scarlett 2i2"
    assert panel._audio_box.count() == 2, "no duplicate row for a device that is here"


def test_opening_another_show_file_leaves_no_dead_audio_rows(
    unplugged_audio_panel,
) -> None:
    """load_settings exists so a second show file can be opened -- its own
    docstring says so. Each absent interface used to leave its " -- not
    connected" row behind, so the list filled with interfaces nobody had."""
    panel = unplugged_audio_panel
    panel.load_settings()

    panel.config.audio_device = "Behringer UMC204HD (WDM)"
    panel.load_settings()

    rows = [panel._audio_box.itemText(i) for i in range(panel._audio_box.count())]
    assert sum("not connected" in row for row in rows) == 1, rows
    assert "Focusrite" not in " ".join(rows)
    assert panel._audio_box.currentData() == "Behringer UMC204HD (WDM)"


# ------------------------------------------ the audio input's sample format
#
# ffmpeg takes whichever format DirectShow lists first, which on both inputs
# measured is 44.1 kHz. A two-minute take through the Blackmagic's embedded
# HDMI audio came out at 44100 Hz while HDMI itself carries 48 kHz, so every
# take was resampled in the driver. The panel now asks each input what it
# offers, off the GUI thread, and requests 48 kHz only where it is listed.

BLACKMAGIC_AUDIO = "Line In (Blackmagic UltraStudio Recorder 3G Audio)"


def _audio_panel(monkeypatch, *, device, formats=(), config_device=None, probe=None):
    from wer.ui import record_panel as module
    from wer.video.devices import AudioDevice

    monkeypatch.setattr(module, "detect_encoders", lambda **_kwargs: [])
    monkeypatch.setattr(
        module, "enumerate_audio_devices", lambda: [AudioDevice(name=device)]
    )
    monkeypatch.setattr(
        module, "probe_audio_formats", probe or (lambda _device: list(formats))
    )
    built = module.RecordPanel(RecordingConfig(audio_device=config_device or device))
    built.start_audio_inputs()
    return built


def _wait_for_audio_probe(panel, seconds: float = 5.0) -> None:
    """Until the audio check has finished and its answer is back on this thread."""
    from PySide6.QtWidgets import QApplication

    panel._audio_probe.join(seconds)
    assert not panel._audio_probe.is_alive(), "the audio format probe never finished"
    _wait_until(QApplication.instance(), lambda: not panel.audio_inputs_pending, seconds)


def test_a_probed_48k_input_is_recorded_at_48k(qt_app, monkeypatch) -> None:
    from wer.video.devices import AudioFormat

    panel = _audio_panel(monkeypatch, device=BLACKMAGIC_AUDIO, formats=[
        AudioFormat(44100, 2, 16), AudioFormat(48000, 2, 16), AudioFormat(96000, 2, 16),
    ])
    _wait_for_audio_probe(panel)

    settings = panel.output_settings()
    assert (settings.audio_sample_rate, settings.audio_sample_bits,
            settings.audio_channels) == (48000, 16, 2)


def test_an_input_without_48k_is_left_on_its_own_format(qt_app, monkeypatch) -> None:
    from wer.video.devices import AudioFormat

    panel = _audio_panel(monkeypatch, device="Old USB Mic",
                         formats=[AudioFormat(44100, 1, 16)])
    _wait_for_audio_probe(panel)

    settings = panel.output_settings()
    assert settings.audio_sample_rate is None
    assert settings.audio_sample_bits is None
    assert settings.audio_channels is None


def test_until_the_probe_answers_nothing_is_forced(qt_app, monkeypatch) -> None:
    """Record can be pressed before the probe is back. A guess in that window
    risks an audio input that will not open; asking for nothing is exactly
    what happened before the probe existed."""
    import threading

    from wer.video.devices import AudioFormat

    release = threading.Event()

    def slow_probe(_device):
        release.wait(5.0)
        return [AudioFormat(48000, 2, 16)]

    panel = _audio_panel(monkeypatch, device=BLACKMAGIC_AUDIO, probe=slow_probe)
    try:
        assert panel.output_settings().audio_sample_rate is None
    finally:
        release.set()
        _wait_for_audio_probe(panel)
    assert panel.output_settings().audio_sample_rate == 48000


def test_probing_audio_formats_never_blocks_the_gui_thread(qt_app, monkeypatch) -> None:
    """One ffmpeg -list_options per device, about a second each on this
    machine. Done inline it would freeze the panel for as long as the list
    of inputs is long."""
    import threading
    import time

    release = threading.Event()

    def slow_probe(_device):
        release.wait(5.0)
        return []

    started = time.monotonic()
    panel = _audio_panel(monkeypatch, device=BLACKMAGIC_AUDIO, probe=slow_probe)
    took = time.monotonic() - started
    release.set()
    _wait_for_audio_probe(panel)
    assert took < 1.0, f"populating the audio list held the GUI thread for {took:.2f} s"


def test_an_unplugged_input_is_never_probed_or_forced(qt_app, monkeypatch) -> None:
    """The configured interface is not connected: there is nothing to ask,
    and nothing may be asked for on its behalf."""
    from wer.video.devices import AudioFormat

    asked = []

    def probe(device):
        asked.append(device.name)
        return [AudioFormat(48000, 2, 16)]

    panel = _audio_panel(
        monkeypatch, device="Microphone Array (Realtek(R) Audio)",
        config_device="Focusrite Scarlett 2i2 USB (WDM)", probe=probe,
    )
    _wait_for_audio_probe(panel)
    assert "Focusrite Scarlett 2i2 USB (WDM)" not in asked
    assert panel.output_settings().audio_sample_rate is None


# ------------------------------------------------- multi-channel audio inputs


def _panel_with_audio(qt_app, monkeypatch, offered):
    """A panel whose audio probe reports ``offered`` for one named input."""
    from wer.video.encoder import ALL_ENCODERS
    from wer.video.devices import AudioDevice
    from wer.ui import record_panel as module

    monkeypatch.setattr(
        module, "detect_encoders",
        lambda **_k: [EncoderAvailability(e, True, "", "") for e in ALL_ENCODERS],
    )
    monkeypatch.setattr(
        module, "enumerate_audio_devices", lambda: [AudioDevice(name="Scarlett 4i4")]
    )
    monkeypatch.setattr(module, "probe_audio_formats", lambda _d: list(offered))
    forget_detected_encoders()
    panel = module.RecordPanel(RecordingConfig())
    # Started explicitly, never from the constructor: on the Blackmagic rig the
    # audio probe would otherwise overlap the camera format probe questioning
    # the same unit. See RecordPanel.start_audio_inputs.
    panel.start_audio_inputs()
    _wait_until(qt_app, lambda: panel._audio_checked)
    return panel


def _audio_item(panel, name):
    index = panel._audio_box.findData(name)
    assert index >= 0, f"{name} is not in the Audio box"
    return panel._audio_box.itemText(index)


def test_a_four_channel_interface_says_it_is_recording_four(qt_app, monkeypatch) -> None:
    """The Scarlett case, and the reason this exists.

    An interface offering four channels is recorded with four. If only one
    pair is patched, two of them are silence -- and a take can be cut before
    anyone notices. The box has to say it before the take, not the log after.
    """
    from wer.video.devices import AudioFormat

    panel = _panel_with_audio(
        qt_app, monkeypatch, [AudioFormat(rate=48000, channels=4, bits=16)]
    )
    try:
        assert "4 ch" in _audio_item(panel, "Scarlett 4i4")
    finally:
        forget_detected_encoders()


def test_an_input_left_on_its_own_format_says_so(qt_app, monkeypatch) -> None:
    """No 48 kHz 16-bit on offer means Wer asks for nothing.

    An unqualified device name would imply a choice was made. None was.
    """
    from wer.video.devices import AudioFormat

    panel = _panel_with_audio(
        qt_app, monkeypatch, [AudioFormat(rate=44100, channels=2, bits=24)]
    )
    try:
        assert "default format" in _audio_item(panel, "Scarlett 4i4")
    finally:
        forget_detected_encoders()


def test_labelling_does_not_disturb_what_the_box_selects_on(qt_app, monkeypatch) -> None:
    """Only the text changes. The item's data is the device name, and the rest
    of the panel -- and the show file -- select on it."""
    from wer.video.devices import AudioFormat

    panel = _panel_with_audio(
        qt_app, monkeypatch, [AudioFormat(rate=48000, channels=2, bits=16)]
    )
    try:
        index = panel._audio_box.findData("Scarlett 4i4")
        assert panel._audio_box.itemData(index) == "Scarlett 4i4"
        assert "None — video only" in [
            panel._audio_box.itemText(i) for i in range(panel._audio_box.count())
        ], "the video-only row was relabelled"
    finally:
        forget_detected_encoders()


# ------------------------------------------- the microphone a first launch uses
#
# A fresh install on a Surface Pro 8 (12 Sep 2026) came up on "None -- video
# only", with the Surface's own microphone sitting in the list and nothing on
# the Recording tab to say a take would be silent. A first launch now starts on
# a microphone. Every later launch keeps what was saved, None included.

LAPTOP_MIC = "Microphone Array (Realtek(R) Audio)"
WEBCAM_MIC = "Microphone (USB Color Camera)"


def _first_launch_panel(
    monkeypatch, *, listed, windows_default, config_device=None, first_launch=True
):
    """A panel whose audio check lists ``listed``, on a machine whose Sound
    settings name ``windows_default`` as the recording device."""
    from types import SimpleNamespace

    from wer.ui import record_panel as module
    from wer.video.devices import AudioDevice

    rig = SimpleNamespace(changes=[], asked_windows=0, probed=[])

    def default_input():
        rig.asked_windows += 1
        return windows_default

    def probe(device):
        rig.probed.append(device.name)
        return []

    monkeypatch.setattr(module, "detect_encoders", lambda **_kwargs: [])
    monkeypatch.setattr(
        module, "enumerate_audio_devices", lambda: [AudioDevice(name=name) for name in listed]
    )
    monkeypatch.setattr(module, "probe_audio_formats", probe)
    monkeypatch.setattr(module, "default_audio_input_name", default_input)
    rig.panel = module.RecordPanel(RecordingConfig(audio_device=config_device))
    rig.panel.settings_changed.connect(lambda: rig.changes.append(True))
    rig.panel.start_audio_inputs(choose_an_input=first_launch)
    _wait_for_audio_probe(rig.panel)
    return rig


def test_a_first_launch_starts_on_the_microphone_windows_records_from(
    qt_app, monkeypatch
) -> None:
    """Windows' own default recording device, not merely the first listed: it
    is the one the machine's owner set in Sound settings."""
    rig = _first_launch_panel(
        monkeypatch, listed=[WEBCAM_MIC, LAPTOP_MIC], windows_default=LAPTOP_MIC
    )
    assert rig.panel.config.audio_device == LAPTOP_MIC
    assert rig.panel._audio_box.currentData() == LAPTOP_MIC
    assert rig.changes == [True], "the choice was not handed on to be saved"


@pytest.mark.parametrize(
    "windows_default",
    [None, "Headset Microphone (Bluetooth)"],
    ids=["Windows names none", "Windows names one that is not listed"],
)
def test_a_first_launch_otherwise_starts_on_the_first_input_listed(
    qt_app, monkeypatch, windows_default
) -> None:
    rig = _first_launch_panel(
        monkeypatch, listed=[WEBCAM_MIC, LAPTOP_MIC], windows_default=windows_default
    )
    assert rig.panel.config.audio_device == WEBCAM_MIC
    assert rig.panel._audio_box.currentData() == WEBCAM_MIC
    assert rig.changes == [True]


def test_the_microphone_a_first_launch_starts_on_is_asked_for_its_format_first(
    qt_app, monkeypatch
) -> None:
    """The input a take will ask for is probed first, so that if the start-up
    wait runs out partway through the check, its format is the one known."""
    rig = _first_launch_panel(
        monkeypatch, listed=[WEBCAM_MIC, LAPTOP_MIC], windows_default=LAPTOP_MIC
    )
    assert rig.probed == [LAPTOP_MIC, WEBCAM_MIC]


def test_a_later_launch_keeps_video_only_and_asks_windows_nothing(
    qt_app, monkeypatch
) -> None:
    """None is a real choice as well -- a booth recording the desk's sound
    separately -- and once saved it cannot be told apart from never having
    chosen. So only a first launch chooses."""
    rig = _first_launch_panel(
        monkeypatch, listed=[WEBCAM_MIC, LAPTOP_MIC], windows_default=LAPTOP_MIC,
        first_launch=False,
    )
    assert rig.panel.config.audio_device is None
    assert rig.panel._audio_box.currentData() is None
    assert rig.changes == [] and rig.asked_windows == 0


def test_a_saved_input_is_never_replaced_by_a_first_launch_choice(
    qt_app, monkeypatch
) -> None:
    """Not even one that is unplugged today; see
    test_an_absent_audio_device_is_not_displayed_as_none."""
    rig = _first_launch_panel(
        monkeypatch, listed=[LAPTOP_MIC], windows_default=LAPTOP_MIC,
        config_device="Focusrite Scarlett 2i2 USB (WDM)",
    )
    assert rig.panel.config.audio_device == "Focusrite Scarlett 2i2 USB (WDM)"
    assert rig.changes == [] and rig.asked_windows == 0


def test_a_first_launch_with_no_inputs_stays_video_only(qt_app, monkeypatch) -> None:
    rig = _first_launch_panel(monkeypatch, listed=[], windows_default=None)
    assert rig.panel.config.audio_device is None
    assert rig.panel._audio_box.currentData() is None
    assert rig.changes == []
