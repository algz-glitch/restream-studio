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
        "scripts/smoke-installer.ps1",
        "scripts/run-smoke-installer-elevated.ps1",
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
    assert "{localappdata}\\RestreamStudio" in installer
    assert "ShouldDeleteUserData" in installer
    assert "/DELETEUSERDATA=1" in installer
    assert "UninstallSilent" in installer
    assert "ExpandConstant('{localappdata}\\RestreamStudio')" in installer
    assert "mbConfirmation" in installer
    assert "MB_DEFBUTTON2" in installer
    assert "[Run]" not in installer
    assert "[UninstallRun]" not in installer


def test_installer_smoke_userdata_override_is_bounded_and_default_is_unchanged() -> None:
    installer = _read("packaging/restream-studio.iss")

    assert 'DefaultUserDataDir := ExpandConstant(\'{localappdata}\\RestreamStudio\')' in installer
    assert "/USERDATADIR=" in installer
    assert "function InitializeSetup(): Boolean" in installer
    assert "function InitializeUninstall(): Boolean" in installer
    assert "RestreamStudioInstallerSmoke-" in installer
    assert "artifacts\\smoke" in installer
    assert "IsSafeSmokeUserDataDir" in installer
    assert "Refusing unsafe /USERDATADIR override." in installer
    assert "GetUserDataDir" in installer
    uninstall_delete = installer[
        installer.index("[UninstallDelete]") : installer.index("[Code]")
    ]
    assert 'Name: "{code:GetUserDataDir}"' in uninstall_delete
    assert 'Name: "{localappdata}\\RestreamStudio"' not in uninstall_delete


def test_installer_lifecycle_smoke_has_real_isolated_cleanup_contract() -> None:
    smoke = _read("scripts/smoke-installer.ps1")

    for marker in (
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/DIR=",
        "/USERDATADIR=",
        "/DELETEUSERDATA=1",
        "RestreamStudioInstallerSmoke-",
        "artifacts\\smoke",
        "RestreamStudio-Installed-Localhost",
        "Get-NetFirewallApplicationFilter",
        "Get-NetFirewallPortFilter",
        "Get-NetFirewallAddressFilter",
        "127.0.0.1",
        "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall",
        "WScript.Shell",
        "Invoke-RestMethod",
        "Invoke-WebRequest",
        "marker",
        "Wait-LocalPortReleased",
        "finally",
    ):
        assert marker in smoke

    assert "Get-FileHash" in smoke
    assert "Assert-SafeLifecyclePath" in smoke
    assert "Remove-Item -LiteralPath" in smoke
    assert "Move-Item" not in smoke
    assert "LOCALAPPDATA\\RestreamStudio" not in smoke.upper()
    assert "Stop-Process -Name" not in smoke
    assert "-Recurse -Force" in smoke


def test_installer_smoke_restores_only_complete_exact_snapshots() -> None:
    smoke = _read("scripts/smoke-installer.ps1")

    assert "$SnapshotCaptured = $false" in smoke
    assert "$FirewallMutationPossible = $false" in smoke
    assert "$SnapshotCaptured = $true" in smoke
    assert "Assert-KnownFirewallSchema" in smoke
    assert "Get-NetFirewallInterfaceFilter" in smoke
    assert "Get-NetFirewallInterfaceTypeFilter" in smoke
    assert "Get-NetFirewallServiceFilter" in smoke
    assert "existing canonical/legacy firewall rules prevent isolated smoke" in smoke
    assert "Remove-IsolatedFirewallRules" in smoke
    assert "New-NetFirewallRule" not in smoke
    assert "non-standard pre-existing Restream Studio firewall rule" in smoke


def test_installer_smoke_shortcut_backups_are_independent_and_hash_verified() -> None:
    smoke = _read("scripts/smoke-installer.ps1")

    assert "$DesktopBackupSucceeded = $false" in smoke
    assert "$StartMenuBackupSucceeded = $false" in smoke
    assert "$DesktopBackupHash" in smoke
    assert "$StartMenuBackupHash" in smoke
    assert "Backup-Shortcut" in smoke
    assert "Restore-ShortcutAtomically" in smoke
    assert "desktop shortcut restore failed" in smoke
    assert "start menu shortcut restore failed" in smoke
    assert "File]::Replace" in smoke
    assert "backup hash verification failed" in smoke


