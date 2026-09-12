from __future__ import annotations

import copy
import json
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
CHECKLIST = ROOT / "docs" / "acceptance" / "platform-checklist.md"
SCHEMA = ROOT / "docs" / "acceptance" / "platform-result.schema.json"
VALIDATOR = ROOT / "scripts" / "validate_platform_acceptance.py"


SCREENSHOT_STEPS = (
    "initial-douyin-preview",
    "initial-wechat-channels-preview",
    "douyin-stop-isolation",
    "wechat-channels-stop-isolation",
    "dual-platform-standby",
    "recovery-second-probe",
    "platform-end-state",
)


def _observation(captured_at_utc: str, *, status: str = "LIVE") -> dict[str, Any]:
    return {
        "status": status,
        "video_visible": True,
        "audio_audible": True,
        "observed_at_utc": captured_at_utc,
        "observed_by": "acceptance-verifier",
    }


def _accepted_result() -> dict[str, Any]:
    samples = []
    for minute in range(1, 31):
        target = {
            "status": "LIVE",
            "bitrate_kbps": 2_500,
            "dropped_frames": 0,
            "reconnect_count": 0,
            "platform_video_visible": True,
            "platform_audio_audible": True,
        }
        samples.append(
            {
                "minute": minute,
                "captured_at_utc": f"2026-09-12T00:{minute:02d}:00Z",
                "application_status": "RUNNING",
                "source_status": "LIVE",
                "douyin": copy.deepcopy(target),
                "wechat_channels": copy.deepcopy(target),
                "cpu_percent": 12.5,
                "memory_mib": 256.0,
                "recorded_by": "acceptance-verifier",
                "notes": "",
            }
        )

    return {
        "schema_version": 1,
        "run_id": "20260912T000000Z-platform-acceptance",
        "status": "PLATFORM_ACCEPTED",
        "started_at_utc": "2026-09-12T00:00:00Z",
        "completed_at_utc": "2026-09-12T00:40:00Z",
        "build": {
            "app_version": "0.1.0",
            "git_commit": "1234567",
            "windows_version": "Windows 11",
            "ffmpeg_version": "7.1",
            "mediamtx_version": "1.14.0",
        },
        "local_chain": {"verified": True, "evidence_sha256": "a" * 64},
        "credential_provenance": {
            "douyin": {
                "official_console": "抖音直播伴侣",
                "obtained_by_account_holder": True,
                "input_channel": "restream_studio_ui",
                "persisted_outside_app": False,
            },
            "wechat_channels": {
                "official_console": "视频号助手",
                "obtained_by_account_holder": True,
                "input_channel": "restream_studio_ui",
                "persisted_outside_app": False,
            },
        },
        "initial_preview": {
            "douyin": _observation("2026-09-12T00:00:05Z"),
            "wechat_channels": _observation("2026-09-12T00:00:06Z"),
        },
        "stop_isolation": [
            {
                "stopped_target": "douyin",
                "stopped_at_utc": "2026-09-12T00:00:10Z",
                "peer_remained_live": True,
                "peer_video_visible": True,
                "peer_audio_audible": True,
                "stopped_target_absent_in_preview": True,
                "resumed_at_utc": "2026-09-12T00:00:20Z",
                "observed_by": "acceptance-verifier",
            },
            {
                "stopped_target": "wechat_channels",
                "stopped_at_utc": "2026-09-12T00:00:30Z",
                "peer_remained_live": True,
                "peer_video_visible": True,
                "peer_audio_audible": True,
                "stopped_target_absent_in_preview": True,
                "resumed_at_utc": "2026-09-12T00:00:40Z",
                "observed_by": "acceptance-verifier",
            },
        ],
        "minute_samples": samples,
        "source_interruption": {
            "started_at_utc": "2026-09-12T00:31:00Z",
            "restored_at_utc": "2026-09-12T00:32:05Z",
            "duration_seconds": 65,
            "standby": {
                "douyin": _observation("2026-09-12T00:32:01Z", status="STANDBY"),
                "wechat_channels": _observation(
                    "2026-09-12T00:32:02Z", status="STANDBY"
                ),
            },
            "consecutive_probes": 2,
            "recovery_probes": [
                {
                    "captured_at_utc": "2026-09-12T00:32:10Z",
                    "source_live": True,
                    "targets": {
                        "douyin": _observation("2026-09-12T00:32:10Z"),
                        "wechat_channels": _observation("2026-09-12T00:32:10Z"),
                    },
                },
                {
                    "captured_at_utc": "2026-09-12T00:32:20Z",
                    "source_live": True,
                    "targets": {
                        "douyin": _observation("2026-09-12T00:32:20Z"),
                        "wechat_channels": _observation("2026-09-12T00:32:20Z"),
                    },
                },
            ],
        },
        "screenshots": [
            {
                "step": step,
                "relative_path": f"screenshots/{step}.png",
                "sha256": f"{index:x}" * 64,
                "captured_at_utc": captured_at_utc,
                "redacted": True,
                "reviewed_by": "redaction-reviewer",
            }
            for index, (step, captured_at_utc) in enumerate(
                zip(
                    SCREENSHOT_STEPS,
                    (
                        "2026-09-12T00:00:07Z",
                        "2026-09-12T00:00:08Z",
                        "2026-09-12T00:00:15Z",
                        "2026-09-12T00:00:35Z",
                        "2026-09-12T00:32:03Z",
                        "2026-09-12T00:32:20Z",
                        "2026-09-12T00:38:00Z",
                    ),
                    strict=True,
                ),
                start=1,
            )
        ],
        "signoff": {
            "account_holder": {
                "name": "account-holder",
                "signed": True,
                "signed_at_utc": "2026-09-12T00:39:00Z",
            },
            "acceptance_verifier": {
                "name": "acceptance-verifier",
                "signed": True,
                "signed_at_utc": "2026-09-12T00:39:30Z",
            },
        },
        "rollback": {
            "backup_reference": "backup-manifest.txt#a1",
            "old_program_reference": "release-manifest.txt#v0.0.9",
            "outputs_stopped": True,
            "platform_sessions_ended": True,
            "credentials_removed_or_rotated": True,
            "rollback_path_verified": True,
            "verified_at_utc": "2026-09-12T00:39:45Z",
        },
        "failures": [],
    }


