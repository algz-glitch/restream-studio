import asyncio
import json
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from restream_studio.media.ffprobe import (
    MediaProbeError,
    MediaProbeOutputTooLargeError,
    MediaProbeParseError,
    MediaProbeTimeoutError,
    _parse_gop_seconds,
    _parse_probe,
    probe_media,
)


def payload(**changes: object) -> bytes:
    video: dict[str, object] = {
        "codec_type": "video", "codec_name": "h264", "profile": "High", "level": 40,
        "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001",
        "r_frame_rate": "30/1", "pix_fmt": "yuv420p", "bit_rate": "4500000",
    }
    video.update(changes)
    return json.dumps({"streams": [video, {"codec_type": "audio", "codec_name": "aac",
        "sample_rate": "48000", "channels": 2}]}).encode()


def test_stream_gop_size_is_not_treated_as_keyframe_evidence() -> None:
    assert _parse_probe(payload(gop_size=60)).gop_seconds is None


class Stream:
    def __init__(self, chunks: list[bytes], hang: bool = False) -> None:
        self.chunks, self.hang, self.reads = chunks, hang, 0

    async def read(self, size: int) -> bytes:
        self.reads += 1
        if self.hang:
            await asyncio.Event().wait()
        return self.chunks.pop(0) if self.chunks else b""


class Process:
    def __init__(self, out: list[bytes], err: list[bytes] | None = None,
                 returncode: int | None = 0, hang: bool = False) -> None:
        self.stdout, self.stderr = Stream(out, hang), Stream(err or [], hang)
        self.returncode, self.killed, self.waited = returncode, False, False

    def kill(self) -> None:
        self.killed, self.returncode = True, -9

    async def wait(self) -> int | None:
        self.waited = True
        return self.returncode


@pytest.fixture(autouse=True)
def dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def public(host: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)
    monkeypatch.setattr("restream_studio.media.ffprobe._resolve_host_addresses", public)


def install(monkeypatch: pytest.MonkeyPatch, process: Process, calls: list[tuple[object, ...]] | None = None) -> None:
    async def spawn(*args: object, **kwargs: object) -> Process:
        if calls is not None:
            calls.append(args)
        return process
    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.create_subprocess_exec", spawn)


@pytest.mark.asyncio
async def test_chunk_reads_protocol_allowlist_and_extended_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = payload()
    calls: list[tuple[object, ...]] = []
    process = Process([raw[:30], raw[30:]], [b"warning"])
    install(monkeypatch, process, calls)
    result = await probe_media("https://media.example.test/path?token=raw")
    assert calls[0][calls[0].index("-protocol_whitelist") + 1] == "http,https,tcp,tls,crypto"
    assert calls[0][calls[0].index("-max_redirects") + 1] == "0"
    assert process.stdout.reads >= 2 and process.stderr.reads >= 1
    assert (result.video_profile, result.video_level, result.video_bitrate) == ("High", 40, 4_500_000)
    assert result.gop_seconds is None


@pytest.mark.asyncio
async def test_frame_probe_computes_gop_from_two_keyframes(monkeypatch: pytest.MonkeyPatch) -> None:
    processes = iter([
        Process([payload()]),
        Process([json.dumps({"frames": [
            {"key_frame": 1, "best_effort_timestamp_time": "0.000000"},
            {"key_frame": 0, "best_effort_timestamp_time": "1.000000"},
            {"key_frame": 1, "best_effort_timestamp_time": "2.002000"},
        ]}).encode()]),
    ])
    calls: list[tuple[object, ...]] = []
    async def spawn(*args: object, **kwargs: object) -> Process:
        calls.append(args)
        return next(processes)
    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.create_subprocess_exec", spawn)
    result = await probe_media("https://media.example.test/live")
    assert result.gop_seconds == pytest.approx(2.002)
    assert "-show_frames" in calls[1]
    assert calls[1][calls[1].index("-read_intervals") + 1] == "%+6"


@pytest.mark.asyncio
async def test_frame_probe_needs_two_keyframes(monkeypatch: pytest.MonkeyPatch) -> None:
    processes = iter([Process([payload()]), Process([b'{"frames":[{"key_frame":1,"best_effort_timestamp_time":"0"}]}'])])
    async def spawn(*args: object, **kwargs: object) -> Process:
        return next(processes)
    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.create_subprocess_exec", spawn)
    assert (await probe_media("https://media.example.test/live")).gop_seconds is None


@pytest.mark.asyncio
async def test_frame_probe_timeout_kills_reaps_and_returns_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_process = Process([payload()])
    frame_process = Process([], returncode=None, hang=True)
    processes = iter([metadata_process, frame_process])

    async def spawn(*args: object, **kwargs: object) -> Process:
        return next(processes)

    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.create_subprocess_exec", spawn)
    result = await probe_media("https://media.example.test/live", timeout=0.05)
    assert result.gop_seconds is None
    assert frame_process.killed and frame_process.waited


