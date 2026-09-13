from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_windows_packaging_files_exist() -> None:
    required = (
        "packaging/restream-studio.spec",
        "packaging/restream-studio.iss",
        "packaging/firewall-install.ps1",
        "packaging/firewall-remove.ps1",
        "packaging/Enable-Localhost.ps1",
        "packaging/Enable-Localhost.cmd",
        "scripts/dev.ps1",
        "scripts/verify.ps1",
        "scripts/package.ps1",
        "scripts/build-installer.ps1",
        "README.md",
    )
    assert all((ROOT / item).is_file() for item in required)


def test_inno_installer_has_stable_per_user_lifecycle_contract() -> None:
    installer = _read("packaging/restream-studio.iss")
    assert "AppId={{6D5EE4A8-17C8-4DA0-84F8-28710EAB90F4}" in installer
    assert "DefaultDirName={localappdata}\\Programs\\RestreamStudio" in installer
    assert "PrivilegesRequired=lowest" in installer
    assert "ArchitecturesAllowed=x64compatible" in installer
    assert "ArchitecturesInstallIn64BitMode=x64compatible" in installer
    assert "UninstallDisplayName=Restream Studio" in installer
    assert 'Name: "{autodesktop}\\Restream Studio"' in installer
    assert 'Name: "{group}\\Restream Studio"' in installer
    assert "RestreamStudio.exe" in installer
    assert "RestreamStudioUpdateHelper.exe" in installer
    assert "firewall-install.ps1" in installer
    assert "firewall-remove.ps1" in installer
    assert "[UninstallRun]" in installer
    assert "{localappdata}\\RestreamStudio" in installer
    assert "ShouldDeleteUserData" in installer
    assert "mbConfirmation" in installer
    assert "MB_DEFBUTTON2" in installer


def test_firewall_hooks_self_elevate_and_scope_both_loopback_ends_to_program() -> None:
    install = _read("packaging/firewall-install.ps1")
    remove = _read("packaging/firewall-remove.ps1")

    for script in (install, remove):
        assert "WindowsBuiltInRole]::Administrator" in script
        assert "-Verb RunAs" in script
        assert "RestreamStudio.exe" in script
        assert "127.0.0.1" in script
        assert "$application.Program -ieq $resolvedExecutable" in script
        assert "$address.LocalAddress -contains '127.0.0.1'" in script
        assert "$address.RemoteAddress -contains '127.0.0.1'" in script

    assert "New-NetFirewallRule" in install
    assert "-Program $resolvedExecutable" in install
    assert "-Direction Inbound" in install
    assert "-Protocol TCP" in install
    assert "-LocalAddress '127.0.0.1'" in install
    assert "-RemoteAddress '127.0.0.1'" in install
    assert "Remove-NetFirewallRule" in remove


def test_installer_build_contract_bundles_helper_and_uses_pinned_iscc_discovery() -> None:
    spec = _read("packaging/restream-studio.spec")
    package = _read("scripts/package.ps1")
    build = _read("scripts/build-installer.ps1")
    verify = _read("scripts/verify.ps1")

    assert '"packaging" / "update-helper-entry.py"' in spec
    assert 'name="RestreamStudioUpdateHelper"' in spec
    assert (
        "from restream_studio.update.helper import main"
        in _read("packaging/update-helper-entry.py")
    )
    assert "RestreamStudioUpdateHelper.exe" in package
    assert "RestreamStudioUpdateHelper.exe" in verify
    assert "ISCC_PATH" in build
    assert "G:\\Apps\\Inno\\ISCC.exe" in build
    assert "Get-Command 'ISCC.exe'" in build
    assert "winget" not in build.casefold()
    assert "RestreamStudio-Setup-0.1.0.exe" in build
    assert "Get-FileHash" in build
    assert "SHA256=" in build
    assert "Start-Process -FilePath $updateHelper" in build
    assert "$helperProcess.ExitCode -ne 2" in build


