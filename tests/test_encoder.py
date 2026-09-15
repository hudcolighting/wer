"""Output settings and ffmpeg command construction.

Command building is a pure function, so these tests inspect arguments rather
than encoding video. No Qt in this process.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wer.video.encoder import (
    ALL_ENCODERS,
    QUALITY_PRESETS,
    SOUND_LEVEL_KEY,
    SOUND_STATS_FORMAT,
    Container,
    OutputSettings,
    build_ffmpeg_command,
    detect_encoders,
    raw_bitrate_mbps,
    sound_level_chain,
)

FAKE_FFMPEG = Path("C:/fake/ffmpeg.exe")


def build(settings: OutputSettings, **kwargs) -> list[str]:
    defaults = dict(
        capture_width=1920,
        capture_height=1080,
        capture_fps=30.0,
        output_path=Path("out.mkv"),
        ffmpeg=FAKE_FFMPEG,
    )
    defaults.update(kwargs)
    return build_ffmpeg_command(settings, **defaults)


def arg_after(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


# ------------------------------------------------------- capture/output decoupling


def test_output_defaults_to_the_capture_size() -> None:
    assert OutputSettings().output_size(3840, 2160) == (3840, 2160)


def test_output_size_can_be_smaller_than_capture() -> None:
    """Capture 4K, record 1080p - the main lever for making 4K workable."""
    settings = OutputSettings(width=1920, height=1080)
    assert settings.output_size(3840, 2160) == (1920, 1080)


def test_odd_dimensions_are_rounded_down_to_even() -> None:
    """yuv420p subsamples chroma by two; libx264 refuses odd sizes outright."""
    assert OutputSettings(width=1921, height=1081).output_size(1921, 1081) == (1920, 1080)


def test_scale_filter_is_added_only_when_scaling() -> None:
    scaled = build(OutputSettings(width=1920, height=1080),
                   capture_width=3840, capture_height=2160)
    assert "-vf" in scaled
    assert arg_after(scaled, "-vf") == "scale=1920:1080:flags=bicubic"

    unscaled = build(OutputSettings())
    assert "-vf" not in unscaled, "no filter should be added when sizes match"


def test_input_size_is_always_the_capture_size() -> None:
    """Frames arrive on the pipe at capture size; ffmpeg does the scaling."""
    command = build(OutputSettings(width=1280, height=720),
                    capture_width=1920, capture_height=1080)
    assert arg_after(command, "-s") == "1920x1080"


def test_raw_bitrate_shows_why_4k_is_different() -> None:
    assert round(raw_bitrate_mbps(1920, 1080, 30)) == 187
    assert round(raw_bitrate_mbps(3840, 2160, 30)) == 746


# ------------------------------------------------------------------------ quality


def test_quality_presets_span_a_useful_range() -> None:
    values = [
        OutputSettings(quality_preset=p.key).resolve_quality() for p in QUALITY_PRESETS
    ]
    assert values == sorted(values), "presets should go best-to-worst"
    assert values[0] < values[-1]


def test_explicit_quality_overrides_the_preset() -> None:
    settings = OutputSettings(quality=31, quality_preset="archive")
    assert settings.resolve_quality() == 31


def test_quality_is_clamped_to_the_encoder_range() -> None:
    """Quick Sync's global_quality starts at 1, not 0."""
    settings = OutputSettings(encoder="h264_qsv", quality_preset="archive")
    encoder = settings.resolve_encoder()
    assert encoder.quality_min <= settings.resolve_quality() <= encoder.quality_max


@pytest.mark.parametrize(
    ("encoder", "expected_flag"),
    [
        ("libx264", "-crf"),
        ("h264_nvenc", "-cq"),
        ("h264_qsv", "-global_quality"),
    ],
)
def test_each_encoder_gets_its_own_quality_flag(encoder: str, expected_flag: str) -> None:
    """Passing -crf to nvenc is silently ignored and yields the wrong bitrate."""
    command = build(OutputSettings(encoder=encoder))
    assert expected_flag in command
    if encoder != "libx264":
        assert "-crf" not in command


def test_amf_uses_per_frame_type_qp() -> None:
    """AMF has no single quality knob."""
    command = build(OutputSettings(encoder="h264_amf"))
    assert "-qp_i" in command and "-qp_p" in command and "-rc" in command


def test_unknown_encoder_falls_back_to_software_rather_than_failing() -> None:
    assert OutputSettings(encoder="h264_wishful").resolve_encoder().name == "libx264"


def test_every_encoder_default_preset_is_valid_for_it() -> None:
    """Catches e.g. giving nvenc a default preset from the deprecated name set.

    An encoder with no speed presets at all is allowed -- Media Foundation has
    no such option, and offering one would mean passing ffmpeg a flag it
    rejects outright. What is not allowed is having one without the other.
    """
    for encoder in ALL_ENCODERS:
        if not encoder.presets:
            assert not encoder.preset_default, (
                f"{encoder.name} has a default preset {encoder.preset_default!r} "
                f"but no presets to choose it from"
            )
            continue
        assert encoder.preset_default in encoder.presets, (
            f"{encoder.name} default preset {encoder.preset_default!r} "
            f"is not in {encoder.presets}"
        )


# -------------------------------------------------------------------- containers


def test_mkv_is_the_default_container() -> None:
    """A power loss four hours into a tech must not destroy the file."""
    assert OutputSettings().container is Container.MKV
    assert "-movflags" not in build(OutputSettings())


def test_fragmented_mp4_flags_are_added_for_mp4() -> None:
    command = build(OutputSettings(container=Container.MP4),
                    output_path=Path("out.mp4"))
    assert arg_after(command, "-movflags") == "+frag_keyframe+empty_moov+delay_moov"


def test_unfragmented_mp4_can_be_requested() -> None:
    command = build(OutputSettings(container=Container.MP4, fragmented_mp4=False),
                    output_path=Path("out.mp4"))
    assert "-movflags" not in command


# ------------------------------------------------------------------------- audio


def _inputs(command: list[str]) -> list[str]:
    """What each -i names, in the order ffmpeg opens them."""
    return [command[i + 1] for i, token in enumerate(command) if token == "-i"]


def _maps(command: list[str]) -> list[str]:
    """What each -map names, in the order the output streams are made."""
    return [command[i + 1] for i, token in enumerate(command) if token == "-map"]


def test_audio_is_omitted_entirely_when_no_device_is_chosen() -> None:
    command = build(OutputSettings())
    assert "dshow" not in command
    assert "-c:a" not in command
    assert _inputs(command) == ["pipe:0"]
    assert _maps(command) == ["0:v"]


