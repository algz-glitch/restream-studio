import asyncio
import json
import shutil
import subprocess
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast

import pytest

from restream_studio.media.ffprobe import (
    MediaProbeError,
    MediaProbeOutputTooLargeError,
    MediaProbeParseError,
    MediaProbeProcessError,
    MediaProbeTimeoutError,
    _parse_probe,
    probe_media,
)


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    try:
        coroutine.send(None)
    except StopIteration as stopped:
        return cast(T, stopped.value)
    raise AssertionError("fake subprocess unexpectedly suspended")


@pytest.fixture(autouse=True)
def immediate_wait_for(monkeypatch: pytest.MonkeyPatch) -> None:
    async def direct(awaitable: object, timeout: float) -> object:
        del timeout
        return await awaitable  # type: ignore[misc]

    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.wait_for", direct)


def _payload(*, rate: str = "30000/1001", include_video: bool = True) -> bytes:
    streams: list[dict[str, object]] = []
    if include_video:
        streams.append(
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": rate,
                "pix_fmt": "yuv420p",
            }
        )
    streams.append(
        {
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "48000",
            "channels": 2,
        }
    )
    return json.dumps({"streams": streams}).encode()


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int | None = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int | None:
        self.waited = True
        return self.returncode


def test_probe_uses_exec_argv_and_maps_stream_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []
    process = FakeProcess(_payload())

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        calls.append(args)
        assert kwargs["stdout"] is asyncio.subprocess.PIPE
        assert kwargs["stderr"] is asyncio.subprocess.PIPE
        return process

    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.create_subprocess_exec", fake_exec)
    result = _run(probe_media("https://media.example.test/live.flv?token=raw-secret"))

    assert calls == [
        (
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            "https://media.example.test/live.flv?token=raw-secret",
        )
    ]
    assert result.video_codec == "h264"
    assert (result.width, result.height) == (1920, 1080)
    assert result.frame_rate == pytest.approx(30000 / 1001)
    assert result.pixel_format == "yuv420p"
    assert result.audio_codec == "aac"
    assert result.audio_sample_rate == 48000
    assert result.audio_channels == 2


@pytest.mark.parametrize("rate", ["1/0", "__import__('os').system('whoami')", "nan", "1/2/3"])
def test_probe_rejects_unsafe_or_invalid_frame_rates(
    monkeypatch: pytest.MonkeyPatch, rate: str
) -> None:
    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess(_payload(rate=rate))

    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(MediaProbeParseError, match="frame rate"):
        _run(probe_media("https://media.example.test/live.m3u8"))


def test_probe_requires_video_and_valid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = iter((_payload(include_video=False), b"not-json"))

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess(next(outputs))

    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(MediaProbeParseError, match="video stream"):
        _run(probe_media("https://media.example.test/a.flv"))
    with pytest.raises(MediaProbeParseError, match="JSON"):
        _run(probe_media("https://media.example.test/b.flv"))


def test_probe_timeout_kills_and_reaps_without_leaking_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_url = "https://media.example.test/live.flv?token=DO-NOT-LEAK"

    class HangingProcess(FakeProcess):
        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    process = HangingProcess(b"", returncode=None)

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.create_subprocess_exec", fake_exec)
    async def timeout_wait(awaitable: object, timeout: float) -> object:
        del timeout
        awaitable.close()  # type: ignore[attr-defined]
        raise TimeoutError

    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.wait_for", timeout_wait)
    with pytest.raises(MediaProbeTimeoutError) as caught:
        _run(probe_media(secret_url, timeout=0.001))
    assert process.killed and process.waited
    assert "DO-NOT-LEAK" not in str(caught.value)


def test_probe_nonzero_and_spawn_errors_are_typed_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_url = "https://media.example.test/live.flv?signature=DO-NOT-LEAK"

    async def nonzero(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess(b"", f"failed {secret_url}".encode(), 1)

    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.create_subprocess_exec", nonzero)
    with pytest.raises(MediaProbeProcessError) as caught:
        _run(probe_media(secret_url))
    assert "DO-NOT-LEAK" not in str(caught.value)

    async def broken(*args: object, **kwargs: object) -> FakeProcess:
        raise OSError(f"spawn failed {secret_url}")

    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.create_subprocess_exec", broken)
    with pytest.raises(MediaProbeError) as caught_spawn:
        _run(probe_media(secret_url))
    assert "DO-NOT-LEAK" not in str(caught_spawn.value)


def test_probe_rejects_unsafe_input_and_oversized_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(MediaProbeError):
        _run(probe_media("file:///etc/passwd"))
    with pytest.raises(MediaProbeError):
        _run(probe_media("https://user:pass@example.test/live.flv"))

    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess(b"x" * 101)

    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(MediaProbeOutputTooLargeError):
        _run(probe_media("https://media.example.test/live.flv", max_output_bytes=100))


def test_local_ffprobe_generated_fixture_smoke(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("local FFmpeg tools are unavailable")
    fixture = tmp_path / "task6-smoke.mp4"
    generated = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x64:rate=25:duration=0.1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=sample_rate=48000:channel_layout=stereo",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(fixture),
        ],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert generated.returncode == 0
    probed = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(fixture)],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert probed.returncode == 0
    result = _parse_probe(probed.stdout)
    assert result.video_codec == "h264"
    assert result.audio_codec == "aac"
    assert (result.width, result.height, result.pixel_format) == (64, 64, "yuv420p")
