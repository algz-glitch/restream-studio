from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
CHECKLIST = ROOT / "docs" / "acceptance" / "platform-checklist.md"


def test_real_platform_acceptance_checklist_is_fail_closed_and_auditable() -> None:
    assert CHECKLIST.is_file()

    checklist = CHECKLIST.read_text(encoding="utf-8")
    required_contract = (
        "账号持有人",
        "抖音直播伴侣",
        "视频号助手",
        "仅通过 Restream Studio UI 输入",
        "独立预览",
        "停止隔离",
        "30 分钟",
        "每分钟",
        "码率",
        "掉帧",
        "重连",
        "平台预览",
        "CPU",
        "内存",
        "> 60 秒",
        "连续两次探测",
        "截图脱敏",
        "人工签字",
        "UTC",
        "回滚",
        "result.json",
        "LOCAL_CHAIN_PENDING_PLATFORM_ACCEPTANCE_PENDING",
        "LOCAL_CHAIN_VERIFIED_PLATFORM_ACCEPTANCE_PENDING",
        "PLATFORM_ACCEPTED",
    )
    for item in required_contract:
        assert item in checklist

    assert '"minItems": 30' in checklist
    assert '"maxItems": 30' in checklist
    assert '"additionalProperties": false' in checklist
    assert "不得创建或提交伪造的 passed artifact" in checklist
    assert "当前仓库基线状态" in checklist
    assert "`LOCAL_CHAIN_PENDING_PLATFORM_ACCEPTANCE_PENDING`" in checklist
    assert [int(value) for value in re.findall(r"^\| (\d{2}) \|", checklist, re.MULTILINE)] == list(
        range(1, 31)
    )

    tracked_acceptance_results = subprocess.run(
        ["git", "ls-files", "artifacts/**/result.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert tracked_acceptance_results.stdout.strip() == ""


def test_embedded_result_schema_is_valid_json_and_closes_the_status_enum() -> None:
    checklist = CHECKLIST.read_text(encoding="utf-8")
    schema_match = re.search(r"```json\n(?P<schema>.+?)\n```", checklist, re.DOTALL)
    assert schema_match is not None

    schema = json.loads(schema_match.group("schema"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["status"]["enum"] == [
        "LOCAL_CHAIN_PENDING_PLATFORM_ACCEPTANCE_PENDING",
        "LOCAL_CHAIN_VERIFIED_PLATFORM_ACCEPTANCE_PENDING",
        "PLATFORM_ACCEPTED",
    ]
    minute_samples = schema["properties"]["minute_samples"]
    assert minute_samples["minItems"] == minute_samples["maxItems"] == 30
    recovery_probes = schema["properties"]["source_interruption"]["properties"][
        "recovery_probes"
    ]
    assert recovery_probes["minItems"] == recovery_probes["maxItems"] == 2