def test_audio_device_is_passed_verbatim() -> None:
    """Device names contain spaces, brackets and parentheses. Do not prettify."""
    name = "Microphone Array (Realtek(R) Audio)"
    command = build(OutputSettings(audio_device=name))
    assert f"audio={name}" in command
    assert _maps(command) == ["1:v", "0:a"]


def test_the_audio_input_is_opened_before_the_picture_pipe() -> None:
    """ffmpeg opens its inputs in command-line order and zeroes each on its own
    first timestamp. With the pipe first, its first frame was read at launch and
    the audio input opened only after it, so every take's sound was early by
    however long that took: measured 280-790 ms, different on every take, with
    the first 8-24 frames frozen. Audio first, the sound was 58-67 ms late,
    take after take.

    The picture stays the first output stream: that follows the -map order."""
    command = build(OutputSettings(audio_device="Mic"))
    assert _inputs(command) == ["audio=Mic", "pipe:0"]
    assert _maps(command) == ["1:v", "0:a"]


# -------------------------------------------------------------------- A/V offset


def test_positive_av_offset_delays_audio() -> None:
    command = build(OutputSettings(audio_device="Mic", av_offset_ms=40))
    offset_index = command.index("-itsoffset")
    audio_index = command.index("-f", offset_index)
    assert command[offset_index + 1] == "0.040"
    assert command[audio_index + 1] == "dshow", "offset should attach to the audio input"


def test_negative_av_offset_delays_video() -> None:
    command = build(OutputSettings(audio_device="Mic", av_offset_ms=-40))
    offset_index = command.index("-itsoffset")
    assert command[offset_index + 1] == "0.040"
    # The video input follows immediately; it is the rawvideo one.
    assert "rawvideo" in command[offset_index : offset_index + 4]


def test_zero_offset_adds_no_flag() -> None:
    assert "-itsoffset" not in build(OutputSettings(audio_device="Mic"))


@pytest.mark.parametrize("offset_ms, delayed", [(40, "audio=Mic"), (-40, "pipe:0")])
def test_each_offset_stays_with_the_input_it_delays(offset_ms: int, delayed: str) -> None:
    """-itsoffset belongs to the next -i. Moved without the block it heads, a
    positive offset would sit in front of the pipe and delay the picture instead
    of the sound -- in the order the inputs are now opened, as in the old one."""
    command = build(OutputSettings(audio_device="Mic", av_offset_ms=offset_ms))
    assert _inputs(command) == ["audio=Mic", "pipe:0"]
    assert command.count("-itsoffset") == 1
    offset = command.index("-itsoffset")
    assert command[offset + 1] == "0.040"
    assert command[command.index("-i", offset) + 1] == delayed


@pytest.mark.parametrize("offset_ms", [0, 40, -40])
@pytest.mark.parametrize("audio_device", [None, "Mic"])
def test_the_pipe_is_timed_to_the_millisecond_without_a_forced_rate(
    audio_device: str | None, offset_ms: int
) -> None:
    """-framerate 1000 is the rawvideo demuxer's time base, so each arrival is
    rounded to the nearest millisecond rather than 40, and -fpsprobesize 0 keeps
    its probe to one frame rather than up to 41. Neither is -r, which on an
    input stamps frames at a fixed rate whatever their arrival and made a
    16.8 fps take play 1.79x too fast: no -r may appear among the inputs, with
    sound or without, whichever way the offset goes."""
    command = build(OutputSettings(audio_device=audio_device, av_offset_ms=offset_ms))
    video_input = command[command.index("rawvideo") : command.index("pipe:0")]
    assert video_input[video_input.index("-framerate") + 1] == "1000"
    assert video_input[video_input.index("-fpsprobesize") + 1] == "0"
    last_input = len(command) - 1 - command[::-1].index("-i")
    assert "-r" not in command[: last_input + 1], "an input -r forces the rate"
    assert command.count("-r") == 1, "the nominal rate belongs on the output, once"


# ------------------------------------------------------------------------- misc


def test_keyframe_interval_defaults_to_two_seconds() -> None:
    assert arg_after(build(OutputSettings()), "-g") == "60"
    assert arg_after(build(OutputSettings(), capture_fps=25.0), "-g") == "50"


def test_the_output_pixel_format_suits_the_encoder() -> None:
    """Software takes yuv420p, which plays everywhere. Quick Sync wants nv12
    and silently converts otherwise, doing a needless pass per frame -- and
    the H.264 it emits is 4:2:0 either way, so nothing downstream can tell.
    Confirmed on a real Quick Sync recording: ffmpeg reports the stream as
    yuv420p regardless."""
    software = build(OutputSettings(encoder="libx264"))
    assert software.count("-pix_fmt") == 2   # once for input, once for output
    assert software[software.index("pipe:0"):].count("yuv420p") == 1

    quick_sync = build(OutputSettings(encoder="h264_qsv"))
    assert quick_sync[quick_sync.index("pipe:0"):].count("nv12") == 1


def test_missing_ffmpeg_raises_something_explicable(monkeypatch) -> None:
    """`ffmpeg=None` means "find it yourself", so the not-found path needs the
    lookup itself stubbed out. Worth testing: on a broken bundle this is the
    error the user sees, and it should say what to do."""
    monkeypatch.setattr("wer.video.encoder.ffmpeg_path", lambda: None)
    with pytest.raises(RuntimeError, match="ffmpeg was not found"):
        build_ffmpeg_command(
            OutputSettings(), capture_width=1920, capture_height=1080,
            capture_fps=30.0, output_path=Path("x.mkv"), ffmpeg=None,
        )


# --------------------------------------------------------------- real detection


def test_detection_always_offers_software_encoding() -> None:
    """A strange laptop may have no usable hardware encoder at all."""
    results = detect_encoders(test_encode=False)
    software = [r for r in results if not r.encoder.is_hardware]
    assert software and all(r.available for r in software)


def test_unavailable_encoders_explain_themselves() -> None:
    """An encoder that silently vanishes from a dropdown is a bug."""
    for result in detect_encoders():
        if not result.available:
            assert result.reason, f"{result.encoder.name} gave no reason"
            assert "unavailable" in str(result)


def test_shortest_is_set_when_there_is_a_live_audio_input() -> None:
    """Without it ffmpeg never exits, and Stop hangs for the whole timeout.

    ffmpeg finishes when all inputs are exhausted. A dshow microphone is a live
    capture device and is never exhausted, so closing the video pipe at the end
    of a take left ffmpeg waiting on audio forever -- a 30-second frozen window.
    """
    assert "-shortest" in build(OutputSettings(audio_device="Mic"))


def test_shortest_is_absent_without_audio() -> None:
    """Video-only already ends when the pipe closes; -shortest would be noise."""
    assert "-shortest" not in build(OutputSettings())


