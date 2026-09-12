from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "tools" / "mediamtx" / "mediamtx.yml"
SCRIPT = ROOT / "scripts" / "e2e-local.ps1"
RESULT = ROOT / "artifacts" / "e2e-local" / "result.json"


def test_local_rtmp_assets_encode_the_acceptance_contract() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8-sig")

    assert "api: yes" in config
    assert "apiAddress: 127.0.0.1:9997" in config
    assert "source/main:" in config
    assert "target/douyin:" in config
    assert "target/wechat:" in config
    assert config.count("source: publisher") == 3

    for required in (
        "Invoke-MediaMtxApi",
        "Invoke-Ffprobe",
        "Wait-Condition",
        "source/main",
        "target/douyin",
        "target/wechat",
        "60",
        "finally",
        "result.json",
        "ValidateRange(61",
        "initial_dual_target",
        "target_isolation",
        "standby_after_source_loss",
        "recovery_probe_1",
        "recovery_probe_2",
    ):
        assert required in script

    assert "Stop-Process -Name" not in script
    assert "taskkill /IM" not in script
    assert "Stop-Process -Id $Record.Id" in script
    assert "Start-Sleep -Seconds" not in script


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
    serialized = json.dumps(evidence)
    assert "rtmp://" not in serialized
    assert "stream_key" not in serialized.casefold()
    assert "pid" not in serialized.casefold()
