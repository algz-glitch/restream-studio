from dataclasses import fields

import pytest

from restream_studio.domain import DestinationKind, MediaProbe
from restream_studio.media.ffmpeg_commands import (
    CommandValidationError,
    FfmpegCommand,
    build_ffmpeg_command,
)


def compatible_probe() -> MediaProbe:
    return MediaProbe(
        "h264", "aac", 1920, 1080, 30.0, "yuv420p", 48000, 2,
        video_profile="High", video_level=40, video_bitrate=4_500_000, gop_seconds=2.0,
    )


def test_compatible_probe_uses_copy_and_redacts_raw_destination_key() -> None:
    raw = "rtmps://push.example.com/live/SUPER-SECRET-KEY?token=RAW"
    command = build_ffmpeg_command(
        "https://media.example.test/live.flv?token=SOURCE-RAW",
        raw,
        DestinationKind.DOUYIN,
        compatible_probe(),
    )
    assert command.argv[-1] == raw
    assert command.argv.count("-c") == 1
    assert command.argv[command.argv.index("-c") + 1] == "copy"
    assert "SUPER-SECRET-KEY" not in command.display_command
    assert "SOURCE-RAW" not in command.display_command
    assert "SUPER-SECRET-KEY" not in repr(command)
    assert "SOURCE-RAW" not in repr(command)
    assert next(item for item in fields(FfmpegCommand) if item.name == "argv").repr is False


def test_display_redacts_all_source_query_values_and_single_segment_output_key() -> None:
    command = build_ffmpeg_command(
        "https://media.example.test/live.flv?wsSecret=SOURCE-SECRET&expires=123",
        "rtmp://push.example.com/ONLY-SECRET-KEY",
        DestinationKind.DOUYIN,
        compatible_probe(),
    )
    assert "SOURCE-SECRET" not in command.display_command
    assert "ONLY-SECRET-KEY" not in command.display_command
    assert command.argv[-1].endswith("ONLY-SECRET-KEY")
    assert command.display_argv[command.display_argv.index("-i") + 1] == "https://media.example.test/***"
    assert command.display_argv[-1] == "rtmp://push.example.com/***"


def test_display_redacts_complete_paths_queries_and_fragments() -> None:
    command = build_ffmpeg_command(
        "https://media.example.test/path/SOURCE-SIGNATURE?x=secret#source-fragment",
        "rtmps://push.example.com/app/OUTPUT-SIGNATURE?auth=secret#output-fragment",
        DestinationKind.DOUYIN,
        compatible_probe(),
    )
    for secret in ("SOURCE-SIGNATURE", "OUTPUT-SIGNATURE", "source-fragment", "output-fragment"):
        assert secret not in command.display_command


@pytest.mark.parametrize(
    "probe",
    [
        MediaProbe("hevc", "aac", 1920, 1080, 30, "yuv420p", 48000, 2),
        MediaProbe("h264", "aac", 3840, 2160, 30, "yuv420p", 48000, 2),
        MediaProbe("h264", "aac", 1920, 1080, 31, "yuv420p", 48000, 2),
        MediaProbe("h264", "aac", 1920, 1080, 30, "yuv444p", 48000, 2),
        MediaProbe("h264", "aac", 1920, 1080, 30, "yuv420p", 96000, 2),
        MediaProbe("h264", "aac", 1920, 1080, 30, "yuv420p", 48000, 6),
    ],
)
def test_copy_requires_every_compatibility_constraint(probe: MediaProbe) -> None:
    command = build_ffmpeg_command(
        "https://media.example.test/live.m3u8",
        "rtmp://push.example.com/live/key",
        DestinationKind.WECHAT,
        probe,
    )
    assert "libx264" in command.argv
    assert "aac" in command.argv
    assert "yuv420p" in command.argv
    assert "-g" in command.argv
    assert command.argv[command.argv.index("-g") + 1] == "60"
    assert "-maxrate" in command.argv and "-bufsize" in command.argv


@pytest.mark.parametrize(
    "probe",
    [
        MediaProbe("h264", "aac", 1920, 1080, 30, "yuv420p", 48000, 2),
        MediaProbe("h264", "aac", 1920, 1080, 30, "yuv420p", 48000, 2, video_profile="High", video_level=40, video_bitrate=4_500_000),
        MediaProbe("h264", "aac", 1920, 1080, 30, "yuv420p", 48000, 2, video_profile="High", video_level=40, video_bitrate=7_000_000, gop_seconds=2.0),
        MediaProbe("h264", "aac", 1920, 1080, 30, "yuv420p", 44100, 1, video_profile="High", video_level=40, video_bitrate=4_500_000, gop_seconds=2.0),
        MediaProbe("h264", "aac", 1920, 1080, 30, "yuv420p", 48000, 2, video_profile="High", video_level=51, video_bitrate=4_500_000, gop_seconds=2.0),
    ],
)
def test_unknown_or_out_of_bounds_copy_evidence_forces_transcode(probe: MediaProbe) -> None:
    command = build_ffmpeg_command(
        "https://media.example.test/live.m3u8", "rtmp://push.example.com/live/key",
        DestinationKind.DOUYIN, probe,
    )
    assert "libx264" in command.argv


