from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path
from time import monotonic, sleep

import pytest

from restream_studio.persistence import Database
from restream_studio.runtime import RuntimeManager

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "tools" / "mediamtx" / "mediamtx.yml"
SCRIPT = ROOT / "scripts" / "e2e-local.ps1"
HARNESS = ROOT / "scripts" / "e2e_local_harness.py"
RESULT = ROOT / "artifacts" / "e2e-local" / "result.json"


def test_local_rtmp_assets_delegate_application_orchestration_to_python() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8-sig")

    assert "api: yes" in config
    assert "apiAddress: 127.0.0.1:9997" in config
    assert "source/main:" in config
    assert "target/douyin:" in config
    assert "target/wechat:" in config
    assert config.count("source: publisher") == 3
    assert "moq: no" in config

    assert "e2e_local_harness.py" in script
    assert "Start-LiveTarget" not in script
    assert "Start-StandbyTarget" not in script
    assert "target-$Target" not in script
    assert "required tool is unavailable: mediamtx" in script
    assert "Stop-Process -Name" not in script
    assert "taskkill /IM" not in script
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in script
    assert "AssignProcessToJobObject" in script
    assert "Assert-PortsReleased" in script
    assert "@($Ports | Where-Object" in script


def test_harness_uses_production_runtime_database_probe_and_command_paths() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    assert "RuntimeManager" in source
    assert "Database" in source
    assert "probe_media" in source
    assert "LocalMediaProbe" not in source
    assert "LocalRtmpDestinationAdapter" not in source
    assert "ConfiguredDestination" not in source
    assert "build_ffmpeg_command" not in source


def test_harness_runtime_factory_returns_production_objects() -> None:
    import scripts.e2e_local_harness as harness

    runtime_dir = ROOT / "artifacts" / "pytest-runtime-factory"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    database_file = runtime_dir / "restream-studio-e2e.sqlite3"
    database_file.unlink(missing_ok=True)
    runtime, database = harness.build_runtime(
        runtime_dir=runtime_dir,
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
    )
    try:
        assert isinstance(runtime, RuntimeManager)
        assert isinstance(database, Database)
    finally:
        database.close()
        database_file.unlink(missing_ok=True)


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
    process = subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=240)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        _wait_for_ports_released((1935, 9997), timeout=10.0)
        pytest.fail(f"local RTMP acceptance timed out\n{stdout}{stderr}")
    completed = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    assert completed.returncode == 0, completed.stdout + completed.stderr

    evidence = json.loads(RESULT.read_text(encoding="utf-8"))
    assert evidence["status"] == "passed"
    assert evidence["paths"] == ["source/main", "target/douyin", "target/wechat"]
    assert [check["name"] for check in evidence["checks"]] == [
        "initial_dual_target",
        "target_isolation",
        "source_loss_detected",
        "recovered_live",
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
    assert "room_identity" not in serialized.casefold()
    assert evidence["source_probe"]["decision"] in {"copy", "transcode"}
    assert evidence["source_probe"]["frame_rate"] > 0


def _wait_for_ports_released(ports: tuple[int, ...], *, timeout: float) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if all(_port_available(port) for port in ports):
            return
        sleep(0.1)
    pytest.fail(f"local ports were not released: {ports}")


def _port_available(port: int) -> bool:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        listener.close()
    return True
