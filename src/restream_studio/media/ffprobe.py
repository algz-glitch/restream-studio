"""Bounded, shell-free ffprobe execution."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import socket
from collections.abc import Mapping
from dataclasses import replace
from itertools import pairwise
from statistics import median
from typing import cast
from urllib.parse import urlsplit

from restream_studio.domain import MediaProbe

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576
DEFAULT_MAX_STREAM_BYTES = 786_432
_READ_CHUNK_BYTES = 65_536
_RATIONAL = re.compile(r"^[0-9]+(?:\.[0-9]+)?(?:/[0-9]+(?:\.[0-9]+)?)?$")


class MediaProbeError(RuntimeError):
    """Base class for sanitized probing failures."""


class MediaProbeTimeoutError(MediaProbeError):
    pass


class MediaProbeProcessError(MediaProbeError):
    pass


class MediaProbeParseError(MediaProbeError):
    pass


class MediaProbeOutputTooLargeError(MediaProbeError):
    pass


def validate_input_url(value: str) -> str:
    if not isinstance(value, str) or not value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise MediaProbeError("media URL is malformed")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise MediaProbeError("media URL is malformed") from None
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise MediaProbeError("media URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise MediaProbeError("media URL must not contain userinfo")
    if port is not None and not 1 <= port <= 65535:
        raise MediaProbeError("media URL port is malformed")
    lowered_host = host.casefold().rstrip(".")
    if lowered_host == "localhost":
        raise MediaProbeError("media URL host must be public")
    try:
        address = ipaddress.ip_address(lowered_host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise MediaProbeError("media URL IP address must be public")
    return value


def _rational(value: object) -> float:
    if not isinstance(value, str) or _RATIONAL.fullmatch(value) is None:
        raise MediaProbeParseError("ffprobe frame rate is malformed")
    parts = value.split("/", maxsplit=1)
    numerator = float(parts[0])
    denominator = float(parts[1]) if len(parts) == 2 else 1.0
    if denominator == 0:
        raise MediaProbeParseError("ffprobe frame rate has a zero denominator")
    result = numerator / denominator
    if not math.isfinite(result) or result <= 0 or result > 1000:
        raise MediaProbeParseError("ffprobe frame rate is out of range")
    return result


def _positive_int(value: object, field: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise MediaProbeParseError(f"ffprobe {field} is malformed")
    if isinstance(value, str) and (not value or not value.isdecimal()):
        raise MediaProbeParseError(f"ffprobe {field} is malformed") from None
    result = int(value)
    if not 1 <= result <= maximum:
        raise MediaProbeParseError(f"ffprobe {field} is malformed")
    return result


def _frame_rate(video: Mapping[str, object]) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            return _rational(video.get(key))
        except MediaProbeParseError:
            continue
    raise MediaProbeParseError("ffprobe frame rate is malformed")


def _parse_probe(payload: bytes) -> MediaProbe:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MediaProbeParseError("ffprobe returned malformed JSON") from None
    if not isinstance(document, Mapping) or not isinstance(document.get("streams"), list):
        raise MediaProbeParseError("ffprobe JSON has no streams array")
    streams = cast(list[object], document["streams"])
    video = next(
        (item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "video"),
        None,
    )
    if video is None:
        raise MediaProbeParseError("ffprobe found no video stream")
    audio = next(
        (item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "audio"),
        None,
    )
    video_map = cast(Mapping[str, object], video)
    audio_map = cast(Mapping[str, object], audio) if audio is not None else {}
    codec = video_map.get("codec_name")
    if not isinstance(codec, str) or not codec:
        raise MediaProbeParseError("ffprobe video codec is malformed")
    audio_codec = audio_map.get("codec_name", "")
    pixel_format = video_map.get("pix_fmt", "")
    if not isinstance(audio_codec, str) or not isinstance(pixel_format, str):
        raise MediaProbeParseError("ffprobe codec metadata is malformed")
    profile_value = video_map.get("profile")
    profile = profile_value if isinstance(profile_value, str) and profile_value else None
    level_value = video_map.get("level")
    bitrate_value = video_map.get("bit_rate")
    frame_rate = _frame_rate(video_map)
    return MediaProbe(
        video_codec=codec,
        audio_codec=audio_codec,
        width=_positive_int(video_map.get("width"), "video width", maximum=16_384),
        height=_positive_int(video_map.get("height"), "video height", maximum=16_384),
        frame_rate=frame_rate,
        pixel_format=pixel_format,
        audio_sample_rate=(
            _positive_int(audio_map.get("sample_rate"), "audio sample rate", maximum=768_000) if audio else 0
        ),
        audio_channels=_positive_int(audio_map.get("channels"), "audio channels", maximum=64) if audio else 0,
        video_profile=profile,
        video_level=_positive_int(level_value, "video level", maximum=1_000) if level_value is not None else None,
        video_bitrate=_positive_int(bitrate_value, "video bitrate", maximum=1_000_000_000) if bitrate_value is not None else None,
        gop_seconds=None,
    )


def _parse_gop_seconds(payload: bytes) -> float | None:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, Mapping) or not isinstance(document.get("frames"), list):
        return None
    keyframe_times: list[float] = []
    for frame in cast(list[object], document["frames"]):
        if not isinstance(frame, Mapping) or frame.get("key_frame") != 1:
            continue
        value = frame.get("best_effort_timestamp_time")
        if not isinstance(value, str) or _RATIONAL.fullmatch(value) is None or "/" in value:
            continue
        timestamp = float(value)
        if math.isfinite(timestamp) and timestamp >= 0:
            keyframe_times.append(timestamp)
    intervals = [later - earlier for earlier, later in pairwise(keyframe_times)]
    if not intervals or any(interval <= 0 or interval > 60 for interval in intervals):
        return None
    result = median(intervals)
    if any(abs(interval - result) > max(0.05, result * 0.1) for interval in intervals):
        return None
    return result


async def _resolve_host_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except OSError:
        raise MediaProbeError("media URL host resolution failed") from None
    addresses = tuple({str(answer[4][0]) for answer in answers})
    if not addresses:
        raise MediaProbeError("media URL host resolution returned no addresses")
    return addresses


async def _validate_resolved_host(url: str) -> None:
    parsed = urlsplit(url)
    host = cast(str, parsed.hostname)
    try:
        ipaddress.ip_address(host.rstrip("."))
        return
    except ValueError:
        pass
    addresses = await _resolve_host_addresses(
        host, parsed.port or (443 if parsed.scheme == "https" else 80)
    )
    try:
        has_non_public = any(not ipaddress.ip_address(address).is_global for address in addresses)
    except ValueError:
        raise MediaProbeError("media URL host resolution returned malformed addresses") from None
    if has_non_public:
        raise MediaProbeError("media URL host must resolve only to public addresses")


async def _terminate_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def _read_bounded(
    stream: asyncio.StreamReader, per_stream: int, total: list[int], lock: asyncio.Lock
) -> bytes:
    output = bytearray()
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        async with lock:
            total[0] += len(chunk)
            if len(output) > per_stream or total[0] > total[1]:
                raise MediaProbeOutputTooLargeError("ffprobe output exceeded the configured limit")


async def _read_process(
    process: asyncio.subprocess.Process, max_output_bytes: int, max_stream_bytes: int
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise MediaProbeError("ffprobe pipes are unavailable")
    total = [0, max_output_bytes]
    lock = asyncio.Lock()
    stdout_task = asyncio.create_task(_read_bounded(process.stdout, max_stream_bytes, total, lock))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, max_stream_bytes, total, lock))
    try:
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    except BaseException:
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    await process.wait()
    return stdout, stderr


async def _execute_ffprobe(
    executable: str,
    url: str,
    options: tuple[str, ...],
    timeout: float,
    max_output_bytes: int,
    max_stream_bytes: int,
) -> bytes:
    argv = (
        executable,
        "-v",
        "error",
        "-protocol_whitelist",
        "http,https,tcp,tls,crypto",
        "-max_redirects",
        "0",
        *options,
        "-of",
        "json",
        url,
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError:
        raise MediaProbeError("unable to start ffprobe") from None
    try:
        stdout, _stderr = await asyncio.wait_for(
            _read_process(process, max_output_bytes, max_stream_bytes), timeout=timeout
        )
    except TimeoutError as exc:
        await _terminate_and_reap(process)
        raise MediaProbeTimeoutError("ffprobe timed out") from exc
    except BaseException:
        await _terminate_and_reap(process)
        raise
    if process.returncode != 0:
        raise MediaProbeProcessError("ffprobe exited with a non-zero status")
    return stdout


async def probe_media(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_stream_bytes: int = DEFAULT_MAX_STREAM_BYTES,
    executable: str = "ffprobe",
) -> MediaProbe:
    """Probe one HTTP(S) input without a shell and return immutable metadata."""
    validated_url = validate_input_url(url)
    if timeout <= 0 or max_output_bytes <= 0 or max_stream_bytes <= 0:
        raise ValueError("timeout and output limits must be positive")
    await _validate_resolved_host(validated_url)
    stdout = await _execute_ffprobe(
        executable,
        validated_url,
        ("-show_streams",),
        timeout,
        max_output_bytes,
        max_stream_bytes,
    )
    result = _parse_probe(stdout)
    try:
        frame_stdout = await _execute_ffprobe(
            executable,
            validated_url,
            (
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=key_frame,best_effort_timestamp_time",
                "-read_intervals",
                "%+6",
            ),
            timeout,
            max_output_bytes,
            max_stream_bytes,
        )
    except MediaProbeError:
        return result
    return replace(result, gop_seconds=_parse_gop_seconds(frame_stdout))
