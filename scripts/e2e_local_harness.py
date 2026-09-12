"""Production-path local RTMP acceptance harness.

Only the synthetic publisher and StreamGet resolver replacement are fixtures.
Runtime assembly, persistence, probing, command building, DNS validation, and
both output supervisors use production implementations.
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
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from restream_studio.config import AppPaths
from restream_studio.domain import (
    DestinationKind,
    MediaProbe,
    OutputState,
    ResolvedStream,
    SourceState,
)
from restream_studio.media.ffprobe import probe_media
from restream_studio.orchestration import ControllerSnapshot, OutputInput
from restream_studio.persistence import Database
from restream_studio.runtime import RuntimeManager
from restream_studio.security import redact
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
    """Minimal StreamGet replacement mapping the configured room to loopback."""

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


def _encrypt_ephemeral(value: str) -> str:
    return f"local-e2e:{value}"


def _decrypt_ephemeral(value: str) -> str:
    prefix = "local-e2e:"
    if not value.startswith(prefix):
        raise ValueError("invalid ephemeral secret")
    return value[len(prefix) :]


def build_runtime(
    *, runtime_dir: Path, ffmpeg: str, ffprobe: str
) -> tuple[RuntimeManager, Database]:
    runtime_dir = runtime_dir.resolve(strict=True)
    database_file = runtime_dir / "restream-studio-e2e.sqlite3"
    paths = AppPaths(
        base_dir=runtime_dir,
        data_dir=runtime_dir,
        log_dir=runtime_dir,
        standby_dir=runtime_dir,
        database_file=database_file,
    )
    database = Database(
        paths.database_file,
        paths=paths,
        encrypt_secret=_encrypt_ephemeral,
        decrypt_secret=_decrypt_ephemeral,
    ).open()
    database.set_source(ROOM_IDENTITY, None, False)
    for kind, identity in (
        (DestinationKind.DOUYIN, "douyin"),
        (DestinationKind.WECHAT, "wechat"),
    ):
        database.set_destination(
            kind,
            "rtmp://127.0.0.1:1935/target",
            identity,
            controller_identity=identity,
        )
    return (
        RuntimeManager(
            database,
            resolver_factory=LocalSourceResolver,
            ffmpeg_executable=ffmpeg,
            ffprobe_executable=ffprobe,
            allow_local_test=True,
        ),
        database,
    )


class SourcePublisher:
    """Synthetic input fixture; output processes remain production-owned."""

    def __init__(self, ffmpeg: str) -> None:
        self._ffmpeg = ffmpeg
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        await self.stop()
        self._process = await asyncio.create_subprocess_exec(
            self._ffmpeg,
            "-hide_banner", "-loglevel", "warning", "-nostdin", "-re",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-profile:v", "main", "-pix_fmt", "yuv420p", "-r", "30",
            "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-f", "flv", SOURCE_URL,
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


def _probe_evidence(probe: MediaProbe, decision: str) -> dict[str, Any]:
    return {
        "video_codec": probe.video_codec,
        "audio_codec": probe.audio_codec,
        "width": probe.width,
        "height": probe.height,
        "frame_rate": probe.frame_rate,
        "pixel_format": probe.pixel_format,
        "audio_sample_rate": probe.audio_sample_rate,
        "audio_channels": probe.audio_channels,
        "video_profile": probe.video_profile,
        "video_level": probe.video_level,
        "video_bitrate": probe.video_bitrate,
        "gop_seconds": probe.gop_seconds,
        "decision": decision,
    }


async def probe_target(ffprobe: str, target: str) -> dict[str, Any]:
    probe = await probe_media(TARGET_URLS[target], executable=ffprobe, allow_local_test=True)
    return {
        "video_codec": probe.video_codec,
        "audio_codec": probe.audio_codec,
        "width": probe.width,
        "height": probe.height,
        "frame_rate": probe.frame_rate,
    }


def safe_snapshot(label: str, snapshot: ControllerSnapshot) -> dict[str, Any]:
    state_counts = {state.value: 0 for state in OutputState}
    input_counts = {input_kind.value: 0 for input_kind in OutputInput}
    for output in snapshot.outputs:
        state_counts[output.state.value] += 1
        input_counts[output.input.value] += 1
    return {
        "label": label,
        "desired_running": snapshot.desired_running,
        "source_state": snapshot.source_state.value,
        "source_failure": snapshot.source_failure.value if snapshot.source_failure else None,
        "has_error": snapshot.error_detail is not None,
        "recovery_successes": snapshot.recovery_successes,
        "output_count": len(snapshot.outputs),
        "enabled_count": sum(output.enabled for output in snapshot.outputs),
        "output_state_counts": state_counts,
        "output_input_counts": input_counts,
        "output_error_count": sum(output.last_error is not None for output in snapshot.outputs),
    }


def _actual_command_decision(runtime: RuntimeManager) -> str:
    commands = [adapter._last_command for adapter in runtime._adapters.values()]
    if len(commands) != 2 or any(command is None for command in commands):
        raise HarnessError("production runtime did not prepare two output commands")
    modes = {
        "copy" if command is not None and "copy" in command.argv else "transcode"
        for command in commands
    }
    if len(modes) != 1:
        raise HarnessError("production output commands made inconsistent probe decisions")
    return modes.pop()


async def wait_for(
    label: str, predicate: Callable[[], Awaitable[Any]], *, timeout: float
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
    runtime: RuntimeManager, predicate: Callable[[ControllerSnapshot], bool]
) -> ControllerSnapshot | None:
    snapshot = await runtime.snapshot()
    return snapshot if predicate(snapshot) else None


async def _target_probe_when(
    ffprobe: str, targets: Sequence[str] = ("douyin", "wechat")
) -> dict[str, dict[str, Any]] | None:
    if not all(await asyncio.gather(*(path_ready(f"target/{item}") for item in targets))):
        return None
    probes = {item: await probe_target(ffprobe, item) for item in targets}
    valid = all(
        probe["video_codec"] == "h264"
        and probe["audio_codec"] == "aac"
        and (probe["width"], probe["height"]) == (640, 360)
        and probe["frame_rate"] > 0
        for probe in probes.values()
    )
    return probes if valid else None


async def _target_b_stopped(ffprobe: str) -> bool:
    if await path_ready("target/wechat"):
        return False
    return await _target_probe_when(ffprobe, ("douyin",)) is not None


async def run_scenario(
    *, ffmpeg: str, ffprobe: str, runtime_dir: Path, source_loss_seconds: float
) -> dict[str, Any]:
    runtime, database = build_runtime(runtime_dir=runtime_dir, ffmpeg=ffmpeg, ffprobe=ffprobe)
    publisher = SourcePublisher(ffmpeg)
    checks: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    target_probes: list[dict[str, Any]] = []
    try:
        await runtime.initialize()
        await publisher.start()
        await wait_for("source publisher readiness", lambda: path_ready("source/main"), timeout=20)
        source_probe = await probe_media(SOURCE_URL, executable=ffprobe, allow_local_test=True)

        await runtime.start()
        initial = await wait_for(
            "production runtime initial dual-target live state",
            lambda: _snapshot_when(
                runtime,
                lambda item: item.source_state is SourceState.LIVE
                and len(item.outputs) == 2
                and all(output.enabled and output.state is OutputState.LIVE
                        and output.input is OutputInput.LIVE for output in item.outputs),
            ),
            timeout=45,
        )
        initial_probe = await wait_for(
            "both initial target probes", lambda: _target_probe_when(ffprobe), timeout=45
        )
        decision = _actual_command_decision(runtime)
        snapshots.append(safe_snapshot("initial_dual_target", initial))
        target_probes.append({"label": "initial_dual_target", "targets": initial_probe})
        checks.append({"name": "initial_dual_target", "passed": True})

        await runtime.set_destination_enabled("wechat", False)
        isolated = await wait_for(
            "production runtime target B disabled state",
            lambda: _snapshot_when(
                runtime,
                lambda item: sum(output.state is OutputState.DISABLED for output in item.outputs) == 1,
            ),
            timeout=15,
        )
        await wait_for("target B stopped", lambda: _target_b_stopped(ffprobe), timeout=20)
        snapshots.append(safe_snapshot("target_b_disabled", isolated))
        target_probes.append({
            "label": "target_b_disabled",
            "targets": {"douyin": await probe_target(ffprobe, "douyin"), "wechat": {"ready": False}},
        })
        checks.append({"name": "target_isolation", "passed": True})

        await runtime.set_destination_enabled("wechat", True)
        restored = await wait_for(
            "production runtime target B re-enabled state",
            lambda: _snapshot_when(
                runtime,
                lambda item: len(item.outputs) == 2
                and all(output.enabled and output.state is OutputState.LIVE
                        and output.input is OutputInput.LIVE for output in item.outputs),
            ),
            timeout=45,
        )
        restored_probe = await wait_for(
            "both targets after re-enable", lambda: _target_probe_when(ffprobe), timeout=45
        )
        snapshots.append(safe_snapshot("target_b_reenabled", restored))
        target_probes.append({"label": "target_b_reenabled", "targets": restored_probe})

        await publisher.stop()
        reconnecting = await wait_for(
            "production runtime detects source loss",
            lambda: _snapshot_when(runtime, lambda item: item.source_state is SourceState.RECONNECTING),
            timeout=30,
        )
        await asyncio.sleep(source_loss_seconds)
        snapshots.append(safe_snapshot("source_loss", reconnecting))
        checks.append({"name": "source_loss_detected", "passed": True})

        await publisher.start()
        await wait_for("recovered source readiness", lambda: path_ready("source/main"), timeout=20)
        recovered = await wait_for(
            "production runtime recovered live outputs",
            lambda: _snapshot_when(
                runtime,
                lambda item: item.source_state is SourceState.LIVE
                and len(item.outputs) == 2
                and all(output.enabled and output.state is OutputState.LIVE
                        and output.input is OutputInput.LIVE for output in item.outputs),
            ),
            timeout=45,
        )
        recovered_probe = await wait_for(
            "both recovered target probes", lambda: _target_probe_when(ffprobe), timeout=45
        )
        snapshots.append(safe_snapshot("recovered_live", recovered))
        target_probes.append({"label": "recovered_live", "targets": recovered_probe})
        checks.append({"name": "recovered_live", "passed": True})
        return {
            "checks": checks,
            "application_snapshots": snapshots,
            "target_probes": target_probes,
            "source_probe": _probe_evidence(source_probe, decision),
        }
    finally:
        await runtime.shutdown()
        await publisher.stop()
        database.close()


def _safe_error(error: BaseException) -> str:
    message = str(error) or type(error).__name__
    safe = str(redact(message))
    safe = re.sub(r"(?i)\b(?:https?|rtmps?)://\S+", "[stream]", safe)
    safe = re.sub(re.escape(str(ROOT)), "[workspace]", safe, flags=re.IGNORECASE)
    return safe[:300]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--source-loss-seconds", type=float, default=3.0)
    parser.add_argument("--overall-timeout-seconds", type=float, default=180.0)
    args = parser.parse_args(argv)
    if not 1.0 <= args.source_loss_seconds <= 30.0:
        parser.error("--source-loss-seconds must be between 1 and 30")
    if not 120.0 <= args.overall_timeout_seconds <= 600.0:
        parser.error("--overall-timeout-seconds must be between 120 and 600")
    return args


async def async_main(args: argparse.Namespace) -> int:
    started_at = datetime.now(UTC)
    started_timer = monotonic()
    result: dict[str, Any] = {
        "schema_version": 3,
        "status": "failed",
        "started_at_utc": started_at.isoformat(),
        "paths": ["source/main", "target/douyin", "target/wechat"],
        "checks": [],
        "application_snapshots": [],
        "target_probes": [],
        "source_probe": {},
        "error": None,
    }
    exit_code = 1
    try:
        scenario = await asyncio.wait_for(
            run_scenario(
                ffmpeg=args.ffmpeg,
                ffprobe=args.ffprobe,
                runtime_dir=args.runtime_dir,
                source_loss_seconds=args.source_loss_seconds,
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
