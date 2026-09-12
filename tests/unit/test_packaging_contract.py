from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_windows_packaging_files_exist() -> None:
    required = (
        "packaging/restream-studio.spec",
        "scripts/dev.ps1",
        "scripts/verify.ps1",
        "scripts/package.ps1",
        "README.md",
    )
    assert all((ROOT / item).is_file() for item in required)


def test_verify_gate_is_fail_fast_complete_and_checks_dynamic_health() -> None:
    script = _read("scripts/verify.ps1")
    assert "$ErrorActionPreference = 'Stop'" in script
    labels = (
        "PYTHON_LINT",
        "PYTHON_TYPES",
        "PYTHON_TESTS",
        "FRONTEND_DEPS",
        "FRONTEND_TYPES",
        "FRONTEND_LINT",
        "FRONTEND_TESTS",
        "FRONTEND_BUILD",
        "LOCAL_RTMP_E2E",
        "PYINSTALLER_PACKAGE",
        "PACKAGE_CONTENTS",
        "PACKAGE_SMOKE",
    )
    for label in labels:
        assert re.search(rf"Invoke-Gate\s+-Name\s+['\"]{label}['\"]", script)
    assert "TcpListener" in script
    assert "Invoke-RestMethod" in script
    assert "/health" in script
    assert "exit 1" in script


def test_package_builds_frontend_first_and_uses_directory_distribution() -> None:
    package_script = _read("scripts/package.ps1")
    assert package_script.index("npm ci") < package_script.index("npm run frontend:build")
    assert package_script.index("npm run frontend:build") < package_script.index("PyInstaller")
    assert "default-standby.mp4" in package_script
    assert "lavfi" in package_script
    assert "FFMPEG_PATH" in package_script
    assert "FFPROBE_PATH" in package_script

    spec = _read("packaging/restream-studio.spec")
    assert "frontend build output is missing" in spec
    assert "src/restream_studio/static" in spec.replace("\\", "/")
    assert "ffmpeg.exe" in spec and "ffprobe.exe" in spec
    assert "default-standby.mp4" in spec
    assert "licenses" in spec and "metadata" in spec
    assert "COLLECT(" in spec
    assert "onefile" not in spec.casefold()


def test_distribution_contract_excludes_runtime_and_secret_material() -> None:
    scripts = _read("scripts/package.ps1") + _read("scripts/verify.ps1")
    for forbidden in (
        "fixtures",
        ".env",
        ".sqlite3",
        "cookies",
        "stream-keys",
        ".git",
        "*.log",
    ):
        assert forbidden in scripts


def test_readme_distinguishes_local_and_platform_acceptance() -> None:
    readme = _read("README.md")
    for topic in (
        "LOCAL_CHAIN",
        "PLATFORM_ACCEPTANCE",
        "官方 RTMP",
        "local-test",
        "待机",
        "日志",
        "无秘密",
        "升级",
        "回滚",
        "卸载",
        "已知限制",
    ):
        assert topic in readme


def test_server_port_accepts_only_unprivileged_tcp_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    from restream_studio.main import _server_port

    monkeypatch.delenv("RESTREAM_STUDIO_PORT", raising=False)
    assert _server_port() == 8000
    monkeypatch.setenv("RESTREAM_STUDIO_PORT", "49152")
    assert _server_port() == 49152
    monkeypatch.setenv("RESTREAM_STUDIO_PORT", "0")
    with pytest.raises(ValueError, match="RESTREAM_STUDIO_PORT"):
        _server_port()