def test_smoke_paths_reject_reparse_ancestors_and_cleanup_fixed_appid_only() -> None:
    smoke = _read("scripts/smoke-installer.ps1")
    installer = _read("packaging/restream-studio.iss")

    assert "Assert-NoReparsePointAncestors" in smoke
    assert "FileAttributes]::ReparsePoint" in smoke
    assert smoke.count("Assert-NoReparsePointAncestors -Path $Path") >= 2
    assert "$UninstallSubKey = '{6D5EE4A8-17C8-4DA0-84F8-28710EAB90F4}_is1'" in smoke
    assert "DeleteSubKeyTree($UninstallSubKey" in smoke
    assert "Remove-IsolatedUninstallRegistration" in smoke
    assert "HasReparsePointAncestor" in installer
    assert "GetFileAttributesW@kernel32.dll stdcall" in installer
    reparse = installer[installer.index("function HasReparsePointAncestor") : installer.index("function IsTempSmokePath")]
    assert "powershell.exe" not in reparse


def test_installer_processes_are_bounded_and_smoke_requires_administrator() -> None:
    smoke = _read("scripts/smoke-installer.ps1")

    assert "Invoke-BoundedProcess" in smoke
    assert "WaitForExit($TimeoutSeconds * 1000)" in smoke
    assert "taskkill.exe" in smoke
    assert "$killer.ExitCode" in smoke
    assert "$taskkillCompleted = $false" in smoke
    assert "$taskkillExitCode = $null" in smoke
    assert "$LifecycleProcessStillRunning" in smoke
    assert smoke.count("if ($LifecycleProcessStillRunning)") >= 3
    assert "$Target.Refresh()" in smoke
    assert "$taskkillCompleted -and $taskkillExitCode -eq 0 -and" in smoke
    stopped = smoke[smoke.index("function Stop-KnownProcessTree") : smoke.index("function Invoke-BoundedProcess")]
    assert stopped.index("$taskkillCompleted -and $taskkillExitCode -eq 0 -and") < stopped.index(
        "$script:LifecycleProcessStillRunning = $false"
    )
    assert "CRITICAL: installer or uninstaller process tree may still be running" in smoke
    cleanup = smoke[smoke.rindex("\nfinally {\n") :]
    assert cleanup.index("$LifecycleProcessStillRunning") < cleanup.index(
        "Remove-IsolatedUninstallRegistration"
    )
    assert "installer lifecycle smoke requires administrator PowerShell" in smoke
    assert "RunAs" not in smoke
    assert "ElevatedChild" not in smoke
    assert "Invoke-NativeChecked" not in smoke
    broker = _read("scripts/run-smoke-installer-elevated.ps1")
    assert "-Verb RunAs" in broker
    assert "-EncodedCommand" in broker
    assert "-Wait -PassThru" in broker
    assert "taskkill" not in broker
    assert "Test-IsAdministrator" in broker
    assert "if (Test-IsAdministrator)" in broker


def test_smoke_build_uses_real_upgrade_version_and_failure_injection_fixture() -> None:
    installer = _read("packaging/restream-studio.iss")
    build = _read("scripts/build-installer.ps1")
    smoke = _read("scripts/smoke-installer.ps1")
    verify = _read("scripts/verify.ps1")
    release = _read(".github/workflows/release.yml")

    assert "#ifndef MyAppVersion" in installer
    assert "#ifndef SmokeTestBuild" in installer
    assert "/SMOKEFIREWALLFAIL=" in installer
    assert "cancel" in installer
    assert "command" in installer
    assert "[switch]$BuildSmokeFixtures" in build
    assert "function Get-NextPatchVersion" in build
    assert "$SmokeUpgradeVersion = Get-NextPatchVersion" in build
    assert '"/DMyAppVersion=$SmokeUpgradeVersion"' in build
    assert "/DMyAppVersion=0.1.1" not in build
    assert "/DSmokeTestBuild=1" in build
    assert "SMOKE_UPGRADE_INSTALLER_PATH=" in build
    assert "UpgradeInstaller" in smoke
    assert "DisplayVersion" in smoke
    assert "Get-NextPatchVersion" in smoke
    assert "Assert-UninstallRegistration $CurrentVersion" in smoke
    assert "Assert-UninstallRegistration $UpgradeVersion" in smoke
    assert "tamperedProgramHash" in smoke
    assert "@('cancel','command')" in smoke
    assert '"/SMOKEFIREWALLFAIL=$mode"' in smoke
    assert "-BuildSmokeFixtures" in verify
    assert "-BuildSmokeFixtures" not in release
    assert "/DSmokeTestBuild" not in release
    package = _read("scripts/package.ps1")
    assert "Get-NextPatchVersion" in package
    assert "VersionOverride must equal the smoke-only next patch version" in package
    assert '(str(version_file), "restream_studio")' in _read(
        "packaging/restream-studio.spec"
    )
    assert '$health.version -eq $ExpectedVersion' in smoke