@pytest.mark.parametrize("fps", [15.0, 24.0, 25.0])
def test_non_preset_frame_rates_force_transcode(fps: float) -> None:
    probe = compatible_probe()
    probe = MediaProbe(
        probe.video_codec, probe.audio_codec, probe.width, probe.height, fps,
        probe.pixel_format, probe.audio_sample_rate, probe.audio_channels,
        probe.video_profile, probe.video_level, probe.video_bitrate, probe.gop_seconds,
    )
    command = build_ffmpeg_command(
        "https://media.example.test/live", "rtmp://push.example.com/live/key",
        DestinationKind.DOUYIN, probe,
    )
    assert "libx264" in command.argv


@pytest.mark.parametrize("fps", [30.0, 29.97, 30000 / 1001])
def test_30_and_ntsc_2997_allow_copy_when_all_other_evidence_matches(fps: float) -> None:
    probe = compatible_probe()
    probe = MediaProbe(
        probe.video_codec, probe.audio_codec, probe.width, probe.height, fps,
        probe.pixel_format, probe.audio_sample_rate, probe.audio_channels,
        probe.video_profile, probe.video_level, probe.video_bitrate, probe.gop_seconds,
    )
    command = build_ffmpeg_command(
        "https://media.example.test/live", "rtmp://push.example.com/live/key",
        DestinationKind.DOUYIN, probe,
    )
    assert command.argv[command.argv.index("-c") + 1] == "copy"


def test_hls_and_flv_inputs_include_reconnect_options_and_one_output() -> None:
    for source in ("https://media.example.test/live.flv", "https://media.example.test/live.m3u8"):
        command = build_ffmpeg_command(
            source,
            "rtmp://push.example.com/live/key",
            DestinationKind.DOUYIN,
            compatible_probe(),
        )
        assert "-reconnect" in command.argv
        assert "-reconnect_streamed" in command.argv
        assert command.argv[-2:] == ("-f", "flv") or command.argv[-1].startswith("rtmp")
        assert sum(arg.startswith(("rtmp://", "rtmps://")) for arg in command.argv) == 1


def test_output_command_enables_machine_readable_progress_on_stderr() -> None:
    command = build_ffmpeg_command(
        "https://media.example.test/live.flv",
        "rtmp://push.example.com/live/key",
        DestinationKind.DOUYIN,
        compatible_probe(),
    )

    assert command.argv.count("-progress") == 1
    progress_index = command.argv.index("-progress")
    assert command.argv[progress_index + 1] == "pipe:2"
    assert command.argv.count("-nostats") == 1
    assert progress_index < command.argv.index("-f")


def test_local_test_command_allows_explicit_loopback_source_and_destination() -> None:
    command = build_ffmpeg_command(
        "rtmp://127.0.0.1:1935/source/main",
        "rtmp://127.0.0.1:1935/target/douyin",
        DestinationKind.LOCAL_TEST,
        compatible_probe(),
    )

    assert "rtmp://127.0.0.1:1935/source/main" in command.argv
    assert "rtmp://127.0.0.1:1935/target/douyin" in command.argv
    assert "-reconnect" not in command.argv
    assert "-reconnect_streamed" not in command.argv


@pytest.mark.parametrize(
    "destination",
    [
        "http://push.example.com/live/key",
        "rtmp://user:pass@push.example.com/live/key",
        "rtmp://127.0.0.1/live/key",
        "rtmp://10.0.0.1/live/key",
        "rtmp://[::1]/live/key",
        "rtmp://push.example.com/live/key\n-injected",
    ],
)
def test_rejects_unsafe_output_destinations(destination: str) -> None:
    with pytest.raises(CommandValidationError):
        build_ffmpeg_command(
            "https://media.example.test/live.flv",
            destination,
            DestinationKind.DOUYIN,
            compatible_probe(),
        )


def test_each_destination_is_built_as_an_independent_process() -> None:
    first = build_ffmpeg_command(
        "https://media.example.test/live.flv",
        "rtmp://one.example.com/live/key-one",
        DestinationKind.DOUYIN,
        compatible_probe(),
    )
    second = build_ffmpeg_command(
        "https://media.example.test/live.flv",
        "rtmps://two.example.com/live/key-two",
        DestinationKind.WECHAT,
        compatible_probe(),
    )
    assert "two.example.com" not in " ".join(first.argv)
    assert "one.example.com" not in " ".join(second.argv)
    assert first.argv != second.argv