def test_the_sound_statistics_are_asked_for_only_when_there_is_sound() -> None:
    """How the recorder tells an input delivering only part of its sound.

    A USB webcam's microphone delivered half of it, in gaps of about 50 ms in
    every 100, and nothing ffmpeg printed said so. These lines carry the
    samples reaching the audio encoder and the timestamps they arrive under,
    which between them do.
    """
    command = build(OutputSettings(audio_device="Mic"))

    assert command[command.index("-stats_enc_pre:a") + 1] == "pipe:1"
    assert command[command.index("-stats_enc_pre_fmt:a") + 1] == SOUND_STATS_FORMAT
    # After the last input, not merely before the output path. Moved up among
    # the input options the flags are still before "-y", and ffmpeg 8.1.2 then
    # refuses to start at all for any take with sound: "Option stats_enc_pre:a
    # ... cannot be applied to input url".
    last_input = len(command) - 1 - command[::-1].index("-i")
    assert command.index("-stats_enc_pre:a") > last_input, (
        "an output option, and ffmpeg will not start with it among the inputs"
    )
    assert "-stats_enc_pre:a" not in build(OutputSettings())


def test_the_peak_level_is_asked_for_only_when_there_is_sound() -> None:
    """How the recorder tells digital silence from a quiet house.

    The delivery statistics above cannot: an input handing over exact zeros
    delivers every sample its timestamps promise and reads a perfect 100%.
    The peak level says so, on the same pipe, once a second.
    """
    command = build(OutputSettings(audio_device="Mic"))
    chain = command[command.index("-filter:a") + 1]

    assert chain.startswith("asetnsamples=n=48000,"), chain
    assert "astats=metadata=1:reset=1" in chain, chain
    assert f"ametadata=mode=print:key={SOUND_LEVEL_KEY}" in chain, chain
    # Onto the pipe the delivery statistics already use, and written straight
    # out rather than held until a buffer fills.
    assert "file=pipe\\\\:1" in chain, chain
    assert "direct=1" in chain, chain
    assert "-filter:a" not in build(OutputSettings())


def test_only_one_audio_filter_option_is_ever_passed() -> None:
    """ffmpeg keeps the last -af/-filter:a for a stream and drops the rest,
    saying so in one line among a hundred. Anything else Wer ever filters the
    sound with has to join this chain, not arrive beside it."""
    command = build(OutputSettings(audio_device="Mic"))

    assert command.count("-filter:a") == 1
    assert "-af" not in command


def test_an_input_opened_at_its_own_rate_is_measured_a_second_at_a_time() -> None:
    """The frames astats measures are set in samples, so the rate the input
    was opened at is what makes one of them a second. Unset, DirectShow picks
    its own -- 44.1 kHz on the inputs measured -- and 48000 samples then cover
    1.09 seconds, which is near enough because the recorder judges the stretch
    the reports' own timestamps cover rather than counting them."""
    assert sound_level_chain(44100).startswith("asetnsamples=n=44100,")
    assert sound_level_chain(None).startswith("asetnsamples=n=48000,")


def test_shortest_does_not_hold_the_sound_back_long_enough_to_lose_it() -> None:
    """-shortest's default ten-second hold backed the sound up into DirectShow
    until it dropped packets: an hour on libx264 lost 238 s of sound. At one
    second a minute on the same input lost none."""
    command = build(OutputSettings(audio_device="Mic"))
    assert command[command.index("-shortest_buf_duration") + 1] == "1"
    assert "-shortest_buf_duration" not in build(OutputSettings())


def test_the_audio_input_has_room_to_ride_out_a_stall() -> None:
    """DirectShow drops sound from 62% of its buffer, and the default buffer
    is about ten seconds of 48 kHz stereo. The larger buffer is an input
    option, so it has to sit between -f dshow and -i."""
    options = _dshow_audio_input_options(build(OutputSettings(audio_device="Mic")))
    size = options[options.index("-rtbufsize") + 1]
    assert size.endswith("M") and int(size[:-1]) >= 64, size


def test_quick_sync_gets_the_pixel_format_it_wants() -> None:
    """h264_qsv cannot take yuv420p; it warns and converts every frame."""
    command = build(OutputSettings(encoder="h264_qsv"))
    assert command[command.index("-c:v") : ][1] == "h264_qsv"
    output_pix_fmt = command[command.index("-pix_fmt", command.index("-c:v")) + 1]
    assert output_pix_fmt == "nv12"


def test_other_encoders_still_get_yuv420p() -> None:
    """Anything else plays everywhere as yuv420p."""
    for encoder in ("libx264", "h264_nvenc", "h264_amf"):
        command = build(OutputSettings(encoder=encoder))
        assert command[command.index("-pix_fmt", command.index("-c:v")) + 1] == "yuv420p"


# ------------------------------------------------ the file is the right length


def test_the_nominal_rate_is_not_on_the_input() -> None:
    """An input -r stamps PTS at that rate regardless of when frames arrive,
    which silently defeats -use_wallclock_as_timestamps. Measured: 15 fps fed
    into a pipeline told 30 produced a file exactly 2.000x too fast."""
    command = build(OutputSettings())
    pipe = command.index("pipe:0")
    before_input = command[:pipe]
    assert "-use_wallclock_as_timestamps" in before_input
    assert "-r" not in before_input, (
        "an input -r overrides the wallclock timestamps and makes the file "
        "the wrong length whenever the camera runs off its nominal rate"
    )


def test_the_output_is_constant_rate_at_the_nominal_fps() -> None:
    """Where the nominal rate belongs: ffmpeg fills gaps and drops excess so
    the file is always as long as the take really was."""
    command = build(OutputSettings())
    after_input = command[command.index("pipe:0"):]
    assert "-fps_mode" in after_input
    assert after_input[after_input.index("-fps_mode") + 1] == "cfr"
    assert after_input[after_input.index("-r") + 1] == "30"