def test_verify_builds_and_runs_installer_lifecycle_without_release_skip() -> None:
    verify = _read("scripts/verify.ps1")
    release = _read(".github/workflows/release.yml")

    assert "[switch]$SkipInstallerLifecycle" in verify
    assert "INSTALLER_BUILD" in verify
    assert "INSTALLER_LIFECYCLE" in verify
    assert "build-installer.ps1" in verify
    assert "run-smoke-installer-elevated.ps1" in verify
    assert "SMOKE_UPGRADE_INSTALLER_PATH=" in verify
    assert "$SmokeUpgradeInstaller = ''" in verify
    assert "-ReuseVerifiedPackage" in verify
    assert "-PackageManifest" in verify
    assert "INSTALLER_LIFECYCLE=SKIP" in verify
    assert "-SkipInstallerLifecycle" not in release


def test_inno_checks_firewall_exit_codes_before_install_and_uninstall_mutation() -> None:
    installer = _read("packaging/restream-studio.iss")
    assert "function PrepareToInstall" not in installer
    assert 'Source: "{#MyDistributionDir}\\RestreamStudio.exe"' in installer
    files = installer[installer.index("[Files]") : installer.index("[Icons]")]
    assert "AfterInstall: ConfigureFirewall" in files
    assert "procedure CurStepChanged(CurStep: TSetupStep)" in installer
    assert "CurStep = ssDone" in installer
    assert "InstallCommitted := True" in installer
    assert "ssPostInstall" not in installer
    assert "procedure ConfigureFirewall" in installer
    assert "ShellExec('runas'" in installer
    assert "ResultCode" in installer
    assert "ResultCode <> 0" in installer
    assert "RaiseException(" in installer
    assert "CurUninstallStepChanged" in installer
    assert "CurUninstallStep = usUninstall" in installer
    assert "Abort;" in installer
    assert "-File" not in installer
    assert "Flags: dontcopy" not in installer
    assert "HexEncode(ApplicationPath)" in installer
    assert "$p='''';for($i=0;" in installer
    assert "-join (for(" not in installer
    assert "-Command" in installer
    assert "RestreamStudio-Installed-Localhost" in installer
    assert "Restream Studio (Loopback TCP)" in installer
    assert "$restoreProgram" in installer
    assert "$a.Program" in installer
    assert "$a.Program -ieq $p" in installer
    assert "[IO.Path]::GetFileName($a.Program)" in installer
    assert "&$isFull $a.Program" in installer
    assert "-Program $old" in installer
    assert "Sort-Object Name -Unique" in installer
    install_command = installer[
        installer.index("function InstalledFirewallCommand") :
        installer.index("function RemoveInstalledFirewallCommand")
    ]
    capture = install_command[: install_command.index("'try{' +")]
    assert "$a.Program-ieq $p" not in capture


def test_inno_firewall_runs_after_every_file_and_before_only_optional_launch() -> None:
    installer = _read("packaging/restream-studio.iss")
    files = installer[installer.index("[Files]") : installer.index("[Icons]")]
    entries = [line for line in files.splitlines() if line.startswith("Source:")]
    assert entries[-1].startswith('Source: "firewall-remove.ps1"')
    assert entries[-1].endswith("AfterInstall: ConfigureFirewall")
    assert sum("AfterInstall:" in entry for entry in entries) == 1