def _run_validator(result: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    test_directory = ROOT / ".review-tmp-task14"
    test_directory.mkdir(exist_ok=True)
    result_path = test_directory / f"result-{uuid.uuid4().hex}.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    try:
        return subprocess.run(
            [sys.executable, str(VALIDATOR), str(result_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        result_path.unlink(missing_ok=True)


def test_platform_acceptance_positive_contract_is_executable() -> None:
    assert SCHEMA.is_file()
    assert VALIDATOR.is_file()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    completed = _run_validator(_accepted_result())

    assert completed.returncode == 0
    assert completed.stdout.strip() == "PLATFORM_ACCEPTED"
    assert completed.stderr == ""


def test_platform_acceptance_accepts_inclusive_time_and_minute_interval_boundaries() -> None:
    result = _accepted_result()
    result["started_at_utc"] = "2026-09-12T00:00:05Z"
    result["completed_at_utc"] = "2026-09-12T00:39:45Z"
    result["minute_samples"][1]["captured_at_utc"] = "2026-09-12T00:01:45Z"

    completed = _run_validator(result)

    assert completed.returncode == 0
    assert completed.stdout.strip() == "PLATFORM_ACCEPTED"
    assert completed.stderr == ""


def test_platform_acceptance_accepts_recovery_observations_at_probe_boundary() -> None:
    result = _accepted_result()
    probes = result["source_interruption"]["recovery_probes"]
    for probe in probes:
        for target in ("douyin", "wechat_channels"):
            probe["targets"][target]["observed_at_utc"] = probe["captured_at_utc"]

    completed = _run_validator(result)

    assert completed.returncode == 0
    assert completed.stdout.strip() == "PLATFORM_ACCEPTED"
    assert completed.stderr == ""


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (
            lambda result: result["source_interruption"]["recovery_probes"][1]["targets"]
            ["douyin"].update(observed_at_utc="2026-09-12T00:32:04Z"),
            "recovery_probes[1].targets.douyin.observed_at_utc",
        ),
        (
            lambda result: result["screenshots"][6].update(
                sha256=result["screenshots"][0]["sha256"]
            ),
            "screenshots[6].sha256",
        ),
        (
            lambda result: result["minute_samples"][14].update(application_status="ERROR"),
            "minute_samples[14].application_status",
        ),
    ],
)
def test_platform_acceptance_rejects_final_semantic_false_positives(
    mutate: Callable[[dict[str, Any]], object],
    expected_error: str,
) -> None:
    result = _accepted_result()
    mutate(result)

    completed = _run_validator(result)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert expected_error in completed.stderr


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (lambda result: result["local_chain"].update(verified=False), "local_chain.verified"),
        (
            lambda result: result["initial_preview"]["douyin"].update(audio_audible=False),
            "initial_preview.douyin.audio_audible",
        ),
        (
            lambda result: result["stop_isolation"][0].update(peer_video_visible=False),
            "stop_isolation",
        ),
        (
            lambda result: result["minute_samples"].__setitem__(
                29, copy.deepcopy(result["minute_samples"][28])
            ),
            "minute_samples",
        ),
        (
            lambda result: result["minute_samples"][5]["wechat_channels"].update(
                platform_video_visible=False
            ),
            "minute_samples",
        ),
        (
            lambda result: result["minute_samples"][4]["douyin"].update(bitrate_kbps=None),
            "bitrate_kbps",
        ),
        (
            lambda result: result["source_interruption"].update(duration_seconds=60),
            "duration_seconds",
        ),
        (
            lambda result: result["source_interruption"]["standby"]["wechat_channels"].update(
                audio_audible=False
            ),
            "standby.wechat_channels.audio_audible",
        ),
        (
            lambda result: result["source_interruption"].update(consecutive_probes=1),
            "consecutive_probes",
        ),
        (
            lambda result: result["rollback"].update(platform_sessions_ended=False),
            "rollback.platform_sessions_ended",
        ),
        (
            lambda result: result["signoff"]["account_holder"].update(signed=False),
            "signoff.account_holder.signed",
        ),
        (lambda result: result.pop("rollback"), "rollback"),
    ],
)
def test_platform_acceptance_rejects_false_duplicate_or_missing_fields(
    mutate: Callable[[dict[str, Any]], object],
    expected_error: str,
) -> None:
    result = _accepted_result()
    mutate(result)

    completed = _run_validator(result)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert expected_error in completed.stderr


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (
            lambda result: result.update(started_at_utc="2026-09-12T00:00:06Z"),
            "started_at_utc",
        ),
        (
            lambda result: result.update(completed_at_utc="2026-09-12T00:39:44Z"),
            "completed_at_utc",
        ),
        (
            lambda result: result["source_interruption"]["recovery_probes"][0].update(
                captured_at_utc="2026-09-12T00:32:05Z"
            ),
            "recovery_probes[0]",
        ),
        (
            lambda result: result["source_interruption"]["recovery_probes"][1].update(
                captured_at_utc="2026-09-12T00:32:10Z"
            ),
            "recovery_probes[1]",
        ),
        (
            lambda result: result["minute_samples"][1].update(
                captured_at_utc="2026-09-12T00:01:44Z"
            ),
            "minute_samples[1].captured_at_utc",
        ),
        (
            lambda result: result["minute_samples"][1].update(
                captured_at_utc="2026-09-12T00:02:16Z"
            ),
            "minute_samples[1].captured_at_utc",
        ),
        (
            lambda result: result["minute_samples"][29].update(
                captured_at_utc="2026-09-12T00:29:59Z"
            ),
            "minute_samples",
        ),
        (
            lambda result: result["minute_samples"][2].update(source_status="STANDBY"),
            "minute_samples[2].source_status",
        ),
        (
            lambda result: result["minute_samples"][3]["douyin"].update(status="STANDBY"),
            "minute_samples[3].douyin.status",
        ),
        (
            lambda result: result["minute_samples"][4]["wechat_channels"].update(
                bitrate_kbps=0
            ),
            "minute_samples[4].wechat_channels.bitrate_kbps",
        ),
        (
            lambda result: result["minute_samples"][5]["douyin"].update(dropped_frames=1),
            "minute_samples[5].douyin.dropped_frames",
        ),
        (
            lambda result: result["minute_samples"][6]["douyin"].update(reconnect_count=1),
            "minute_samples[6].douyin.reconnect_count",
        ),
        (
            lambda result: result["initial_preview"]["douyin"].update(status="STANDBY"),
            "initial_preview.douyin.status",
        ),
        (
            lambda result: result["source_interruption"]["standby"]["douyin"].update(
                status="LIVE"
            ),
            "source_interruption.standby.douyin.status",
        ),
        (
            lambda result: result["source_interruption"]["recovery_probes"][1]["targets"][
                "wechat_channels"
            ].update(status="STANDBY"),
            "recovery_probes[1].targets.wechat_channels.status",
        ),
        (
            lambda result: result["screenshots"][6].update(step="dual-platform-standby"),
            "screenshots",
        ),
        (
            lambda result: result["screenshots"][6].update(
                relative_path=result["screenshots"][0]["relative_path"]
            ),
            "screenshots[6].relative_path",
        ),
        (
            lambda result: result["screenshots"][0].update(
                relative_path="screenshots/https" + "://redacted.invalid.png"
            ),
            "result",
        ),
        (
            lambda result: result["source_interruption"]["standby"]["douyin"].update(
                observed_at_utc="2026-09-12T00:40:01Z"
            ),
            "completed_at_utc",
        ),
    ],
)
def test_platform_acceptance_rejects_p1_evidence_gaps(
    mutate: Callable[[dict[str, Any]], object],
    expected_error: str,
) -> None:
    result = _accepted_result()
    mutate(result)

    completed = _run_validator(result)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert expected_error in completed.stderr