def test_packaged_loopback_setup_is_program_scoped_and_loopback_only() -> None:
    script = _read("packaging/Enable-Localhost.ps1")
    assert "WindowsBuiltInRole]::Administrator" in script
    assert "-Verb RunAs" in script
    assert "-Program $resolvedExecutable" in script
    assert "-Direction Inbound" in script
    assert "-Protocol TCP" in script
    assert "-LocalAddress '127.0.0.1'" in script
    assert "-RemoteAddress '127.0.0.1'" in script
    assert "-Profile Any" in script
    assert "RemoteAddress -notcontains '127.0.0.1'" in script


def test_npm_lock_is_complete_for_windows_x64() -> None:
    lock = json.loads(_read("package-lock.json"))
    packages = lock["packages"]
    workspace_paths = {"", *packages[""]["workspaces"]}

    incomplete = {
        path: [field for field in ("version", "resolved", "integrity") if not metadata.get(field)]
        for path, metadata in packages.items()
        if path not in workspace_paths and not metadata.get("link")
        if any(not metadata.get(field) for field in ("version", "resolved", "integrity"))
    }
    assert incomplete == {}

    for path, metadata in packages.items():
        if path in workspace_paths or metadata.get("link"):
            continue
        assert metadata["resolved"].startswith("https://registry.npmjs.org/")
        assert metadata["integrity"].startswith("sha512-")

    windows_runtime_packages = {
        "node_modules/@esbuild/win32-x64": ("win32", "x64"),
        "node_modules/@rollup/rollup-win32-x64-msvc": ("win32", "x64"),
    }
    for path, (operating_system, cpu) in windows_runtime_packages.items():
        metadata = packages[path]
        assert operating_system in metadata["os"]
        assert cpu in metadata["cpu"]
        assert metadata["optional"] is True


def test_root_test_script_does_not_repeat_vitest_run_flag() -> None:
    package = json.loads(_read("package.json"))
    assert package["scripts"]["test"] == "npm --prefix frontend run test"


def test_verify_gate_is_fail_fast_complete_and_checks_dynamic_health() -> None:
    script = _read("scripts/verify.ps1")
    assert "$ErrorActionPreference = 'Stop'" in script
    labels = (
        "PYTHON_LINT",
        "PYTHON_TYPES",
        "PYTHON_TESTS",
        "FRONTEND_TYPES",
        "FRONTEND_LINT",
        "FRONTEND_TESTS",
        "FRONTEND_BUILD",
        "LOCAL_RTMP_E2E",
        "PACKAGE_SMOKE",
    )
    invoked_labels = tuple(
        re.findall(r"Invoke-Gate\s+-Name\s+['\"]([A-Z0-9_]+)['\"]", script)
    )
    assert invoked_labels == labels
    assert "FRONTEND_DEPS=PASS" not in script
    assert "FRONTEND_DEPS=FAIL" not in script
    assert "FRONTEND_DEPS blocker:" in script
    frontend_types = script.index("Invoke-Gate -Name 'FRONTEND_TYPES'")
    frontend_lint = script.index("Invoke-Gate -Name 'FRONTEND_LINT'")
    assert frontend_types < script.index("npm dependency closure is not reproducible") < frontend_lint
    package_smoke = script.index("Invoke-Gate -Name 'PACKAGE_SMOKE'")
    assert package_smoke < script.index("& $Package -Clean")
    assert package_smoke < script.index("Assert-Distribution", package_smoke)
    assert package_smoke < script.index("Test-PackageHealth", package_smoke)
    assert "TcpListener" in script
    assert "Invoke-RestMethod" in script
    assert "/health" in script
    assert "restream-studio" in script
    assert "0.1.0" in script
    assert "Restream Studio" in script
    assert "WaitForExit" in script
    assert "Test-LocalPortAvailable" in script
    assert "packaged port was not released" in script
    assert "exit 1" in script


def test_package_smoke_disposes_process_once_after_stop_wait_and_port_release() -> None:
    script = _read("scripts/verify.ps1")
    start = script.index("function Test-PackageHealth {")
    end = script.index("\n\nif (-not (Test-Path", start)
    package_health = script[start:end]

    cleanup = package_health.index("finally {")
    stop = package_health.index("Stop-Process -Id $process.Id -Force", cleanup)
    wait = package_health.index("$process.WaitForExit(10000)", stop)
    port_release = package_health.index("Wait-LocalPortReleased -Port $port", wait)
    dispose_finally = package_health.index("finally {", port_release)
    dispose = package_health.index("$process.Dispose()", dispose_finally)

    assert cleanup < stop < wait < port_release < dispose_finally < dispose
    assert package_health.count("$process.Dispose()") == 1