@pytest.mark.asyncio
async def test_frame_probe_output_limit_kills_reaps_and_returns_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_process = Process([payload()])
    frame_process = Process([b"x" * 1_001], returncode=None)
    processes = iter([metadata_process, frame_process])

    async def spawn(*args: object, **kwargs: object) -> Process:
        return next(processes)

    monkeypatch.setattr("restream_studio.media.ffprobe.asyncio.create_subprocess_exec", spawn)
    result = await probe_media(
        "https://media.example.test/live", max_output_bytes=1_000, max_stream_bytes=1_000
    )
    assert result.gop_seconds is None
    assert frame_process.killed and frame_process.waited


@pytest.mark.asyncio
@pytest.mark.parametrize("per_stream", [False, True])
async def test_output_limit_immediately_kills_and_reaps(monkeypatch: pytest.MonkeyPatch, per_stream: bool) -> None:
    process = Process([b"a" * 81, b"b" * 81], [b"c" * 60], None)
    install(monkeypatch, process)
    with pytest.raises(MediaProbeOutputTooLargeError):
        await probe_media(
            "https://media.example.test/live",
            max_output_bytes=100,
            max_stream_bytes=80 if per_stream else 100,
        )
    assert process.killed and process.waited


@pytest.mark.asyncio
async def test_timeout_kills_and_reaps(monkeypatch: pytest.MonkeyPatch) -> None:
    process = Process([], returncode=None, hang=True)
    install(monkeypatch, process)
    with pytest.raises(MediaProbeTimeoutError):
        await probe_media("https://media.example.test/live?token=secret", timeout=0.001)
    assert process.killed and process.waited


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["file:///x", "https://u:p@example.test/x", "https://localhost/x",
    "https://127.0.0.1/x", "https://10.0.0.1/x", "https://[::1]/x"])
async def test_rejects_unsafe_input(url: str) -> None:
    with pytest.raises(MediaProbeError):
        await probe_media(url)


@pytest.mark.asyncio
async def test_rejects_any_non_global_dns_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mixed(host: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34", "10.0.0.8")
    monkeypatch.setattr("restream_studio.media.ffprobe._resolve_host_addresses", mixed)
    with pytest.raises(MediaProbeError, match="public"):
        await probe_media("https://media.example.test/live")


@pytest.mark.asyncio
async def test_dns_uses_async_getaddrinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    calls: list[tuple[str, int]] = []
    async def answer(host: str, port: int, **kwargs: int) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", answer)
    install(monkeypatch, Process([payload()]))
    await probe_media("https://media.example.test/live")
    assert calls == [("media.example.test", 443)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("avg", "real", "fps"), [("0/0", "25/1", 25), ("0", "30/1", 30), ("bad", "24/1", 24)])
async def test_bad_average_rate_falls_back(monkeypatch: pytest.MonkeyPatch, avg: str, real: str, fps: float) -> None:
    install(monkeypatch, Process([payload(avg_frame_rate=avg, r_frame_rate=real)]))
    assert (await probe_media("https://media.example.test/live")).frame_rate == pytest.approx(fps)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, 1.0, "1.0", "-1", "999999999999999999999"])
async def test_integer_fields_are_strict_and_bounded(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    install(monkeypatch, Process([payload(width=value)]))
    with pytest.raises(MediaProbeParseError, match="width"):
        await probe_media("https://media.example.test/live")


@pytest.mark.asyncio
async def test_malformed_json_and_missing_video_are_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw, message in [(b"bad", "JSON"), (b'{"streams":[]}', "video")]:
        install(monkeypatch, Process([raw]))
        with pytest.raises(MediaProbeParseError, match=message):
            await probe_media("https://media.example.test/live")


def test_local_generated_fixture_smoke(tmp_path: Path) -> None:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("local FFmpeg tools are unavailable")
    fixture = tmp_path / "task6.mp4"
    generated = subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=size=64x64:rate=30:duration=5",
         "-c:v", "libx264", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
         "-pix_fmt", "yuv420p", str(fixture)],
        capture_output=True, check=False, timeout=10,
    )
    assert generated.returncode == 0
    probed = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(fixture)],
        capture_output=True, check=False, timeout=10,
    )
    assert probed.returncode == 0
    assert _parse_probe(probed.stdout).video_codec == "h264"
    frames = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
         "-show_entries", "frame=key_frame,best_effort_timestamp_time", "-read_intervals", "%+5",
         "-of", "json", str(fixture)], capture_output=True, check=False, timeout=10,
    )
    assert frames.returncode == 0
    assert _parse_gop_seconds(frames.stdout) == pytest.approx(2.0, abs=0.02)