def test_validator_rejects_pending_status_and_does_not_echo_sensitive_value() -> None:
    result = _accepted_result()
    result["status"] = "LOCAL_CHAIN_VERIFIED_PLATFORM_ACCEPTANCE_PENDING"
    result["unexpected"] = "rtmp" + "://redacted.invalid/live/redacted"

    completed = _run_validator(result)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "redacted.invalid" not in completed.stderr


def test_readme_uses_composite_result_status_enum() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    expected_statuses = (
        "LOCAL_CHAIN_PENDING_PLATFORM_ACCEPTANCE_PENDING",
        "LOCAL_CHAIN_VERIFIED_PLATFORM_ACCEPTANCE_PENDING",
        "PLATFORM_ACCEPTED",
    )
    for status in expected_statuses:
        assert f"`{status}`" in readme
    assert "`LOCAL_CHAIN_VERIFIED`" not in readme
    assert "`PLATFORM_ACCEPTANCE_PENDING`" not in readme


def test_checklist_points_to_the_canonical_schema_and_validator() -> None:
    checklist = CHECKLIST.read_text(encoding="utf-8")
    assert "docs/acceptance/platform-result.schema.json" in checklist
    assert "scripts/validate_platform_acceptance.py" in checklist
    assert "不得创建或提交伪造的 passed artifact" in checklist

    tracked_acceptance_results = subprocess.run(
        ["git", "ls-files", "artifacts/**/result.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert tracked_acceptance_results.stdout.strip() == ""
