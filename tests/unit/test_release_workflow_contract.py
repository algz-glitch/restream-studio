from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
WRITER = ROOT / "scripts" / "write-update-manifest.py"
PROVISIONER = ROOT / "scripts" / "provision-release-tools.ps1"


def _run_writer(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(WRITER), *arguments],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def _valid_arguments(installer: Path, output: Path) -> list[str]:
    return [
        "--version",
        "1.2.3",
        "--installer",
        str(installer),
        "--repository",
        "algz-glitch/restream-studio",
        "--tag",
        "v1.2.3",
        "--output",
        str(output),
    ]


def test_release_workflow_is_tag_only_windows_and_least_privilege() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"(?m)^on:\s*$", workflow)
    assert re.search(r"(?ms)^on:\s*\n\s+push:\s*\n\s+tags:\s*\n\s+- ['\"]v\*['\"]", workflow)
    assert "workflow_dispatch:" not in workflow
    assert "branches:" not in workflow
    assert re.search(r"(?ms)^permissions:\s*\n\s+contents:\s+write\s*$", workflow)
    assert "runs-on: windows-2022" in workflow


def test_release_workflow_pins_runtime_actions_and_locked_node_install() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for action in (
        "actions/checkout",
        "actions/setup-python",
        "actions/setup-node",
        "actions/upload-artifact",
    ):
        assert re.search(rf"uses:\s+{re.escape(action)}@[0-9a-f]{{40}}", workflow)
    assert "python-version: '3.12.10'" in workflow
    assert "cache-dependency-path: requirements-release.lock" in workflow
    assert "node-version: '22.14.0'" in workflow
    assert "cache: npm" in workflow
    assert "cache-dependency-path: package-lock.json" in workflow
    assert "npm ci --ignore-scripts --no-audit --no-fund" in workflow
    assert "--requirement requirements-release.lock" in workflow
    assert "--no-build-isolation --no-deps ." in workflow


def test_release_workflow_never_interpolates_context_inside_powershell() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    lines = workflow.splitlines()
    run_blocks: list[str] = []
    for index, line in enumerate(lines):
        if not line.startswith("        run:"):
            continue
        value = line.partition("run:")[2].strip()
        if value != "|":
            run_blocks.append(value)
            continue
        block: list[str] = []
        for candidate in lines[index + 1 :]:
            if candidate and not candidate.startswith("          "):
                break
            block.append(candidate)
        run_blocks.append("\n".join(block))
    assert run_blocks
    assert all("${{" not in block for block in run_blocks)
    assert "RELEASE_TAG: ${{ github.ref_name }}" in workflow
    assert "GH_REPOSITORY: ${{ github.repository }}" in workflow
    assert "$env:RELEASE_TAG" in workflow
    assert "^v(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)$" in workflow


def test_release_workflow_runs_all_gates_and_publishes_only_verified_assets() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    ordered = (
        "scripts/provision-release-tools.ps1",
        "scripts/verify.ps1",
        "scripts/build-installer.ps1 -Clean",
        "scripts/write-update-manifest.py",
        "actions/upload-artifact@",
        "gh release create",
    )
    positions = [workflow.index(marker) for marker in ordered]
    assert positions == sorted(positions)
    assert "RestreamStudio-Setup-$version.exe" in workflow
    assert "dist/latest.json" in workflow
    assert "if-no-files-found: error" in workflow
    assert "--verify-tag" in workflow
    assert "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in workflow
    assert "persist-credentials: false" in workflow


def test_release_workflow_derives_and_checks_exact_semver_tag() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "github.ref_name" in workflow
    assert "^v(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)$" in workflow
    assert "$version = $tag.Substring(1)" in workflow
    assert "pyproject.toml" in workflow
    assert "package.json" in workflow
    assert "packaging/restream-studio.iss" in workflow


def test_release_lock_contains_only_exact_transitive_pins() -> None:
    lock = (ROOT / "requirements-release.lock").read_text(encoding="utf-8")
    requirements = [
        line.strip()
        for line in lock.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert requirements
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s;]+", item) for item in requirements)
    names = {item.partition("==")[0].casefold() for item in requirements}
    for required in ("fastapi", "pydantic", "streamget", "pytest", "mypy", "ruff", "pyinstaller"):
        assert required in names


def test_release_tools_are_versioned_downloaded_and_sha256_verified() -> None:
    script = PROVISIONER.read_text(encoding="utf-8")
    assert "autobuild-2026-09-12-13-12" in script
    assert "ffmpeg-n8.1.2-52-g5a03dfa0f6-win64-gpl-8.1.zip" in script
    assert "8EBD7E82791B8F753ADE7FD6F2EACF8CE02127DFB1F25102D85154D1779CD2DE" in script
    assert "mediamtx_v1.21.0_windows_amd64.zip" in script
    assert "8A58A9B8C25EE99A96C23DC0A17F39ACE3072C01D2E148329073C64DDF83493D" in script
    assert "Get-FileHash" in script
    assert "-Algorithm SHA256" in script
    assert "Invoke-WebRequest" in script
    assert "Get-Command ffmpeg" not in script
    assert "Get-Command mediamtx" not in script
    assert "function Resolve-SingleFile" in script
    assert "Select-Object -Single" not in script
    for variable in ("FFMPEG_PATH", "FFPROBE_PATH", "MEDIAMTX_PATH"):
        assert variable in script
        assert "$env:GITHUB_ENV" in script


