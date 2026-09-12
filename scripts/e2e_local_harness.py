"""Application-driven local RTMP acceptance harness.

Only the source publisher is a test fixture. Every target FFmpeg process is
created, restarted, and stopped through the production Controller and
OutputSupervisor lifecycle.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, cast
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from restream_studio.domain import (
    DestinationKind,
    MediaProbe,
    OutputState,
    ResolvedStream,
    SourceState,
)
from restream_studio.orchestration import (
    ConfiguredDestination,
    ConfiguredSource,
    Controller,
    ControllerSnapshot,
    OutputInput,
    StandbyMedia,
)
from restream_studio.outputs import OutputSupervisor
from restream_studio.source import ResolverNetworkError

SOURCE_URL = "rtmp://127.0.0.1:1935/source/main"
TARGET_URLS = {
    "douyin": "rtmp://127.0.0.1:1935/target/douyin",
    "wechat": "rtmp://127.0.0.1:1935/target/wechat",
}
MEDIA_MTX_API = "http://127.0.0.1:9997/v3/paths/list"
ROOM_IDENTITY = "https://live.douyin.com/local-e2e"


class HarnessError(RuntimeError):
    pass


def _media_mtx_paths() -> dict[str, bool]:
    try:
        with urlopen(MEDIA_MTX_API, timeout=2.0) as response:
            payload = json.load(response)
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return {}
    items = payload.get("items", []) if isinstance(payload, dict) else []
    return {
        str(item.get("name")): item.get("ready") is True
        for item in items
        if isinstance(item, dict)
    }


async def path_ready(name: str) -> bool:
    return (await asyncio.to_thread(_media_mtx_paths)).get(name, False)


class LocalSourceResolver:
    """Test-only resolver mapping the configured room to the loopback publisher."""

    async def resolve(self, url: str, preferred_quality: str | None) -> ResolvedStream:
        del url, preferred_quality
        if not await path_ready("source/main"):
            raise ResolverNetworkError("local source is unavailable")
        return ResolvedStream(
            url=SOURCE_URL,
            acquired_at=datetime.now(UTC),
            expires_at=None,
            room_id="local-e2e",
            is_live=True,
            selected_quality="deterministic",
        )


class LocalMediaProbe:
    def __init__(self, ffprobe: str) -> None:
        self._ffprobe = ffprobe

    async def probe(self, url: str) -> MediaProbe:
        payload = await _ffprobe(self._ffprobe, url)
        video, audio = _select_streams(payload)
        return MediaProbe(
            video_codec=str(video["codec_name"]),
            audio_codec=str(audio["codec_name"]),
            width=int(video["width"]),
            height=int(video["height"]),
            frame_rate=30.0,
            pixel_format=str(video.get("pix_fmt", "yuv420p")),
            audio_sample_rate=int(audio.get("sample_rate", 48000)),
            audio_channels=int(audio.get("channels", 2)),
            video_profile=str(video.get("profile", "Main")),
            video_level=int(video.get("level", 40)),
            video_bitrate=1_000_000,
            gop_seconds=2.0,
        )


class LocalRtmpDestinationAdapter:
    """Test destination command adapter backed by the production supervisor."""

    def __init__(
        self,
        *,
        identity: str,
        destination: DestinationKind,
        target_url: str,
        ffmpeg: str,
    ) -> None:
        self.identity = identity
        self.destination = destination
        self._target_url = target_url
        self._ffmpeg = ffmpeg
        self._supervisor: OutputSupervisor | None = None
        self._state = OutputState.STOPPED

    @property
    def state(self) -> OutputState:
        return self._supervisor.state if self._supervisor is not None else self._state

    @state.setter
    def state(self, value: OutputState) -> None:
        self._state = value

    def prepare_live(self, source_url: str, probe: MediaProbe) -> tuple[str, ...]:
        del probe
        return (
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-rw_timeout",
            "5000000",
            "-i",
            source_url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-progress",
            "pipe:2",
            "-nostats",
            "-f",
            "flv",
            self._target_url,
        )

    def prepare_standby(self, standby: StandbyMedia) -> tuple[str, ...]:
        del standby
        return (
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x183153:size=320x180:rate=30",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-progress",
            "pipe:2",
            "-nostats",
            "-f",
            "flv",
            self._target_url,
        )

    async def restart(self, command: object) -> None:
        if not isinstance(command, tuple) or not all(isinstance(item, str) for item in command):
            raise HarnessError("controller prepared an invalid local output command")
        await self.stop()
        self._supervisor = OutputSupervisor(
            self.destination,
            cast(tuple[str, ...], command),
            sensitive_values=(SOURCE_URL, self._target_url),
        )
        await self._supervisor.start()

    async def stop(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.stop()
        self._state = OutputState.STOPPED


def build_controller(*, ffmpeg: str, ffprobe: str, standby_after_seconds: float) -> Controller:
    adapters = (
        LocalRtmpDestinationAdapter(
            identity="douyin",
            destination=DestinationKind.DOUYIN,
            target_url=TARGET_URLS["douyin"],
            ffmpeg=ffmpeg,
        ),
        LocalRtmpDestinationAdapter(
            identity="wechat",
            destination=DestinationKind.WECHAT,
            target_url=TARGET_URLS["wechat"],
            ffmpeg=ffmpeg,
        ),
    )
    destinations = tuple(
        ConfiguredDestination(adapter.identity, adapter, enabled=True) for adapter in adapters
    )
    return Controller(
        source=ConfiguredSource(ROOM_IDENTITY),
        resolver=LocalSourceResolver(),
        media_probe=LocalMediaProbe(ffprobe),
        destinations=destinations,
        standby=StandbyMedia("local-generated-slate"),
        source_failure_standby_after=standby_after_seconds,
        standby_recovery_interval=3.0,
        minimum_url_validity=1.0,
        monitor_interval=1.0,
    )


class SourcePublisher:
    def __init__(self, ffmpeg: str) -> None:
        self._ffmpeg = ffmpeg
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        await self.stop()
        self._process = await asyncio.create_subprocess_exec(
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=48000",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-profile:v",
            "main",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-f",
            "flv",
            SOURCE_URL,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            process.kill()
            await process.wait()


def _select_streams(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise HarnessError("ffprobe returned no streams")
    video = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"),
        None,
    )
    if video is None or audio is None:
        raise HarnessError("ffprobe did not find both audio and video")
    return video, audio


async def _ffprobe(executable: str, url: str) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        executable,
        "-v",
        "error",
        "-rw_timeout",
        "3000000",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,pix_fmt,sample_rate,channels,profile,level",
        "-of",
        "json",
        url,
        stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=8.0)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise HarnessError("ffprobe timed out") from None
    if process.returncode != 0:
        raise HarnessError("ffprobe could not inspect the stream")
    try:
        payload = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HarnessError("ffprobe returned malformed JSON") from None
    if not isinstance(payload, dict):
        raise HarnessError("ffprobe returned an invalid document")
    return cast(dict[str, Any], payload)


async def probe_target(ffprobe: str, target: str) -> dict[str, Any]:
    payload = await _ffprobe(ffprobe, TARGET_URLS[target])
    video, audio = _select_streams(payload)
    return {
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
    }


def safe_snapshot(label: str, snapshot: ControllerSnapshot) -> dict[str, Any]:
    return {
        "label": label,
        "room_identity": snapshot.room_identity,
        "desired_running": snapshot.desired_running,
        "source_state": snapshot.source_state.value,
        "source_failure": snapshot.source_failure.value if snapshot.source_failure else None,
        "recovery_successes": snapshot.recovery_successes,
        "outputs": [
            {
                "identity": output.identity,
                "destination": output.destination.value,
                "enabled": output.enabled,
                "state": output.state.value,
                "input": output.input.value,
                "last_error": output.last_error.value if output.last_error else None,
            }
            for output in snapshot.outputs
        ],
    }


async def wait_for(
    label: str,
    predicate: Callable[[], Awaitable[Any]],
    *,
    timeout: float,
) -> Any:
    deadline = monotonic() + timeout
    last_error: Exception | None = None
    while monotonic() < deadline:
        try:
            result = await predicate()
            if result:
                return result
        except Exception as error:  # noqa: BLE001 - transient local readiness failures
            last_error = error
        await asyncio.sleep(0.25)
    suffix = f" ({type(last_error).__name__})" if last_error is not None else ""
    raise HarnessError(f"condition timed out: {label}{suffix}")


async def _snapshot_when(
    controller: Controller,
    predicate: Callable[[ControllerSnapshot], bool],
) -> ControllerSnapshot | None:
    snapshot = await controller.snapshot()
    return snapshot if predicate(snapshot) else None


async def _target_probe_when(
    ffprobe: str,
    expected_size: tuple[int, int],
    targets: Sequence[str] = ("douyin", "wechat"),
) -> dict[str, dict[str, Any]] | None:
    if not all(await asyncio.gather(*(path_ready(f"target/{item}") for item in targets))):
        return None
    probes = {item: await probe_target(ffprobe, item) for item in targets}
    if not all(
        probe["video_codec"] == "h264"
        and probe["audio_codec"] == "aac"
        and (probe["width"], probe["height"]) == expected_size
        for probe in probes.values()
    ):
        return None
    return probes


async def run_scenario(
    *,
    ffmpeg: str,
    ffprobe: str,
    standby_after_seconds: float,
) -> dict[str, Any]:
    controller = build_controller(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        standby_after_seconds=standby_after_seconds,
    )
    publisher = SourcePublisher(ffmpeg)
    checks: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    target_probes: list[dict[str, Any]] = []
    try:
        await publisher.start()
        await wait_for("source publisher readiness", lambda: path_ready("source/main"), timeout=20)

        await controller.start()
        initial = await wait_for(
            "application initial dual-target live state",
            lambda: _snapshot_when(
                controller,
                lambda item: item.source_state is SourceState.LIVE
                and all(
                    output.enabled
                    and output.state is OutputState.LIVE
                    and output.input is OutputInput.LIVE
                    for output in item.outputs
                ),
            ),
            timeout=35,
        )
        initial_probe = await wait_for(
            "both initial target probes",
            lambda: _target_probe_when(ffprobe, (640, 360)),
            timeout=35,
        )
        snapshots.append(safe_snapshot("initial_dual_target", initial))
        target_probes.append({"label": "initial_dual_target", "targets": initial_probe})
        checks.append({"name": "initial_dual_target", "passed": True})

        await controller.set_destination_enabled("wechat", False)
        isolated = await wait_for(
            "application target B disabled state",
            lambda: _snapshot_when(
                controller,
                lambda item: next(output for output in item.outputs if output.identity == "wechat")
                .state
                is OutputState.DISABLED,
            ),
            timeout=15,
        )
        await wait_for(
            "target B stopped by application",
            lambda: _target_b_stopped(ffprobe),
            timeout=20,
        )
        snapshots.append(safe_snapshot("target_b_disabled", isolated))
        target_probes.append(
            {
                "label": "target_b_disabled",
                "targets": {
                    "douyin": await probe_target(ffprobe, "douyin"),
                    "wechat": {"ready": False},
                },
            }
        )
        checks.append({"name": "target_isolation", "passed": True})

        await controller.set_destination_enabled("wechat", True)
        restored = await wait_for(
            "application target B re-enabled state",
            lambda: _snapshot_when(
                controller,
                lambda item: all(
                    output.enabled
                    and output.state is OutputState.LIVE
                    and output.input is OutputInput.LIVE
                    for output in item.outputs
                ),
            ),
            timeout=35,
        )
        await wait_for(
            "both targets after application re-enable",
            lambda: _target_probe_when(ffprobe, (640, 360)),
            timeout=35,
        )
        snapshots.append(safe_snapshot("target_b_reenabled", restored))

        await publisher.stop()
        loss_started = monotonic()
        standby = await wait_for(
            "application standby after continuous source loss",
            lambda: _snapshot_when(
                controller,
                lambda item: item.source_state is SourceState.STANDBY
                and all(
                    output.enabled
                    and output.state is OutputState.LIVE
                    and output.input is OutputInput.STANDBY
                    for output in item.outputs
                ),
            ),
            timeout=standby_after_seconds + 35,
        )
        outage_seconds = monotonic() - loss_started
        if outage_seconds <= 60.0:
            raise HarnessError("standby was entered before a source outage longer than 60 seconds")
        standby_probe = await wait_for(
            "both standby target probes",
            lambda: _target_probe_when(ffprobe, (320, 180)),
            timeout=35,
        )
        snapshots.append(safe_snapshot("standby_after_source_loss", standby))
        target_probes.append({"label": "standby_after_source_loss", "targets": standby_probe})
        checks.append(
            {
                "name": "standby_after_source_loss",
                "passed": True,
                "outage_seconds": round(outage_seconds, 3),
            }
        )

        await publisher.start()
        await wait_for("recovered source readiness", lambda: path_ready("source/main"), timeout=20)
        first_recovery = await wait_for(
            "first application recovery probe",
            lambda: _snapshot_when(
                controller,
                lambda item: item.source_state is SourceState.STANDBY
                and item.recovery_successes == 1,
            ),
            timeout=40,
        )
        first_probe = await wait_for(
            "targets remain on standby after first recovery probe",
            lambda: _target_probe_when(ffprobe, (320, 180)),
            timeout=15,
        )
        snapshots.append(safe_snapshot("recovery_probe_1", first_recovery))
        target_probes.append({"label": "recovery_probe_1", "targets": first_probe})
        checks.append({"name": "recovery_probe_1", "passed": True})

        recovered = await wait_for(
            "second application recovery probe returns live",
            lambda: _snapshot_when(
                controller,
                lambda item: item.source_state is SourceState.LIVE
                and all(
                    output.enabled
                    and output.state is OutputState.LIVE
                    and output.input is OutputInput.LIVE
                    for output in item.outputs
                ),
            ),
            timeout=20,
        )
        recovered_probe = await wait_for(
            "both recovered live target probes",
            lambda: _target_probe_when(ffprobe, (640, 360)),
            timeout=35,
        )
        snapshots.append(safe_snapshot("recovery_probe_2", recovered))
        target_probes.append({"label": "recovery_probe_2", "targets": recovered_probe})
        checks.append({"name": "recovery_probe_2", "passed": True})
        return {
            "checks": checks,
            "application_snapshots": snapshots,
            "target_probes": target_probes,
        }
    finally:
        await controller.shutdown()
        await publisher.stop()


async def _target_b_stopped(ffprobe: str) -> bool:
    if await path_ready("target/wechat"):
        return False
    probe = await _target_probe_when(ffprobe, (640, 360), ("douyin",))
    return probe is not None


def _safe_error(error: BaseException) -> str:
    message = str(error) or type(error).__name__
    message = re.sub(r"rtmps?://\S+", "[local-stream]", message)
    message = message.replace(str(ROOT), "[workspace]")
    return message[:300]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--standby-after-seconds", type=float, default=61.0)
    parser.add_argument("--overall-timeout-seconds", type=float, default=180.0)
    args = parser.parse_args(argv)
    if not 60.0 < args.standby_after_seconds <= 120.0:
        parser.error("--standby-after-seconds must be greater than 60 and at most 120")
    if not 120.0 <= args.overall_timeout_seconds <= 600.0:
        parser.error("--overall-timeout-seconds must be between 120 and 600")
    return args


async def async_main(args: argparse.Namespace) -> int:
    started_at = datetime.now(UTC)
    started_timer = monotonic()
    result: dict[str, Any] = {
        "schema_version": 2,
        "status": "failed",
        "started_at_utc": started_at.isoformat(),
        "paths": ["source/main", "target/douyin", "target/wechat"],
        "checks": [],
        "application_snapshots": [],
        "target_probes": [],
        "error": None,
    }
    exit_code = 1
    try:
        scenario = await asyncio.wait_for(
            run_scenario(
                ffmpeg=args.ffmpeg,
                ffprobe=args.ffprobe,
                standby_after_seconds=args.standby_after_seconds,
            ),
            timeout=args.overall_timeout_seconds,
        )
        result.update(scenario)
        result["status"] = "passed"
        exit_code = 0
    except BaseException as error:  # noqa: BLE001 - always persist acceptance evidence
        result["error"] = _safe_error(error)
    finally:
        result["completed_at_utc"] = datetime.now(UTC).isoformat()
        result["duration_seconds"] = round(monotonic() - started_timer, 3)
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
