from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from collections.abc import Coroutine
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from restream_studio.domain import DestinationKind, MediaProbe
from restream_studio.orchestration import ConfiguredDestination, Controller
from restream_studio.outputs import OutputSupervisor

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "tools" / "mediamtx" / "mediamtx.yml"
SCRIPT = ROOT / "scripts" / "e2e-local.ps1"
HARNESS = ROOT / "scripts" / "e2e_local_harness.py"
RESULT = ROOT / "artifacts" / "e2e-local" / "result.json"


def load_harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e_local_harness", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def drive[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Drive fake-only coroutines without constructing the blocked host event loop."""
    try:
        while True:
            coroutine.send(None)
    except StopIteration as stopped:
        return cast(T, stopped.value)


def test_local_rtmp_assets_delegate_application_orchestration_to_python() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8-sig")

    assert "api: yes" in config
    assert "apiAddress: 127.0.0.1:9997" in config
    assert "source/main:" in config
    assert "target/douyin:" in config
    assert "target/wechat:" in config
    assert config.count("source: publisher") == 3

    assert "e2e_local_harness.py" in script
    assert "Start-LiveTarget" not in script
    assert "Start-StandbyTarget" not in script
    assert "target-$Target" not in script
    assert "required tool is unavailable: mediamtx" in script
    assert "Stop-Process -Name" not in script
    assert "taskkill /IM" not in script


def test_harness_imports_real_controller_and_supervisor_types() -> None:
    harness = load_harness()

    assert harness.Controller is Controller
    assert harness.ConfiguredDestination is ConfiguredDestination
    assert harness.OutputSupervisor is OutputSupervisor


def test_harness_destination_adapter_delegates_process_lifecycle_to_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = load_harness()
    calls: list[tuple[DestinationKind, tuple[str, ...]]] = []

    class SupervisorSpy:
        def __init__(
            self,
            destination: DestinationKind,
            argv: tuple[str, ...],
            **_: object,
        ) -> None:
            calls.append((destination, argv))
            self.state = harness.OutputState.STOPPED

        async def start(self) -> None:
            self.state = harness.OutputState.LIVE

        async def stop(self) -> None:
            self.state = harness.OutputState.STOPPED

    monkeypatch.setattr(harness, "OutputSupervisor", SupervisorSpy)
    adapter = harness.LocalRtmpDestinationAdapter(
        identity="douyin",
        destination=DestinationKind.DOUYIN,
        target_url="rtmp://127.0.0.1:1935/target/douyin",
        ffmpeg="ffmpeg",
    )
    probe = MediaProbe("h264", "aac", 640, 360, 30.0)

    drive(adapter.restart(adapter.prepare_live("rtmp://127.0.0.1:1935/source/main", probe)))
    drive(adapter.stop())

    assert len(calls) == 1
    assert calls[0][0] is DestinationKind.DOUYIN
    assert calls[0][1][0] == "ffmpeg"
    assert "rtmp://127.0.0.1:1935/target/douyin" in calls[0][1]


def test_harness_builds_real_controller_with_two_configured_destinations() -> None:
    harness = load_harness()
    controller = harness.build_controller(
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        standby_after_seconds=61.0,
    )

    assert isinstance(controller, Controller)
    configured = controller._destinations
    assert len(configured) == 2
    assert all(isinstance(item, ConfiguredDestination) for item in configured)
    assert [item.identity for item in configured] == ["douyin", "wechat"]
    assert [item.supervisor.destination for item in configured] == [
        DestinationKind.DOUYIN,
        DestinationKind.WECHAT,
    ]


def test_artifacts_are_ignored() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "artifacts/e2e-local/result.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0


@pytest.mark.skipif(
    os.environ.get("RUN_LOCAL_RTMP_E2E") != "1",
    reason="set RUN_LOCAL_RTMP_E2E=1 to run the 60-second local RTMP acceptance",
)
def test_dual_target_restream_recovers_end_to_end() -> None:
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    evidence = json.loads(RESULT.read_text(encoding="utf-8"))
    assert evidence["status"] == "passed"
    assert evidence["paths"] == ["source/main", "target/douyin", "target/wechat"]
    assert [check["name"] for check in evidence["checks"]] == [
        "initial_dual_target",
        "target_isolation",
        "standby_after_source_loss",
        "recovery_probe_1",
        "recovery_probe_2",
    ]
    assert all(check["passed"] is True for check in evidence["checks"])
    assert len(evidence["application_snapshots"]) >= 5
    assert all(
        set(probe["targets"]) == {"douyin", "wechat"}
        for probe in evidence["target_probes"]
        if probe["label"] != "target_b_disabled"
    )
    serialized = json.dumps(evidence)
    assert "rtmp://" not in serialized
    assert "stream_key" not in serialized.casefold()
    assert "pid" not in serialized.casefold()