def test_dev_supervises_vite_readiness_and_both_exact_processes() -> None:
    script = _read("scripts/dev.ps1")
    root_vite = "node_modules\\vite\\bin\\vite.js"
    frontend_vite = "frontend\\node_modules\\vite\\bin\\vite.js"
    assert script.index(root_vite) < script.index(frontend_vite)
    assert "Test-Path -LiteralPath $_ -PathType Leaf" in script
    assert "Select-Object -First 1" in script
    assert "--strictPort" in script
    assert "$FrontendRoot = Join-Path $Root 'frontend'" in script
    assert "Invoke-WebRequest" in script
    assert "Restream Studio" in script
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in script
    assert "CreateProcessW" in script
    assert "CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW" in script
    assert "QuoteCommandLineArgument" in script
    assert "Start-Process" not in script
    create = script.index("CreateProcessW(", script.index("public Process StartProcessSuspended"))
    assign = script.index("AssignProcessToJobObject(handle, processInfo.Process)", create)
    resume = script.index("ResumeThread(processInfo.Thread)", assign)
    assert create < assign < resume
    assert script.count("$ProcessJob.StartProcessSuspended(") == 2
    frontend_launch = script.index("$frontend = $ProcessJob.StartProcessSuspended(")
    backend_launch = script.index("$backend = $ProcessJob.StartProcessSuspended(")
    assert script.index("$FrontendRoot", frontend_launch, backend_launch) > frontend_launch
    assert script.index("$Root", backend_launch) > backend_launch
    assert "CloseHandle(processInfo.Thread)" in script
    assert "CloseHandle(processInfo.Process)" in script
    assert "managedProcess.Handle" in script
    assert "TerminateJobObject" in script
    assert "$ProcessJob.Dispose()" in script
    for environment_name, old_value in (
        ("FFMPEG_PATH", "$oldFFmpeg"),
        ("FFPROBE_PATH", "$oldFFprobe"),
        ("RESTREAM_STUDIO_DATA_DIR", "$oldData"),
    ):
        assert f"GetEnvironmentVariable('{environment_name}')" in script
        assert f"SetEnvironmentVariable('{environment_name}', {old_value})" in script
    assert "WaitForExit" in script
    assert "Vite readiness timed out" in script
    assert "Vite exited before readiness" in script
    assert "backend exited with code" in script
    assert "Vite exited with code" in script
    assert "Stop-Process -Id $Process.Id -Force" in script
    assert "Stop-ExactProcess -Process $frontend" in script
    assert "Stop-ExactProcess -Process $backend" in script
    cleanup = script.rindex("\nfinally {")
    assert cleanup < script.index("Stop-ExactProcess -Process $frontend", cleanup)
    assert cleanup < script.index("Stop-ExactProcess -Process $backend", cleanup)


def test_dev_disposes_each_exact_process_once_after_stop_and_wait_on_all_paths() -> None:
    script = _read("scripts/dev.ps1")
    start = script.index("function Stop-ExactProcess {")
    end = script.index("\n\nPush-Location", start)
    stop_exact_process = script[start:end]

    cleanup_try = stop_exact_process.index("try {")
    stop = stop_exact_process.index("Stop-Process -Id $Process.Id -Force", cleanup_try)
    wait = stop_exact_process.index("$Process.WaitForExit(10000)", stop)
    dispose_finally = stop_exact_process.index("finally {", wait)
    dispose = stop_exact_process.index("$Process.Dispose()", dispose_finally)

    assert cleanup_try < stop < wait < dispose_finally < dispose
    assert stop_exact_process.count("$Process.Dispose()") == 1

    outer_cleanup = script[script.rindex("\nfinally {") :]
    frontend = outer_cleanup.index("Stop-ExactProcess -Process $frontend")
    backend_finally = outer_cleanup.index("finally {", frontend)
    backend = outer_cleanup.index("Stop-ExactProcess -Process $backend", backend_finally)
    assert frontend < backend_finally < backend


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