def test_inno_snapshots_firewall_to_memory_and_rolls_back_uncommitted_install() -> None:
    installer = _read("packaging/restream-studio.iss")
    configure = installer[
        installer.index("procedure ConfigureFirewall") :
        installer.index("procedure CurStepChanged")
    ]
    assert "NewFirewallStatePath" in configure
    assert "Random(1000000000)" in installer
    assert "ExpandConstant('{tmp}')" in installer
    assert "LoadStringFromFile" in configure
    assert "PreviousFirewallProgram" in configure
    assert "DeleteFile" in configure
    loaded = configure.index("LoadStringFromFile")
    assert loaded < configure.index("DeleteFile", loaded)
    assert "FirewallChanged := True" in configure
    assert "HexDecode" in installer
    assert "IsFullyQualifiedApplicationPath" in installer
    assert "[IO.Path]::IsPathFullyQualified" not in installer
    snapshot_command = installer[
        installer.index("function SnapshotFirewallCommand") :
        installer.index("function InstalledFirewallCommand")
    ]
    install_command = installer[
        installer.index("function InstalledFirewallCommand") :
        installer.index("function RemoveInstalledFirewallCommand")
    ]
    assert "WriteAllText" in snapshot_command
    assert "$sh=$args[0]" in snapshot_command
    assert "WriteAllText" not in install_command
    assert "state path" not in install_command
    assert "$args[1]" not in install_command
    assert "RunSnapshotPowerShell" in configure
    non_elevated = installer[
        installer.index("function RunSnapshotPowerShell") :
        installer.index("function RunElevatedPowerShell")
    ]
    assert "Exec(" in non_elevated
    assert "ShellExec" not in non_elevated
    assert "runas" not in non_elevated
    assert "procedure DeinitializeSetup" in installer
    rollback = installer[installer.index("procedure DeinitializeSetup") :]
    assert "FirewallChanged and (not InstallCommitted)" in rollback
    assert "RollbackFirewallCommand" in rollback
    assert "PreviousFirewallProgram" in rollback
    assert "Log(" in rollback


def test_old_uninstaller_filters_canonical_and_legacy_rules_by_current_program() -> None:
    installer = _read("packaging/restream-studio.iss")
    removal = installer[
        installer.index("function RemoveInstalledFirewallCommand") :
        installer.index("procedure ConfigureFirewall")
    ]
    assert "foreach($r in $candidates)" in removal
    assert "$a.Program -ieq $p" in removal
    assert "$r|Remove-NetFirewallRule" in removal
    assert "Sort-Object Name -Unique" in removal
    assert "firewall removal verification failed" in removal
    assert "Get-NetFirewallRule -Name $n -EA SilentlyContinue|" not in removal


def test_inno_offers_checked_postinstall_launch_only_when_interactive() -> None:
    installer = _read("packaging/restream-studio.iss")
    assert "LaunchAfterInstallCheck" in installer
    assert "WizardForm.FinishedPage" in installer
    assert "not WizardSilent" in installer
    assert "CurPageID = wpFinished" in installer
    assert "ewNoWait" in installer


