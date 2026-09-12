"""Construct one safe FFmpeg process command per streaming destination."""

from __future__ import annotations

import ipaddress
import subprocess
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from restream_studio.domain import DestinationKind, MediaProbe
from restream_studio.media.ffprobe import MediaProbeError, validate_input_url


class CommandValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FfmpegCommand:
    argv: tuple[str, ...] = field(repr=False)
    display_argv: tuple[str, ...]
    display_command: str


@dataclass(frozen=True, slots=True)
class _Preset:
    max_width: int
    max_height: int
    max_fps: float
    output_fps: int
    video_bitrate: str
    maxrate: str
    bufsize: str
    audio_bitrate: str
    sample_rate: int
    channels: int
    copy_max_bitrate: int
    copy_max_level: int


_PRESETS = {
    DestinationKind.DOUYIN: _Preset(1920, 1080, 30, 30, "5000k", "6000k", "10000k", "160k", 48000, 2, 6_000_000, 42),
    DestinationKind.WECHAT: _Preset(1920, 1080, 30, 30, "4000k", "5000k", "8000k", "128k", 48000, 2, 5_000_000, 42),
    DestinationKind.LOCAL_TEST: _Preset(1920, 1080, 30, 30, "4000k", "5000k", "8000k", "128k", 48000, 2, 5_000_000, 42),
}


def _destination_url(value: str) -> str:
    if not isinstance(value, str) or not value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise CommandValidationError("output URL is malformed")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise CommandValidationError("output URL is malformed") from None
    if parsed.scheme.lower() not in {"rtmp", "rtmps"} or not host:
        raise CommandValidationError("output URL must use RTMP or RTMPS")
    if parsed.username is not None or parsed.password is not None:
        raise CommandValidationError("output URL must not contain userinfo")
    if port is not None and not 1 <= port <= 65535:
        raise CommandValidationError("output URL port is malformed")
    try:
        address = ipaddress.ip_address(host.rstrip("."))
    except ValueError:
        if host.casefold().rstrip(".") == "localhost":
            raise CommandValidationError("output URL host must be public") from None
    else:
        if not address.is_global:
            raise CommandValidationError("output URL IP address must be public")
    return value


def _can_copy(probe: MediaProbe, preset: _Preset) -> bool:
    return (
        probe.video_codec.casefold() == "h264"
        and probe.audio_codec.casefold() == "aac"
        and 0 < probe.width <= preset.max_width
        and 0 < probe.height <= preset.max_height
        and (
            abs(probe.frame_rate - preset.output_fps) <= 0.01
            or abs(probe.frame_rate - (preset.output_fps * 1000 / 1001)) <= 0.01
        )
        and probe.pixel_format.casefold() == "yuv420p"
        and probe.audio_sample_rate == preset.sample_rate
        and probe.audio_channels == preset.channels
        and probe.video_profile is not None
        and probe.video_profile.casefold() in {"baseline", "main", "high"}
        and probe.video_level is not None
        and probe.video_level <= preset.copy_max_level
        and probe.video_bitrate is not None
        and probe.video_bitrate <= preset.copy_max_bitrate
        and probe.gop_seconds is not None
        and 1.9 <= probe.gop_seconds <= 2.1
    )


def _display_source_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, "/***", "", ""))


def _display_output_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, "/***", "", ""))


def build_ffmpeg_command(
    source_url: str,
    destination_url: str,
    destination: DestinationKind,
    probe: MediaProbe,
    *,
    executable: str = "ffmpeg",
) -> FfmpegCommand:
    """Build exactly one shell-free output command."""
    try:
        source = validate_input_url(source_url)
    except MediaProbeError as exc:
        raise CommandValidationError(str(exc)) from None
    output = _destination_url(destination_url)
    try:
        preset = _PRESETS[destination]
    except KeyError as exc:
        raise CommandValidationError("unsupported destination preset") from exc
    args = [
        executable,
        "-hide_banner",
        "-nostdin",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_at_eof",
        "1",
        "-reconnect_delay_max",
        "5",
        "-i",
        source,
    ]
    if _can_copy(probe, preset):
        args.extend(("-c", "copy"))
    else:
        keyframe_interval = preset.output_fps * 2
        args.extend(
            (
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(preset.output_fps),
                "-b:v",
                preset.video_bitrate,
                "-maxrate",
                preset.maxrate,
                "-bufsize",
                preset.bufsize,
                "-g",
                str(keyframe_interval),
                "-keyint_min",
                str(keyframe_interval),
                "-sc_threshold",
                "0",
                "-c:a",
                "aac",
                "-b:a",
                preset.audio_bitrate,
                "-ar",
                str(preset.sample_rate),
                "-ac",
                str(preset.channels),
            )
        )
    args.extend(("-f", "flv", output))
    argv = tuple(args)
    display_argv = tuple(
        _display_source_url(item)
        if item == source
        else _display_output_url(item)
        if item == output
        else item
        for item in argv
    )
    return FfmpegCommand(
        argv=argv,
        display_argv=display_argv,
        display_command=subprocess.list2cmdline(display_argv),
    )