def test_a_slow_camera_still_produces_a_correctly_timed_file(tmp_path) -> None:
    """The end-to-end guard, and the one that actually caught this.

    Feeds 15 fps into a pipeline told 30 for eight seconds and checks the file
    lasts eight seconds. Before the fix this produced four.
    """
    import re
    import subprocess
    import time

    import numpy as np

    from wer.paths import ffmpeg_path

    exe = ffmpeg_path()
    if exe is None:
        pytest.skip("no ffmpeg")

    width, height, real_fps, seconds = 320, 180, 15.0, 8.0
    output = tmp_path / "slow.mkv"
    command = build(
        OutputSettings(encoder="libx264", speed_preset="ultrafast"),
        capture_width=width, capture_height=height, capture_fps=30.0,
        output_path=output, ffmpeg=exe,
    )
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    frame = np.zeros((height, width, 3), dtype=np.uint8).tobytes()
    started = time.perf_counter()
    due = started
    while time.perf_counter() - started < seconds:
        due += 1.0 / real_fps
        try:
            process.stdin.write(frame)
        except OSError:
            break
        remaining = due - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)
    wall = time.perf_counter() - started
    process.stdin.close()
    process.wait(timeout=60)

    probe = subprocess.run([str(exe), "-i", str(output)],
                           capture_output=True, text=True).stderr
    match = re.search(r"Duration: (\d+):(\d+):([\d.]+)", probe)
    assert match, f"no duration in:\n{probe[-800:]}"
    hours, minutes, secs = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(secs)

    assert abs(duration - wall) < 1.0, (
        f"recorded {wall:.1f}s of wall clock but the file is {duration:.1f}s "
        f"({wall / duration:.3f}x off). Chapters and markers would all be wrong."
    )


# --------------------------------- hardware encoding by default when present


def test_auto_prefers_hardware_when_the_machine_has_it() -> None:
    """Hardware is preferred for CPU headroom across a four-hour tech.

    It is worth recording what this docstring used to claim, because it was
    wrong. An hour of 1080p30 on x264 spent 32.5% of its seconds under 29 fps
    against Quick Sync's 11.9%, and the conclusion drawn was that software
    encoding starves the capture thread. Reading the same camera with nothing
    encoding and Wer not running at all still spends 20% of its seconds under
    29: the dips are the webcam's auto-exposure hunting, not the encoder.
    """
    from wer.video.encoder import (
        AUTO_ENCODER, SOFTWARE_ENCODER, VideoEncoder, resolve_encoder_name,
    )

    quick_sync = VideoEncoder(
        "h264_qsv", "Intel Quick Sync", "-global_quality", 23, 1, 51,
        ("medium",), "medium", True,
    )
    assert resolve_encoder_name(AUTO_ENCODER, [SOFTWARE_ENCODER, quick_sync]) == "h264_qsv"


def test_auto_falls_back_to_software_on_a_machine_with_none() -> None:
    """A strange laptop with nothing installed on it has to work."""
    from wer.video.encoder import AUTO_ENCODER, SOFTWARE_ENCODER, resolve_encoder_name

    assert resolve_encoder_name(AUTO_ENCODER, [SOFTWARE_ENCODER]) == "libx264"
    assert resolve_encoder_name(AUTO_ENCODER, []) == "libx264"


def test_an_explicit_choice_is_never_overridden() -> None:
    """Someone who picked software gets software, even beside a working GPU.
    Quietly recording with something they did not choose would be worse than
    a slower encode."""
    from wer.video.encoder import SOFTWARE_ENCODER, VideoEncoder, resolve_encoder_name

    quick_sync = VideoEncoder(
        "h264_qsv", "Intel Quick Sync", "-global_quality", 23, 1, 51,
        ("medium",), "medium", True,
    )
    assert resolve_encoder_name("libx264", [SOFTWARE_ENCODER, quick_sync]) == "libx264"


def test_the_default_settings_resolve_to_a_real_encoder() -> None:
    from wer.video.encoder import ALL_ENCODERS, OutputSettings

    assert OutputSettings().encoder == "auto"
    assert OutputSettings().resolve_encoder() in ALL_ENCODERS


# ------------------------- Media Foundation: the GPU path that always works


@pytest.fixture
def clean_detection():
    """Auto-selection reads process-wide detection state. Reset it either side."""
    from wer.video.encoder import forget_detected_encoders

    forget_detected_encoders()
    yield
    forget_detected_encoders()


def _availability(name: str, *, hardware: str = "", available: bool = True):
    from wer.video.encoder import ALL_ENCODERS as encoders, EncoderAvailability

    encoder = next(e for e in encoders if e.name == name)
    return EncoderAvailability(
        encoder, available, "" if available else "not on this machine", hardware
    )


def test_media_foundation_is_never_given_a_preset() -> None:
    """There is no -preset option on h264_mf, and passing one is a hard error.

    Not a silently ignored flag: ffmpeg exits before writing a frame. This is
    the whole reason the encoder carries no presets to offer.
    """
    command = build(OutputSettings(encoder="h264_mf"))
    assert "-preset" not in command


def test_media_foundation_is_made_to_use_real_hardware() -> None:
    """Left to itself it will fall back to a software MFT and report success,
    which would mean calling the CPU a GPU in the settings dialog."""
    assert arg_after(build(OutputSettings(encoder="h264_mf")), "-hw_encoding") == "1"


def test_media_foundation_is_pinned_to_high_profile() -> None:
    """Its silent default is Constrained Baseline -- no CABAC, no B-frames,
    and a bitrate to match. The profile must be set by number: the names
    ffmpeg documents fail to parse here. 100 is High."""
    command = build(OutputSettings(encoder="h264_mf"))
    assert arg_after(command, "-profile:v") == "100"


def test_every_encoder_is_pinned_to_high_profile() -> None:
    """Left alone the encoders disagree, and invisibly.

    Measured on this machine with ffmpeg 8.1.2: x264 and Quick Sync default to
    High, NVENC defaults to Main, Media Foundation to Constrained Baseline. So
    "Archive" meant a different thing depending on which machine was in the
    booth, and the weakest result went to the one with the most hardware in it.

        h264_nvenc, no profile flag  -> h264 (Main)
        h264_nvenc -profile:v high   -> h264 (High)
    """
    for encoder in ALL_ENCODERS:
        command = build(OutputSettings(encoder=encoder.name))
        assert "-profile:v" in command, f"{encoder.name} was left on its default"
        wanted = "100" if encoder.name == "h264_mf" else "high"
        assert arg_after(command, "-profile:v") == wanted, (
            f"{encoder.name} asked for the wrong profile"
        )


def test_media_foundation_asks_for_quality_not_a_bitrate() -> None:
    """-quality alone is ignored unless the rate control mode is set too."""
    command = build(OutputSettings(encoder="h264_mf"))
    assert arg_after(command, "-rate_control") == "quality"
    assert "-quality" in command


def test_media_foundation_quality_counts_the_other_way_up() -> None:
    """0-100, higher is better -- the opposite of CRF and QP. A preset asking
    for better must not come out as a bigger number here, or Archive would
    quietly be the worst setting available."""
    settings = OutputSettings(encoder="h264_mf")
    archive = OutputSettings(encoder="h264_mf", quality_preset="archive")
    tiny = OutputSettings(encoder="h264_mf", quality_preset="tiny")

    assert archive.resolve_quality() > settings.resolve_quality() > tiny.resolve_quality()
    encoder = settings.resolve_encoder()
    for candidate in (archive, settings, tiny):
        assert encoder.quality_min <= candidate.resolve_quality() <= encoder.quality_max