def test_firewall_hooks_self_elevate_and_scope_both_loopback_ends_to_program() -> None:
    install = _read("packaging/firewall-install.ps1")
    remove = _read("packaging/firewall-remove.ps1")

    for script in (install, remove):
        assert "WindowsBuiltInRole]::Administrator" in script
        assert "-Verb RunAs" in script
        assert "RestreamStudio.exe" in script
        assert "$RuleName = 'RestreamStudio-Installed-Localhost'" in script
        assert "Get-NetFirewallRule -Name $RuleName" in script

    assert "127.0.0.1" in install
    assert "New-NetFirewallRule" in install
    assert "-Name $RuleName" in install
    assert "-Program $resolvedExecutable" in install
    assert "-Direction Inbound" in install
    assert "-Protocol TCP" in install
    assert "-LocalAddress '127.0.0.1'" in install
    assert "-RemoteAddress '127.0.0.1'" in install
    assert "$application.Program -ieq $resolvedExecutable" in install
    assert "$port.Protocol -eq 'TCP'" in install
    assert "$address.LocalAddress" in install
    assert "$address.RemoteAddress" in install
    assert "$restoreProgram" in install
    assert "-Program $restoreProgram" in install
    assert "Restream Studio (Loopback TCP)" in install
    assert "Remove-NetFirewallRule" in remove
    assert "Restream Studio (Loopback TCP)" in remove
    assert "$application.Program -ieq $resolvedExecutable" in remove
    assert "firewall rule removal verification failed" in remove


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
    pinned = (
        "F:\\printflow-ai\\workbench-v4-functional\\dist\\tools\\"
        "inno-setup-6.7.3\\ISCC.exe"
    )
    assert pinned not in build
    assert "Get-Command 'ISCC.exe'" not in build
    assert "JRSoftware.InnoSetup" in build
    assert "6.7.3" in build
    assert "--exact" in build
    assert "function Assert-IsccVersion" in build
    assert "$output = @(& $Path $probe" in build
    assert "Compiler engine version: Inno Setup 6.7.3" in build
    assert "ISCC_PATH must point to Inno Setup 6.7.3" in build
    assert "function Find-RegisteredIscc" in build
    assert "DisplayVersion" in build
    assert "InstallLocation" in build
    assert "RegistryView]::Registry64" in build
    assert "RegistryView]::Registry32" in build
    assert "Get-AuthenticodeSignature" in build
    assert "Pyrsys B.V." in build
    assert "ReuseVerifiedPackage" in build
    assert "ReuseVerifiedOutput" in package
    assert "RESTREAM_STUDIO_VERIFIED_COMMIT" in package
    assert "if ($ReuseVerifiedOutput -or $ReuseFrontend)" in package
    assert "PackageManifest" in build
    assert "write-tree-manifest.py" in package
    assert "--verify-existing" in package
    assert "function Get-ReleaseVersion" in build
    assert "RestreamStudio-Setup-$Version.exe" in build
    assert "RestreamStudio-Setup-0.1.0.exe" not in build
    assert "Get-FileHash" in build
    assert "SHA256=" in build
    assert "Start-Process -FilePath $copiedUpdateHelper" in build
    assert "$helperProcess.ExitCode -ne 2" in build
    assert "did not clean its unique runtime directory" in build
    assert "Copy-Item -LiteralPath $updateHelper" in build
    assert "RestreamStudioUpdateHelper-smoke" in build
    smoke = build[build.index("$helperSmokeDirectory") : build.index("$iscc =")]
    assert smoke.index("try {") < smoke.index("New-Item")
    assert "finally" in smoke


def test_update_helper_is_a_standalone_onefile_executable() -> None:
    spec = _read("packaging/restream-studio.spec")
    helper = spec[spec.index("helper_exe = EXE(") : spec.index("distribution = COLLECT(")]
    distribution = spec[spec.index("distribution = COLLECT(") :]
    assert "helper_analysis.binaries" in helper
    assert "helper_analysis.datas" in helper
    assert "exclude_binaries=True" not in helper
    assert "helper_analysis.binaries" not in distribution
    assert "helper_analysis.datas" not in distribution


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
    assert '$RuleName = "RestreamStudio-Portable-Localhost-$pathHash"' in script
    assert "SHA256" in script
    assert "RestreamStudio-Installed-Localhost" not in script


def test_installer_never_elevates_a_user_writable_script() -> None:
    installer = _read("packaging/restream-studio.iss")
    assert "ShellExec('runas'" in installer
    assert "firewall-install.ps1'" not in installer
    assert "firewall-remove.ps1'" not in installer
    assert "ExtractTemporaryFile" not in installer


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
        "FRONTEND_TYPES",
        "FRONTEND_LINT",
        "FRONTEND_TESTS",
        "FRONTEND_BUILD",
        "PYTHON_TESTS",
        "LOCAL_RTMP_E2E",
        "PACKAGE_SMOKE",
        "INSTALLER_BUILD",
        "INSTALLER_LIFECYCLE",
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
    assert "-ReuseFrontend" in script
    assert "FRONTEND_MANIFEST_PATH=" in script
    assert "PACKAGE_MANIFEST_PATH=" in script
    assert script.count("@('ci', '--ignore-scripts', '--no-audit', '--no-fund')") == 1
    assert "RESTREAM_STUDIO_FRONTEND_ALREADY_BUILT" in script
    assert "@('-m', 'mypy', 'src', 'tests', 'scripts')" in script
    assert script.index("Invoke-Gate -Name 'FRONTEND_BUILD'") < script.index(
        "Invoke-Gate -Name 'PYTHON_TESTS'"
    )
    assert "TcpListener" in script
    assert "Invoke-RestMethod" in script
    assert "/health" in script
    assert "restream-studio" in script
    assert "function Get-ProjectVersion" in script
    assert "$health.version -eq $ProjectVersion" in script
    assert "$health.version -eq '0.1.0'" not in script
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
