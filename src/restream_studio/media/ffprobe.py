"""Bounded, shell-free ffprobe execution."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Mapping
from typing import cast
from urllib.parse import urlsplit

from restream_studio.domain import MediaProbe

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576
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


def _positive_int(value: object, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise MediaProbeParseError(f"ffprobe {field} is malformed")
    try:
        result = int(cast(str | int, value))
    except (TypeError, ValueError, OverflowError):
        raise MediaProbeParseError(f"ffprobe {field} is malformed") from None
    if result < (0 if allow_zero else 1):
        raise MediaProbeParseError(f"ffprobe {field} is malformed")
    return result


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
    return MediaProbe(
        video_codec=codec,
        audio_codec=audio_codec,
        width=_positive_int(video_map.get("width"), "video width"),
        height=_positive_int(video_map.get("height"), "video height"),
        frame_rate=_rational(video_map.get("avg_frame_rate", video_map.get("r_frame_rate"))),
        pixel_format=pixel_format,
        audio_sample_rate=(
            _positive_int(audio_map.get("sample_rate"), "audio sample rate") if audio else 0
        ),
        audio_channels=_positive_int(audio_map.get("channels"), "audio channels") if audio else 0,
    )


async def _terminate_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def probe_media(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    executable: str = "ffprobe",
) -> MediaProbe:
    """Probe one HTTP(S) input without a shell and return immutable metadata."""
    validated_url = validate_input_url(url)
    if timeout <= 0 or max_output_bytes <= 0:
        raise ValueError("timeout and max_output_bytes must be positive")
    argv = (
        executable,
        "-v",
        "error",
        "-show_streams",
        "-of",
        "json",
        validated_url,
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError:
        raise MediaProbeError("unable to start ffprobe") from None
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        await _terminate_and_reap(process)
        raise MediaProbeTimeoutError("ffprobe timed out") from exc
    except BaseException:
        await _terminate_and_reap(process)
        raise
    if len(stdout) + len(stderr) > max_output_bytes:
        raise MediaProbeOutputTooLargeError("ffprobe output exceeded the configured limit")
    if process.returncode != 0:
        raise MediaProbeProcessError("ffprobe exited with a non-zero status")
    return _parse_probe(stdout)