def test_media_foundation_gets_the_pixel_format_it_wants() -> None:
    command = build(OutputSettings(encoder="h264_mf"))
    assert command[command.index("-pix_fmt", command.index("-c:v")) + 1] == "nv12"


def test_auto_reaches_a_gpu_whose_own_encoder_will_not_open(clean_detection) -> None:
    """The case this encoder exists for.

    h264_nvenc has a compiled-in NVENC API version the driver must be new
    enough to serve; ours wants 13.1 and a working RTX 5070 Ti on driver
    592.01 offers 13.0, so nvenc refuses. Without Media Foundation the machine
    with the most encoding silicon in it falls back to the Intel iGPU.
    """
    from wer.video.encoder import (
        AUTO_ENCODER, remember_detected_encoders, resolve_encoder_name,
    )

    remember_detected_encoders([
        _availability("libx264"),
        _availability("h264_nvenc", available=False),
        _availability("h264_qsv"),
        _availability("h264_mf", hardware="NVIDIA"),
    ])
    assert resolve_encoder_name(AUTO_ENCODER) == "h264_mf"


def test_auto_prefers_a_vendors_own_encoder_over_the_broker(clean_detection) -> None:
    """Same silicon either way, but the native encoder has real rate control
    where Media Foundation has a 0-100 dial. Reach the chip the better way."""
    from wer.video.encoder import (
        AUTO_ENCODER, remember_detected_encoders, resolve_encoder_name,
    )

    remember_detected_encoders([
        _availability("libx264"),
        _availability("h264_qsv"),
        _availability("h264_mf", hardware="Intel"),
    ])
    assert resolve_encoder_name(AUTO_ENCODER) == "h264_qsv"


def test_media_foundation_says_whose_gpu_it_found() -> None:
    """"GPU (Media Foundation)" does not tell anyone which GPU. It is a broker
    for someone else's encoder, and on a laptop with two of them the answer is
    the point."""
    assert "NVIDIA" in str(_availability("h264_mf", hardware="NVIDIA"))


# ---------------------- detection answers for the take, not for writing nothing


#: What detection is pointed at. Nothing runs it: every test below puts a
#: stand-in in place of subprocess.run first.
_STAND_IN_FFMPEG = Path("C:/stand-in/vendor/ffmpeg/ffmpeg.exe")

#: The bundled 8.1.2's own ``-encoders`` lines for the encoders Wer knows.
_ENCODER_LINES = {
    "libx264": " V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC "
               "/ MPEG-4 part 10 (codec h264)",
    "h264_amf": " V....D h264_amf             AMD AMF H.264 Encoder (codec h264)",
    "h264_mf": " V....D h264_mf              H264 via MediaFoundation (codec h264)",
    "h264_nvenc": " V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)",
    "h264_qsv": " V..... h264_qsv             H.264 / AVC / MPEG-4 AVC / MPEG-4 "
                "part 10 (Intel Quick Sync Video acceleration) (codec h264)",
}

# Media Foundation's output in the shape -loglevel level+debug gives it. The
# words are ffmpeg 8.1.2's own; the addresses, and the severities of lines
# judged only by their words, are illustrative.
_MFT_NAMED = "[h264_mf @ 000001d2c1a0e440] [info] MFT name: '{}'\n"
_NO_STREAM_HEADER = (
    "[h264_mf @ 000001d2c1a0e440] [verbose] Awaiting extradata\n"
    "[h264_mf @ 000001d2c1a0e440] [verbose] Didn't get extradata in 70 ms\n"
    "[out#0/matroska @ 000001d2c0f1b2c0] [error] Could not write header "
    "(incorrect codec parameters ?): Invalid data found when processing input\n"
    "[out#0/matroska @ 000001d2c0f1b2c0] [error] Nothing was written into "
    "output file, because at least one of its streams received no packets.\n"
)
_NO_HARDWARE_MFT = (
    "[h264_mf @ 000001d2c1a0e440] [error] could not find any MFT for the "
    "given media type\n"
    "[h264_mf @ 000001d2c1a0e440] [error] could not create MFT\n"
    "[vost#0:0/h264_mf @ 000001d2c0f1a100] [error] Error while opening encoder "
    "- maybe incorrect parameters such as bit_rate, rate, width or height.\n"
)


def _output_format(command: list[str]) -> str:
    """The muxer a probe writes with: the value of its last -f."""
    return command[len(command) - command[::-1].index("-f")]


def _stand_in_ffmpeg(monkeypatch, probes: dict) -> list[list[str]]:
    """Put a stand-in ffmpeg under detection, playing the machine in ``probes``.

    ``probes`` maps each hardware encoder this build has to what its probe
    does: a function of the command, returning (exit status, stderr). Returns
    the list every command run is appended to.
    """
    import subprocess

    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(list(command))
        if "-encoders" in command:
            listing = "\n".join(_ENCODER_LINES[name] for name in ("libx264", *probes))
            return subprocess.CompletedProcess(command, 0, listing, "")
        status, stderr = probes[arg_after(command, "-c:v")](command)
        return subprocess.CompletedProcess(command, status, "", stderr)

    monkeypatch.setattr("wer.video.encoder.ffmpeg_path", lambda: _STAND_IN_FFMPEG)
    monkeypatch.setattr(subprocess, "run", run)
    return commands


def _probe_of(commands: list[list[str]], name: str) -> list[str]:
    return next(c for c in commands if "-c:v" in c and arg_after(c, "-c:v") == name)


def _detected(name: str):
    return next(r for r in detect_encoders() if r.encoder.name == name)


def _intel_mft(command: list[str]) -> tuple[int, str]:
    """Media Foundation on the development laptop, handed Intel's Quick Sync MFT.

    Encoding to nothing works. Recording Matroska fails before the first frame,
    because the MFT has no stream header to give until it has encoded one.
    """
    named = _MFT_NAMED.format("Intel Quick Sync Video H.264 Encoder MFT")
    if _output_format(command) == "null":
        return 0, named
    return 1, named + _NO_STREAM_HEADER


def _nvidia_mft(command: list[str]) -> tuple[int, str]:
    """Media Foundation handed an encoder that records into anything."""
    if _output_format(command) == "matroska":
        Path(command[-1]).write_bytes(b"\x1a\x45\xdf\xa3")  # how an MKV starts
    return 0, _MFT_NAMED.format("NVIDIA H.264 Encoder MFT")