def test_manifest_writer_calculates_strict_schema_and_atomic_output(tmp_path: Path) -> None:
    installer = tmp_path / "RestreamStudio-Setup-1.2.3.exe"
    installer.write_bytes(b"installer-fixture\x00\xff")
    output = tmp_path / "latest.json"

    result = _run_writer(*_valid_arguments(installer, output), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert list(payload) == [
        "schema_version",
        "version",
        "installer_url",
        "sha256",
        "size",
        "published_at",
        "release_url",
    ]
    assert payload == {
        "schema_version": 1,
        "version": "1.2.3",
        "installer_url": (
            "https://github.com/algz-glitch/restream-studio/releases/download/"
            "v1.2.3/RestreamStudio-Setup-1.2.3.exe"
        ),
        "sha256": hashlib.sha256(installer.read_bytes()).hexdigest().upper(),
        "size": installer.stat().st_size,
        "published_at": payload["published_at"],
        "release_url": "https://github.com/algz-glitch/restream-studio/releases/tag/v1.2.3",
    }
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", payload["published_at"])
    assert not list(tmp_path.glob(".latest.json.*.tmp"))
    writer = WRITER.read_text(encoding="utf-8")
    assert "tempfile.NamedTemporaryFile(" in writer
    assert "os.fsync(" in writer
    assert "os.replace(" in writer


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("--version", "01.2.3", "canonical SemVer"),
        ("--version", "1.2.3-rc.1", "canonical SemVer"),
        ("--repository", "other/restream-studio", "repository"),
        ("--repository", "algz-glitch/restream-studio/extra", "repository"),
        ("--tag", "1.2.3", "tag"),
        ("--tag", "v1.2.4", "tag"),
    ],
)
def test_manifest_writer_rejects_non_exact_release_identity(
    tmp_path: Path, name: str, value: str, message: str
) -> None:
    installer = tmp_path / "RestreamStudio-Setup-1.2.3.exe"
    installer.write_bytes(b"fixture")
    output = tmp_path / "latest.json"
    arguments = _valid_arguments(installer, output)
    arguments[arguments.index(name) + 1] = value

    result = _run_writer(*arguments, cwd=tmp_path)

    assert result.returncode != 0
    assert message in result.stderr
    assert not output.exists()


def test_manifest_writer_rejects_unsafe_or_inexact_paths(tmp_path: Path) -> None:
    installer = tmp_path / "wrong.exe"
    installer.write_bytes(b"fixture")
    output = tmp_path / "latest.json"
    result = _run_writer(*_valid_arguments(installer, output), cwd=tmp_path)
    assert result.returncode != 0
    assert "installer filename" in result.stderr

    exact = tmp_path / "RestreamStudio-Setup-1.2.3.exe"
    exact.write_bytes(b"fixture")
    wrong_output = tmp_path / "manifest.json"
    result = _run_writer(*_valid_arguments(exact, wrong_output), cwd=tmp_path)
    assert result.returncode != 0
    assert "latest.json" in result.stderr

    missing_parent = tmp_path / "missing" / "latest.json"
    result = _run_writer(*_valid_arguments(exact, missing_parent), cwd=tmp_path)
    assert result.returncode != 0
    assert "output directory" in result.stderr


def test_manifest_writer_rejects_an_empty_installer(tmp_path: Path) -> None:
    installer = tmp_path / "RestreamStudio-Setup-1.2.3.exe"
    installer.touch()
    output = tmp_path / "latest.json"

    result = _run_writer(*_valid_arguments(installer, output), cwd=tmp_path)

    assert result.returncode != 0
    assert "installer must not be empty" in result.stderr
    assert not output.exists()


def test_manifest_writer_rejects_sparse_installer_above_hard_limit(tmp_path: Path) -> None:
    installer = tmp_path / "RestreamStudio-Setup-1.2.3.exe"
    with installer.open("wb") as stream:
        stream.truncate(512 * 1024 * 1024 + 1)
    output = tmp_path / "latest.json"

    result = _run_writer(*_valid_arguments(installer, output), cwd=tmp_path)

    assert result.returncode != 0
    assert "maximum installer size" in result.stderr
    assert not output.exists()


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="Windows PowerShell unavailable")
def test_installer_builder_derives_a_0_1_1_fixture_from_three_sources(tmp_path: Path) -> None:
    (tmp_path / "packaging").mkdir()
    (tmp_path / "packaging" / "restream-studio.iss").write_text(
        '#define MyAppVersion "0.1.1"\n', encoding="utf-8"
    )
    (tmp_path / "package.json").write_text('{"version":"0.1.1"}\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0.1.1"\n', encoding="utf-8"
    )

    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "build-installer.ps1"),
            "-SourceRoot",
            str(tmp_path),
            "-ResolveVersionOnly",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "RELEASE_VERSION=0.1.1" in result.stdout
    assert str(tmp_path / "dist" / "installer" / "RestreamStudio-Setup-0.1.1.exe") in result.stdout


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support unavailable")
def test_manifest_writer_refuses_symlink_output(tmp_path: Path) -> None:
    installer = tmp_path / "RestreamStudio-Setup-1.2.3.exe"
    installer.write_bytes(b"fixture")
    target = tmp_path / "target.json"
    target.write_text("do not replace", encoding="utf-8")
    output = tmp_path / "latest.json"
    try:
        output.symlink_to(target)
    except OSError:
        pytest.skip("creating symlinks requires unavailable Windows privilege")

    result = _run_writer(*_valid_arguments(installer, output), cwd=tmp_path)

    assert result.returncode != 0
    assert "symbolic link" in result.stderr
    assert target.read_text(encoding="utf-8") == "do not replace"