def test_the_media_foundation_probe_records_a_real_mkv_not_nothing(monkeypatch) -> None:
    """A probe into -f null passed Intel's MFT, which cannot start one MKV take:
    the null muxer never needs a stream header. The probe writes what a take
    writes by default, in the take's pixel format and hardware mode."""
    commands = _stand_in_ffmpeg(monkeypatch, {"h264_mf": _nvidia_mft})
    detect_encoders()
    probe = _probe_of(commands, "h264_mf")
    assert _output_format(probe) == "matroska"
    assert Path(probe[-1]).suffix == f".{OutputSettings().container.value}"
    assert arg_after(probe, "-pix_fmt") == "nv12"
    assert arg_after(probe, "-hw_encoding") == "1"


@pytest.mark.parametrize("name", ["h264_nvenc", "h264_qsv", "h264_amf"])
def test_the_other_hardware_probes_record_a_real_mkv_too(monkeypatch, name) -> None:
    """Any encoder with no stream header before its first frame breaks an MKV
    take the same way. Measured with rawvideo standing in for the encoder, the
    file and its folder add about 4 ms to a probe."""
    commands = _stand_in_ffmpeg(monkeypatch, {name: lambda _command: (0, "")})
    detect_encoders()
    assert _output_format(_probe_of(commands, name)) == "matroska"


def test_the_test_recording_leaves_nothing_behind(monkeypatch) -> None:
    """It runs at every launch, so a folder left per launch would pile up in
    the temporary folder for as long as the booth machine lasts."""
    commands = _stand_in_ffmpeg(monkeypatch, {"h264_mf": _nvidia_mft})
    detect_encoders()
    written = Path(_probe_of(commands, "h264_mf")[-1])
    assert written.suffix == ".mkv"
    assert not written.parent.exists()


def test_an_encoder_that_cannot_start_an_mkv_is_unavailable_and_says_why(monkeypatch) -> None:
    """The development laptop, where detection said "works on Intel" and every
    MKV take on it failed within a second. The reason names the MKV, the cause
    and the chip -- not "no hardware encoder", which sends someone hunting a
    driver problem that is not there."""
    _stand_in_ffmpeg(monkeypatch, {"h264_mf": _intel_mft})
    result = _detected("h264_mf")
    assert not result.available
    assert "cannot start an MKV recording" in result.reason
    assert "stream header" in result.reason
    assert "Intel" in result.reason and result.hardware == "Intel"
    assert "unavailable" in str(result)


def test_media_foundation_with_no_hardware_encoder_still_says_so(monkeypatch) -> None:
    """The one failure the old wording was right about keeps it."""
    _stand_in_ffmpeg(monkeypatch, {"h264_mf": lambda _command: (1, _NO_HARDWARE_MFT)})
    result = _detected("h264_mf")
    assert not result.available
    assert result.reason == "Windows offers no hardware H.264 encoder here"


def test_an_encoder_windows_could_not_start_is_not_called_missing(monkeypatch) -> None:
    """ffmpeg said "could not create MFT" and not "could not find any MFT", so
    the reason must not claim there is no encoder."""
    stderr = "[h264_mf @ 000001d2c1a0e440] [error] could not create MFT\n"
    _stand_in_ffmpeg(monkeypatch, {"h264_mf": lambda _command: (1, stderr)})
    result = _detected("h264_mf")
    assert not result.available
    assert "would not start" in result.reason
    assert "no hardware" not in result.reason


def test_a_media_foundation_recording_that_works_still_names_the_chip(monkeypatch) -> None:
    """On a laptop with two GPUs, which one it reached is the point."""
    _stand_in_ffmpeg(monkeypatch, {"h264_mf": _nvidia_mft})
    result = _detected("h264_mf")
    assert result.available and result.hardware == "NVIDIA"
    assert "NVIDIA" in str(result)


def test_automatic_does_not_choose_a_media_foundation_that_cannot_start_an_mkv(
    monkeypatch, clean_detection,
) -> None:
    """With no native encoder for the chip, "auto" took Media Foundation at its
    word that it worked on Intel, and every take failed. Software records."""
    from wer.video.encoder import (
        AUTO_ENCODER, remember_detected_encoders, resolve_encoder_name,
    )

    _stand_in_ffmpeg(monkeypatch, {"h264_mf": _intel_mft})
    remember_detected_encoders(detect_encoders())
    assert resolve_encoder_name(AUTO_ENCODER) == "libx264"


def test_a_native_encoder_that_cannot_start_an_mkv_says_so_too(monkeypatch) -> None:
    """ffmpeg's own line names neither the container nor what could not start."""
    stderr = (
        "[out#0/matroska @ 000001d2c0f1b2c0] Could not write header "
        "(incorrect codec parameters ?): Invalid data found when processing input\n"
    )
    _stand_in_ffmpeg(monkeypatch, {"h264_amf": lambda _command: (1, stderr)})
    result = _detected("h264_amf")
    assert not result.available
    assert "cannot start an MKV recording" in result.reason


def test_no_temporary_folder_leaves_the_encoder_unproven_not_working(monkeypatch) -> None:
    """Falling back to recording nothing would bring back the probe that passed
    an encoder no take could use."""
    import tempfile

    def no_folder(*_args, **_kwargs):
        raise PermissionError("Access is denied")

    _stand_in_ffmpeg(monkeypatch, {"h264_mf": _nvidia_mft})
    monkeypatch.setattr(tempfile, "mkdtemp", no_folder)
    result = _detected("h264_mf")
    assert not result.available
    assert "test recording" in result.reason


# ------------------------------- the A/V offset the operator dialled actually
# ------------------------------- arrives in the file


def _real_ffmpeg() -> Path:
    from wer.paths import ffmpeg_path

    exe = ffmpeg_path()
    if exe is None:
        pytest.skip("bundled ffmpeg missing; run tools/fetch-ffmpeg.ps1")
    return exe


def _pipe_frames(command: list[str], frame: bytes, *, fps: float, seconds: float) -> None:
    """Run a built command, feeding raw frames at a real rate.

    Paced deliberately. The command times its input by when frames arrive
    (-use_wallclock_as_timestamps), so shovelling them in as fast as the pipe
    takes them produces a file a few milliseconds long, and anything measured
    against it would be measuring the harness.
    """
    import subprocess
    import time

    process = subprocess.Popen(
        command, stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        started = time.perf_counter()
        index = 0
        while time.perf_counter() - started < seconds:
            try:
                process.stdin.write(frame)
            except OSError:
                break
            index += 1
            remaining = started + index / fps - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
        try:
            process.stdin.close()
        except OSError:
            pass
        process.wait(timeout=120)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def _describe(path: Path) -> str:
    import subprocess

    return subprocess.run(
        [str(_real_ffmpeg()), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, timeout=60,
    ).stderr


def _audio_start(path: Path) -> float:
    """Seconds the audio track is delayed by, as the file itself declares it.

    ffmpeg prints "start 0.478000" on the stream line only when the track
    carries a start delay; a track that begins with the video prints nothing,
    which is 0.0.
    """
    import re

    described = _describe(path)
    for line in described.splitlines():
        if "Audio:" in line:
            match = re.search(r"\bstart ([\d.]+)", line)
            return float(match.group(1)) if match else 0.0
    raise AssertionError(f"no audio stream in:\n{described}")


def _offset_command(exe: Path, output: Path, offset_ms: int) -> list[str]:
    """The real recording command, with the microphone swapped for a tone.

    Only the dshow input is replaced -- everything the muxer sees, including
    the -itsoffset under test, is exactly what a recording would use. A test
    machine cannot be assumed to have a capture device, and a lavfi tone is
    bounded where a live microphone is not.
    """
    command = build(
        OutputSettings(
            container=Container.MP4, audio_device="Test Mic",
            av_offset_ms=offset_ms, encoder="libx264", speed_preset="ultrafast",
        ),
        capture_width=320, capture_height=240, capture_fps=30.0,
        output_path=output, ffmpeg=exe,
    )
    dshow = command.index("dshow")
    # Replaces -f dshow, every DirectShow input option, and -i audio=Test Mic.
    # Found by the -i rather than counted, so a new input option cannot shift
    # the cut into the options that follow.
    device = command.index("-i", dshow) + 1
    return (
        command[: dshow - 1]
        + ["-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=10"]
        + command[device + 1 :]
    )


@pytest.mark.parametrize("offset_ms", [0, 500])
def test_a_positive_audio_offset_reaches_a_fragmented_mp4(tmp_path, offset_ms) -> None:
    """Measured, rather than asserted on the flag: the file's own alignment.

    A fragmented MP4 cannot write the edit list that expresses a track's start
    delay unless the moov box is held back until the first fragment, so
    +frag_keyframe+empty_moov alone silently threw the offset away -- ffmpeg
    exited 0 and said nothing, and +0 ms, +500 ms and +1000 ms produced
    identically aligned files. The slider still worked in the negative
    direction (that one is realised as duplicated leading video frames), so it
    worked one way and not the other. +delay_moov restores it while keeping the
    moof boxes that make an interrupted MP4 playable.

    The ~22 ms residual at 500 ms is AAC encoder priming delay: an unfragmented
    MP4 built from the same command reports the same 0.478.
    """
    import numpy as np

    exe = _real_ffmpeg()
    output = tmp_path / f"offset{offset_ms}.mp4"
    frame = np.zeros((240, 320, 3), np.uint8).tobytes()
    _pipe_frames(_offset_command(exe, output, offset_ms), frame, fps=30.0, seconds=3.0)

    assert output.is_file() and output.stat().st_size > 1000
    measured = _audio_start(output) * 1000
    assert abs(measured - offset_ms) < 60, (
        f"asked for {offset_ms} ms of audio delay; the file carries "
        f"{measured:.0f} ms"
    )


def test_the_mp4_is_still_fragmented(tmp_path) -> None:
    """The offset fix must not cost the crash resilience it sits next to.

    +delay_moov holds the moov box until the first fragment; it does not stop
    ffmpeg fragmenting. With no moof boxes an interrupted recording is
    unplayable again, which is the whole reason the flags are there.
    """
    import numpy as np

    exe = _real_ffmpeg()
    output = tmp_path / "fragmented.mp4"
    frame = np.zeros((240, 320, 3), np.uint8).tobytes()
    _pipe_frames(_offset_command(exe, output, 0), frame, fps=30.0, seconds=3.0)
    assert output.read_bytes().count(b"moof") > 0, "the MP4 came out unfragmented"


# ------------------------------------ something is on disk while it is recording


def test_the_file_is_not_empty_while_the_recording_is_still_running(tmp_path) -> None:
    """MKV is the default because an interrupted file must still play. That is
    worth nothing while the file is still zero bytes.

    ffmpeg buffers muxed output in a 256 KiB block and writes when it fills, so
    how long the file stays empty is purely a function of bitrate. Measured on
    a dark, static stage at 720p Compact -- which is what a lighting rehearsal
    with the house out actually looks like -- the first byte landed 94 seconds
    in, and a hard kill before that left a 0-byte file that would not open at
    all. With -flush_packets the same source had bytes on disk within two
    seconds.

    The static black frame here is not laziness: it is the lowest-bitrate case,
    and the one where the buffer takes longest to fill.

    So this one is killed rather than closed. A clean shutdown flushes
    everything and would pass whatever the buffering is doing, which is exactly
    how this went unnoticed.
    """
    import subprocess
    import time

    import numpy as np

    exe = _real_ffmpeg()
    output = tmp_path / "live.mkv"
    command = build(
        OutputSettings(quality_preset="compact", encoder="libx264",
                       speed_preset="ultrafast"),
        capture_width=1280, capture_height=720, capture_fps=30.0,
        output_path=output, ffmpeg=exe,
    )
    frame = np.zeros((720, 1280, 3), np.uint8).tobytes()
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        started = time.perf_counter()
        index = 0
        while time.perf_counter() - started < 6.0:
            process.stdin.write(frame)
            index += 1
            remaining = started + index / 30.0 - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
        written = output.stat().st_size if output.is_file() else 0
    finally:
        process.kill()                      # no flush, no trailer: a power cut
        process.wait(timeout=10)

    assert written > 0, (
        "six seconds in, nothing had reached the disk; a crash here leaves an "
        "unopenable file"
    )
    assert "Video: h264" in _describe(output), "the killed file will not open"


def test_fragmented_mp4_closes_fragments_on_a_timer_too() -> None:
    """+frag_keyframe closes a fragment only when a keyframe arrives and the
    buffer flushes. A time limit bounds what an interrupted MP4 loses."""
    command = build(OutputSettings(container=Container.MP4),
                    output_path=Path("out.mp4"))
    assert arg_after(command, "-frag_duration") == "2000000"


# ---------------------------------------------- a 4:3 camera is not stretched


def _white_square_aspect(exe: Path, video: Path) -> float:
    """Width/height of the white square in the recorded picture. 1.0 is round."""
    import re
    import subprocess

    import numpy as np

    result = subprocess.run(
        [str(exe), "-hide_banner", "-loglevel", "error", "-i", str(video),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, timeout=60,
    )
    described = _describe(video)
    match = re.search(r", (\d+)x(\d+)[ ,]", described)
    assert match, described
    width, height = int(match.group(1)), int(match.group(2))
    assert len(result.stdout) >= width * height, "no frame came back out"
    picture = np.frombuffer(result.stdout[: width * height], np.uint8)
    picture = picture.reshape(height, width)
    rows = np.where(picture.max(axis=1) > 200)[0]
    columns = np.where(picture.max(axis=0) > 200)[0]
    assert rows.size and columns.size, "the square did not survive at all"
    return (columns[-1] - columns[0] + 1) / (rows[-1] - rows[0] + 1)


def test_a_4_3_camera_recorded_at_16_9_is_not_stretched(tmp_path) -> None:
    """Geometric, not inferred.

    A 200x200 square from a 640x480 camera came out 400x300 at 1280x720 -- a
    1.333x horizontal stretch, held for the whole take, with nothing said about
    it. It reaches a real rig without anyone deliberately choosing a 4:3
    capture: best_format scores only on how close the height is to 1080, so a
    4:3 capture card defaults to 1440x1080, and "Record at 1920x1080" is then
    the obvious next click.
    """
    import numpy as np

    exe = _real_ffmpeg()
    output = tmp_path / "shape.mkv"
    image = np.zeros((480, 640, 3), np.uint8)
    image[140:340, 220:420] = 255           # a 200x200 square, dead centre
    command = build(
        OutputSettings(width=1280, height=720, encoder="libx264",
                       speed_preset="ultrafast"),
        capture_width=640, capture_height=480, capture_fps=30.0,
        output_path=output, ffmpeg=exe,
    )
    _pipe_frames(command, image.tobytes(), fps=30.0, seconds=1.5)

    aspect = _white_square_aspect(exe, output)
    assert abs(aspect - 1.0) < 0.05, (
        f"the square came out {aspect:.3f} times as wide as it is tall; the "
        f"picture is being stretched to fill a shape the camera is not"
    )


def test_matching_aspect_ratios_are_scaled_without_padding() -> None:
    """No pillarbox where none is needed, and the plain filter stays plain."""
    command = build(OutputSettings(width=1920, height=1080),
                    capture_width=3840, capture_height=2160)
    assert arg_after(command, "-vf") == "scale=1920:1080:flags=bicubic"


# -------------------------- Media Foundation reports a chip, it does not choose


def test_auto_never_passes_over_a_working_nvenc(clean_detection) -> None:
    """Media Foundation's choice of chip follows the machine's setup, so it
    must not decide.

    Same laptop, same driver, hours apart: detection said Media Foundation
    "works on NVIDIA" in the morning and "works on Intel" in the evening,
    because in between the Intel iGPU was re-enabled while debugging a
    Thunderbolt port and took the display back. The old rule asked Media
    Foundation first and returned the native encoder of whichever chip it was
    handed -- so the evening's soak recorded on Quick Sync, the iGPU, while a
    working h264_nvenc on a dedicated RTX 5070 Ti sat unused. Nothing failed
    and nothing was logged.
    """
    from wer.video.encoder import (
        AUTO_ENCODER, remember_detected_encoders, resolve_encoder_name,
    )

    remember_detected_encoders([
        _availability("libx264"),
        _availability("h264_nvenc"),
        _availability("h264_qsv"),
        _availability("h264_mf", hardware="Intel"),
    ])
    assert resolve_encoder_name(AUTO_ENCODER) == "h264_nvenc"


def test_auto_prefers_a_dedicated_gpu_over_the_integrated_one(clean_detection) -> None:
    """AMD ahead of Quick Sync on a machine with both.

    HARDWARE_ENCODERS lists Quick Sync before AMF, and the fallback used to
    follow that list, which put the iGPU first on an Intel-plus-Radeon laptop.
    """
    from wer.video.encoder import (
        AUTO_ENCODER, remember_detected_encoders, resolve_encoder_name,
    )

    remember_detected_encoders([
        _availability("libx264"),
        _availability("h264_qsv"),
        _availability("h264_amf"),
    ])
    assert resolve_encoder_name(AUTO_ENCODER) == "h264_amf"


def test_auto_takes_the_dedicated_card_through_media_foundation_over_the_igpu(
    clean_detection,
) -> None:
    """The dedicated GPU is worth the coarser controls.

    AMF will not open (no amfrt64.dll, say) but Media Foundation reaches the
    Radeon, while Quick Sync works natively on the iGPU. The dedicated chip
    wins, for the same reason the NVIDIA case does: headroom across four hours.
    """
    from wer.video.encoder import (
        AUTO_ENCODER, remember_detected_encoders, resolve_encoder_name,
    )

    remember_detected_encoders([
        _availability("libx264"),
        _availability("h264_qsv"),
        _availability("h264_amf", available=False),
        _availability("h264_mf", hardware="AMD"),
    ])
    assert resolve_encoder_name(AUTO_ENCODER) == "h264_mf"


# ------------------------------------------- the audio input's sample format


def _dshow_audio_input_options(command: list[str]) -> list[str]:
    """The arguments between the dshow audio -f and its -i."""
    start = command.index("dshow")
    return command[start + 1 : command.index("-i", start)]


def test_a_probed_audio_format_is_requested_before_the_input() -> None:
    """Input options only take effect before -i. After it they would be read
    as output options and the device would stay on 44.1 kHz."""
    command = build(OutputSettings(
        audio_device="Line In (Blackmagic UltraStudio Recorder 3G Audio)",
        audio_sample_rate=48000, audio_sample_bits=16, audio_channels=2,
    ))
    options = _dshow_audio_input_options(command)
    assert options[options.index("-sample_rate") + 1] == "48000"
    assert options[options.index("-sample_size") + 1] == "16"
    assert options[options.index("-channels") + 1] == "2"


def test_an_unprobed_device_keeps_its_own_format() -> None:
    """None is the safe outcome and must stay exactly today's command."""
    command = build(OutputSettings(audio_device="Mic"))
    for flag in ("-sample_rate", "-sample_size", "-channels"):
        assert flag not in command


def test_a_sample_format_without_an_audio_device_adds_nothing() -> None:
    command = build(OutputSettings(audio_sample_rate=48000, audio_channels=2))
    assert "-sample_rate" not in command and "dshow" not in command


def test_the_av_offset_still_lands_on_the_audio_input_with_a_format_set() -> None:
    """-itsoffset must stay immediately ahead of the input it shifts; the new
    options go between -f dshow and -i, not in front of -f."""
    command = build(OutputSettings(audio_device="Mic", av_offset_ms=40,
                                   audio_sample_rate=48000))
    offset_index = command.index("-itsoffset")
    assert command[offset_index + 2 : offset_index + 4] == ["-f", "dshow"]
