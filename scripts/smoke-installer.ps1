[CmdletBinding()]
param([Parameter(Mandatory)][string]$Installer,
    [Parameter(Mandatory)][string]$UpgradeInstaller, [string]$WorkspaceRoot = '')

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$LifecycleProcessStillRunning = $false

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Stop-KnownProcessTree {
    param([Parameter(Mandatory)][Diagnostics.Process]$Target)
    $script:LifecycleProcessStillRunning = $true
    $killer = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\taskkill.exe') `
        -ArgumentList @('/PID', [string]$Target.Id, '/T', '/F') -PassThru -WindowStyle Hidden
    $taskkillCompleted = $false
    $taskkillExitCode = $null
    $taskkillError = ''
    try {
        $taskkillCompleted = $killer.WaitForExit(15000)
        if (-not $taskkillCompleted) {
            try { Stop-Process -Id $killer.Id -Force }
            catch { $taskkillError = "unable to stop timed-out taskkill: $($_.Exception.Message)" }
            try {
                if (-not $killer.WaitForExit(5000)) {
                    $taskkillError = 'taskkill did not exit after its bounded timeout'
                }
            }
            catch { $taskkillError = "unable to confirm taskkill termination: $($_.Exception.Message)" }
        }
        $killer.Refresh()
        if ($taskkillCompleted -and $killer.HasExited) {
            $taskkillExitCode = $killer.ExitCode
        }
    }
    catch { $taskkillError = "taskkill status check failed: $($_.Exception.Message)" }
    finally { $killer.Dispose() }

    $targetWaitCompleted = $false
    $targetHasExited = $false
    $targetError = ''
    try {
        $targetWaitCompleted = $Target.WaitForExit(15000)
        $Target.Refresh()
        $targetHasExited = $Target.HasExited
    }
    catch { $targetError = $_.Exception.Message }
    if (-not ($taskkillCompleted -and $taskkillExitCode -eq 0 -and
        $targetWaitCompleted -and $targetHasExited)) {
        throw "CRITICAL: installer or uninstaller process tree may still be running because termination was not fully verified; taskkillCompleted=$taskkillCompleted; taskkillExitCode=$taskkillExitCode; targetWaitCompleted=$targetWaitCompleted; targetHasExited=$targetHasExited; taskkillError=$taskkillError; targetError=$targetError; no restore or delete is permitted; manual intervention required"
    }
    $script:LifecycleProcessStillRunning = $false
}

function Invoke-BoundedProcess {
    param([string]$FilePath, [string[]]$Arguments, [int]$TimeoutSeconds = 300,
        [string]$TimeoutMessage = 'process timed out', [switch]$AllowNonZero)
    $p = @{ FilePath = $FilePath; ArgumentList = $Arguments; PassThru = $true }
    $p['WindowStyle'] = 'Hidden'
    $child = Start-Process @p
    try {
        if (-not $child.WaitForExit($TimeoutSeconds * 1000)) {
            Stop-KnownProcessTree -Target $child
            throw $TimeoutMessage
        }
        if (-not $AllowNonZero -and $child.ExitCode -ne 0) {
            throw "$FilePath exited with code $($child.ExitCode)"
        }
        return $child.ExitCode
    }
    finally { $child.Dispose() }
}

$Root = if ($WorkspaceRoot) {
    [IO.Path]::GetFullPath($WorkspaceRoot)
}
else {
    Split-Path -Parent $PSScriptRoot
}
$Installer = (Resolve-Path -LiteralPath $Installer -ErrorAction Stop).Path
$UpgradeInstaller = (Resolve-Path -LiteralPath $UpgradeInstaller -ErrorAction Stop).Path
if (-not (Test-IsAdministrator)) { throw 'installer lifecycle smoke requires administrator PowerShell' }
$SmokeParent = Join-Path $Root 'artifacts\smoke'
$SmokeRoot = Join-Path $SmokeParent "RestreamStudioInstallerSmoke-$([Guid]::NewGuid().ToString('N'))"
$InstallDir = Join-Path $SmokeRoot 'app'
$UserDataDir = Join-Path $SmokeRoot 'data'
$BackupDir = Join-Path $SmokeRoot 'backup'
$Distribution = Join-Path $Root 'dist\RestreamStudio'
$UpgradeDistribution = Join-Path $Root 'dist\smoke-upgrade\RestreamStudio'
$RuleName = 'RestreamStudio-Installed-Localhost'
$LegacyRuleDisplayName = 'Restream Studio (Loopback TCP)'
$UninstallRegistryPath = 'Software\Microsoft\Windows\CurrentVersion\Uninstall'
$UninstallSubKey = '{6D5EE4A8-17C8-4DA0-84F8-28710EAB90F4}_is1'
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'Restream Studio.lnk'
$StartMenuGroup = Join-Path ([Environment]::GetFolderPath('Programs')) 'Restream Studio'
$StartMenuShortcut = Join-Path $StartMenuGroup 'Restream Studio.lnk'
$Process = $null
$Port = 0
$InitialFirewall = @()
$InitialFirewallFingerprint = ''
$SnapshotCaptured = $false
$FirewallMutationPossible = $false
$UserStateMutationPossible = $false
$DesktopBackup = Join-Path $BackupDir 'desktop.lnk'
$StartMenuBackup = Join-Path $BackupDir 'start-menu.lnk'
$DesktopExisted = Test-Path -LiteralPath $DesktopShortcut -PathType Leaf
$StartMenuExisted = Test-Path -LiteralPath $StartMenuShortcut -PathType Leaf
$StartMenuGroupExisted = Test-Path -LiteralPath $StartMenuGroup -PathType Container
$DesktopBackupSucceeded = $false
$StartMenuBackupSucceeded = $false
$DesktopBackupHash = ''
$StartMenuBackupHash = ''

function Get-NextPatchVersion {
    param([Parameter(Mandatory)][string]$Version)
    if ($Version -cnotmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') {
        throw 'lifecycle version must be canonical SemVer'
    }
    $nextPatch = [Numerics.BigInteger]::Parse($Matches[3]) + 1
    return "$($Matches[1]).$($Matches[2]).$nextPatch"
}

$CurrentVersion = [string](
    [IO.File]::ReadAllText((Join-Path $Root 'package.json')) | ConvertFrom-Json
).version
$UpgradeVersionPath = Join-Path $UpgradeDistribution '_internal\restream_studio\version.txt'
if (-not (Test-Path -LiteralPath $UpgradeVersionPath -PathType Leaf)) {
    throw 'smoke upgrade payload version metadata is missing'
}
$UpgradeVersion = [IO.File]::ReadAllText($UpgradeVersionPath).Trim()
if ($UpgradeVersion -cne (Get-NextPatchVersion $CurrentVersion)) {
    throw 'smoke upgrade payload is not the next patch version'
}

function Test-PathInside {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Parent)
    $fullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $fullParent = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    return $fullPath.Length -gt $fullParent.Length -and
        $fullPath.StartsWith("$fullParent\", [StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparsePointAncestors {
    param([string]$Path)
    $current = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "reparse point ancestor rejected: $current"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if (-not $parent -or $parent -ceq $current) { break }
        $current = $parent.TrimEnd('\')
    }
}

function Assert-SafeLifecyclePath {
    param([Parameter(Mandatory)][string]$Path)
    if (-not [IO.Path]::IsPathFullyQualified($Path)) {
        throw "lifecycle path is not absolute: $Path"
    }
    $temp = [IO.Path]::GetFullPath($env:TEMP)
    $workspaceSmoke = [IO.Path]::GetFullPath((Join-Path $Root 'artifacts\smoke'))
    $allowed = (Test-PathInside -Path $Path -Parent $workspaceSmoke) -or
        ((Test-PathInside -Path $Path -Parent $temp) -and
         ([IO.Path]::GetFileName(([IO.Path]::GetDirectoryName($Path))) -like
            'RestreamStudioInstallerSmoke-*' -or
          [IO.Path]::GetFileName($Path) -like 'RestreamStudioInstallerSmoke-*'))
    if (-not $allowed) { throw "refusing lifecycle path outside workspace/temp smoke roots: $Path" }
    Assert-NoReparsePointAncestors -Path $Path
}

function Remove-SafeTree {
    param([Parameter(Mandatory)][string]$Path)
    Assert-SafeLifecyclePath -Path $Path
    Assert-NoReparsePointAncestors -Path $Path
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Get-DynamicPort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally { $listener.Stop() }
}

function Test-LocalPortAvailable {
    param([Parameter(Mandatory)][int]$PortNumber)
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $PortNumber)
    try { $listener.Start(); return $true }
    catch { return $false }
    finally { $listener.Stop() }
}

function Wait-LocalPortReleased {
    param([Parameter(Mandatory)][int]$PortNumber, [int]$TimeoutSeconds = 15)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-LocalPortAvailable -PortNumber $PortNumber) { return $true }
        Start-Sleep -Milliseconds 100
    }
    return $false
}

function Get-UninstallEntry {
    $views = @([Microsoft.Win32.RegistryView]::Registry64,
        [Microsoft.Win32.RegistryView]::Registry32)
    foreach ($view in $views) {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
            [Microsoft.Win32.RegistryHive]::CurrentUser, $view
        )
        try {
            $uninstall = $base.OpenSubKey("$UninstallRegistryPath\$UninstallSubKey")
            if ($null -eq $uninstall) { continue }
            try {
                return [pscustomobject]@{ View=$view; Name=$UninstallSubKey
                    DisplayName=[string]$uninstall.GetValue('DisplayName')
                    DisplayVersion=[string]$uninstall.GetValue('DisplayVersion')
                    InstallLocation=[string]$uninstall.GetValue('InstallLocation')
                    UninstallString=[string]$uninstall.GetValue('UninstallString') }
            }
            finally { $uninstall.Dispose() }
        }
        finally { $base.Dispose() }
    }
    return $null
}

function Remove-IsolatedUninstallRegistration {
    $entry = Get-UninstallEntry
    if ($null -eq $entry) { return }
    if (-not $entry.InstallLocation -or
        -not (Test-PathInside $entry.InstallLocation $SmokeParent)) {
        throw 'refusing fixed AppId cleanup outside smoke root'
    }
    Assert-SafeLifecyclePath $entry.InstallLocation
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::CurrentUser, $entry.View)
    try {
        $parent = $base.OpenSubKey($UninstallRegistryPath, $true)
        try { $parent.DeleteSubKeyTree($UninstallSubKey, $false) }
        finally { if ($null -ne $parent) { $parent.Dispose() } }
    }
    finally { $base.Dispose() }
    if ($null -ne (Get-UninstallEntry)) { throw 'fixed AppId cleanup verification failed' }
}

function Get-ShortcutTarget {
    param([Parameter(Mandatory)][string]$Path)
    $shell = New-Object -ComObject WScript.Shell
    try { return [IO.Path]::GetFullPath($shell.CreateShortcut($Path).TargetPath) }
    finally { [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) | Out-Null }
}

function Assert-KnownFirewallSchema {
    param($Rule)
    $application = $Rule | Get-NetFirewallApplicationFilter -ErrorAction Stop
    $portFilter = $Rule | Get-NetFirewallPortFilter -ErrorAction Stop
    $address = $Rule | Get-NetFirewallAddressFilter -ErrorAction Stop
    $interface = $Rule | Get-NetFirewallInterfaceFilter -ErrorAction Stop
    $interfaceType = $Rule | Get-NetFirewallInterfaceTypeFilter -ErrorAction Stop
    $service = $Rule | Get-NetFirewallServiceFilter -ErrorAction Stop
    $one = { param($v,$e) @($v).Count -eq 1 -and [string]@($v)[0] -ceq $e }
    $program = [string]$application.Program
    if (-not (($Rule.Name -ceq $RuleName -or $Rule.DisplayName -ceq $LegacyRuleDisplayName) -and
        [IO.Path]::IsPathFullyQualified($program) -and [IO.Path]::GetFileName($program) -ieq 'RestreamStudio.exe' -and
        [string]$Rule.Direction -ceq 'Inbound' -and [string]$Rule.Action -ceq 'Allow' -and
        [string]$Rule.Enabled -ceq 'True' -and [string]$Rule.Profile -ceq 'Any' -and
        [string]$Rule.EdgeTraversalPolicy -ceq 'Block' -and [string]$portFilter.Protocol -ceq 'TCP' -and
        (&$one $portFilter.LocalPort 'Any') -and (&$one $portFilter.RemotePort 'Any') -and
        (&$one $address.LocalAddress '127.0.0.1') -and (&$one $address.RemoteAddress '127.0.0.1') -and
        (&$one $interface.InterfaceAlias 'Any') -and (&$one $interfaceType.InterfaceType 'Any') -and
        [string]$service.Service -ceq 'Any')) {
        throw 'non-standard pre-existing Restream Studio firewall rule'
    }
    return [pscustomobject]@{ Name=[string]$Rule.Name; DisplayName=[string]$Rule.DisplayName
        Description=[string]$Rule.Description; Group=[string]$Rule.Group; Program=$program }
}

function Get-FirewallSnapshot {
    $rules = @(@(Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue) +
        @(Get-NetFirewallRule -DisplayName $LegacyRuleDisplayName -ErrorAction SilentlyContinue) |
        Sort-Object Name -Unique)
    return @($rules | ForEach-Object {
        Assert-KnownFirewallSchema $_
    })
}

function Remove-IsolatedFirewallRules {
    $rules = @(@(Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue) +
        @(Get-NetFirewallRule -DisplayName $LegacyRuleDisplayName -ErrorAction SilentlyContinue) |
        Sort-Object Name -Unique)
    foreach ($rule in $rules) {
        $application = $rule | Get-NetFirewallApplicationFilter -ErrorAction Stop
        if (Test-PathInside $application.Program $InstallDir) {
            $rule | Remove-NetFirewallRule -ErrorAction Stop
        }
    }
    $remaining = @(@(Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue) +
        @(Get-NetFirewallRule -DisplayName $LegacyRuleDisplayName -ErrorAction SilentlyContinue) |
        Sort-Object Name -Unique)
    if ($remaining.Count -ne 0) {
        throw 'refusing to remove non-isolated Restream Studio firewall rule'
    }
}

function Assert-InstalledFiles {
    param([string]$ExpectedDistribution=$Distribution)
    $sourceFiles = @(Get-ChildItem -LiteralPath $ExpectedDistribution -File -Recurse)
    if ($sourceFiles.Count -eq 0) { throw 'packaged distribution is empty' }
    foreach ($source in $sourceFiles) {
        $relative = $source.FullName.Substring($ExpectedDistribution.Length).TrimStart('\')
        $target = Join-Path $InstallDir $relative
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw "installed file is missing: $relative"
        }
        if ((Get-FileHash -LiteralPath $source.FullName -Algorithm SHA256).Hash -cne
            (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash) {
            throw "installed file hash differs: $relative"
        }
    }
    foreach ($relative in ('packaging\firewall-install.ps1', 'packaging\firewall-remove.ps1')) {
        if (-not (Test-Path -LiteralPath (Join-Path $InstallDir $relative) -PathType Leaf)) {
            throw "installer support file is missing: $relative"
        }
    }
}

function Assert-Shortcuts {
    $expected = [IO.Path]::GetFullPath((Join-Path $InstallDir 'RestreamStudio.exe'))
    foreach ($shortcut in ($DesktopShortcut, $StartMenuShortcut)) {
        if (-not (Test-Path -LiteralPath $shortcut -PathType Leaf)) {
            throw "shortcut is missing: $shortcut"
        }
        if ((Get-ShortcutTarget -Path $shortcut) -cne $expected) {
            throw "shortcut target is not the isolated executable: $shortcut"
        }
    }
}

function Assert-UninstallRegistration {
    param([string]$ExpectedVersion)
    $entry = Get-UninstallEntry
    if ($null -eq $entry -or $entry.DisplayVersion -cne $ExpectedVersion) { throw 'HKCU uninstall registration is missing or wrong version' }
    if ([IO.Path]::GetFullPath($entry.InstallLocation).TrimEnd('\') -cne
        [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')) {
        throw 'HKCU uninstall registration points outside the isolated install'
    }
    if (-not $entry.UninstallString.Contains((Join-Path $InstallDir 'unins000.exe'))) {
        throw 'HKCU uninstall command does not target the isolated install'
    }
    return $entry
}

function Assert-FirewallRule {
    $rules = @(Get-NetFirewallRule -Name $RuleName -ErrorAction Stop)
    if ($rules.Count -ne 1) { throw 'installed firewall rule count is not exactly one' }
    $rule = $rules[0]
    $application = $rule | Get-NetFirewallApplicationFilter -ErrorAction Stop
    $portFilter = $rule | Get-NetFirewallPortFilter -ErrorAction Stop
    $address = $rule | Get-NetFirewallAddressFilter -ErrorAction Stop
    $expectedProgram = [IO.Path]::GetFullPath((Join-Path $InstallDir 'RestreamStudio.exe'))
    if ([IO.Path]::GetFullPath($application.Program) -cne $expectedProgram -or
        [string]$portFilter.Protocol -cne 'TCP' -or
        (@($address.LocalAddress) -join ',') -cne '127.0.0.1' -or
        (@($address.RemoteAddress) -join ',') -cne '127.0.0.1' -or
        [string]$rule.Direction -cne 'Inbound' -or [string]$rule.Action -cne 'Allow' -or
        [string]$rule.Enabled -cne 'True') {
        throw 'installed firewall rule is outside the exact Program/TCP/loopback contract'
    }
}

function Start-And-TestInstalledApplication {
    param([string]$ExpectedVersion)
    $script:Port = Get-DynamicPort
    $stdout = Join-Path $SmokeRoot "app-$Port.stdout.log"
    $stderr = Join-Path $SmokeRoot "app-$Port.stderr.log"
    $oldPort = [Environment]::GetEnvironmentVariable('RESTREAM_STUDIO_PORT')
    $oldData = [Environment]::GetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR')
    try {
        [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_PORT', [string]$Port)
        [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR', $UserDataDir)
        $script:Process = Start-Process -FilePath (Join-Path $InstallDir 'RestreamStudio.exe') `
            -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr
    }
    finally {
        [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_PORT', $oldPort)
        [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR', $oldData)
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        $Process.Refresh()
        if ($Process.HasExited) { throw "installed application exited with code $($Process.ExitCode)" }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            $ui = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/" -TimeoutSec 2
            if ($health.status -eq 'ok' -and $health.app -eq 'restream-studio' -and
                $health.version -eq $ExpectedVersion -and
                $ui.StatusCode -eq 200 -and $ui.Content -match '<title>\s*Restream Studio\s*</title>') {
                return
            }
        }
        catch { Start-Sleep -Milliseconds 200 }
    }
    throw 'installed API/UI readiness timed out'
}

function Stop-InstalledApplication {
    if ($null -eq $script:Process) { return }
    try {
        $script:Process.Refresh()
        if (-not $script:Process.HasExited) { Stop-Process -Id $script:Process.Id -Force }
        if (-not $script:Process.WaitForExit(10000)) { throw 'installed process did not exit' }
        if (-not (Wait-LocalPortReleased -PortNumber $script:Port)) {
            throw "installed application port was not released: $script:Port"
        }
    }
    finally { $script:Process.Dispose(); $script:Process = $null }
}

function Invoke-Install {
    param([string]$Setup=$Installer,[string[]]$ExtraArguments=@(),[switch]$ExpectFailure)
    Assert-NoReparsePointAncestors -Path $InstallDir
    Assert-NoReparsePointAncestors -Path $UserDataDir
    $script:FirewallMutationPossible=$true; $script:UserStateMutationPossible=$true
    $code=Invoke-BoundedProcess -FilePath $Setup -Arguments (@(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        "/DIR=$InstallDir", "/USERDATADIR=$UserDataDir"
    ) + $ExtraArguments) -TimeoutSeconds 300 -TimeoutMessage 'installer timed out' -AllowNonZero:$ExpectFailure
    if($ExpectFailure -and $code -eq 0){throw 'failure fixture unexpectedly succeeded'}
}

function Invoke-Uninstall {
    param([switch]$DeleteUserData)
    $uninstaller = Join-Path $InstallDir 'unins000.exe'
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
        throw 'registered uninstaller executable is missing'
    }
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        '/CLOSEAPPLICATIONS', '/FORCECLOSEAPPLICATIONS', "/USERDATADIR=$UserDataDir")
    if ($DeleteUserData) { $arguments += '/DELETEUSERDATA=1' }
    $script:FirewallMutationPossible=$true
    Invoke-BoundedProcess -FilePath $uninstaller -Arguments $arguments `
        -TimeoutSeconds 300 -TimeoutMessage 'uninstaller timed out' | Out-Null
}

function Backup-Shortcut {
    param([string]$Path,[string]$Backup,[ref]$Succeeded,[ref]$Hash)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $sourceHash=(Get-FileHash $Path -Algorithm SHA256).Hash
    Copy-Item -LiteralPath $Path -Destination $Backup
    $backupHash=(Get-FileHash $Backup -Algorithm SHA256).Hash
    if ($sourceHash -cne $backupHash) { throw 'shortcut backup hash verification failed' }
    $Hash.Value=$backupHash; $Succeeded.Value=$true
}

function Restore-ShortcutAtomically {
    param([string]$Path,[bool]$Existed,[bool]$BackupSucceeded,[string]$Backup,[string]$BackupHash)
    if (-not $UserStateMutationPossible) { return }
    if (-not $Existed) {
        if (Test-Path -LiteralPath $Path -PathType Leaf) { Remove-Item -LiteralPath $Path -Force }
        return
    }
    if (-not $BackupSucceeded -or -not (Test-Path -LiteralPath $Backup -PathType Leaf) -or
        (Get-FileHash $Backup -Algorithm SHA256).Hash -cne $BackupHash) {
        throw 'shortcut backup hash verification failed; preserving current shortcut'
    }
    $replacement=Join-Path (Split-Path -Parent $Path) ".restore-$([guid]::NewGuid().ToString('N')).lnk"
    try {
        Copy-Item -LiteralPath $Backup -Destination $replacement
        if ((Get-FileHash $replacement -Algorithm SHA256).Hash -cne $BackupHash) { throw 'backup hash verification failed' }
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($replacement,$Path,$null) }
        else { [IO.File]::Move($replacement,$Path) }
    }
    finally { if(Test-Path -LiteralPath $replacement){Remove-Item -LiteralPath $replacement -Force} }
}

Assert-SafeLifecyclePath -Path $SmokeRoot
if (-not (Test-Path -LiteralPath $Distribution -PathType Container)) {
    throw 'packaged distribution is missing; build the installer first'
}
if ($null -ne (Get-UninstallEntry)) {
    throw 'an existing Restream Studio uninstall registration would make this smoke non-isolated'
}

try {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    Backup-Shortcut $DesktopShortcut $DesktopBackup ([ref]$DesktopBackupSucceeded) ([ref]$DesktopBackupHash)
    Backup-Shortcut $StartMenuShortcut $StartMenuBackup ([ref]$StartMenuBackupSucceeded) ([ref]$StartMenuBackupHash)
    $InitialFirewall = @(Get-FirewallSnapshot)
    if ($InitialFirewall.Count -ne 0) {
        throw 'existing canonical/legacy firewall rules prevent isolated smoke'
    }
    $InitialFirewallFingerprint = ConvertTo-Json -InputObject @($InitialFirewall) -Depth 5 -Compress
    $SnapshotCaptured = $true

    foreach($mode in @('cancel','command')) {
        Invoke-Install -Setup $UpgradeInstaller -ExpectFailure -ExtraArguments @("/SMOKEFIREWALLFAIL=$mode")
        if((Test-Path -LiteralPath $InstallDir) -or $null -ne (Get-UninstallEntry)){throw 'failure fixture file rollback failed'}
        $now=ConvertTo-Json -InputObject @(Get-FirewallSnapshot) -Depth 5 -Compress
        if($now -cne $InitialFirewallFingerprint){throw 'failure fixture firewall rollback failed'}
    }

    Invoke-Install
    Assert-InstalledFiles
    Assert-Shortcuts
    Assert-UninstallRegistration $CurrentVersion | Out-Null
    Assert-FirewallRule
    Start-And-TestInstalledApplication $CurrentVersion
    Stop-InstalledApplication

    New-Item -ItemType Directory -Path $UserDataDir -Force | Out-Null
    $marker = Join-Path $UserDataDir 'installer-smoke-marker.json'
    [ordered]@{ marker = 'preserve-across-upgrade'; created_utc = [DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding utf8
    $markerHash = (Get-FileHash -LiteralPath $marker -Algorithm SHA256).Hash

    $program=Join-Path $InstallDir 'RestreamStudio.exe'
    $releaseProgramHash=(Get-FileHash (Join-Path $Distribution 'RestreamStudio.exe') -Algorithm SHA256).Hash
    $expectedProgramHash=(Get-FileHash (Join-Path $UpgradeDistribution 'RestreamStudio.exe') -Algorithm SHA256).Hash
    if($releaseProgramHash -ceq $expectedProgramHash){throw 'upgrade payload executable is not distinct'}
    $bytes=[IO.File]::ReadAllBytes($program); $bytes[0]=$bytes[0] -bxor 1; [IO.File]::WriteAllBytes($program,$bytes)
    $tamperedProgramHash=(Get-FileHash $program -Algorithm SHA256).Hash
    Invoke-Install -Setup $UpgradeInstaller
    Assert-InstalledFiles $UpgradeDistribution
    Assert-Shortcuts
    Assert-UninstallRegistration $UpgradeVersion | Out-Null
    Assert-FirewallRule
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf) -or
        (Get-FileHash -LiteralPath $marker -Algorithm SHA256).Hash -cne $markerHash) {
        throw 'isolated configuration marker was not preserved by version upgrade'
    }
    $upgradedProgramHash=(Get-FileHash $program -Algorithm SHA256).Hash
    if($upgradedProgramHash -cne $expectedProgramHash -or $upgradedProgramHash -ceq $tamperedProgramHash){throw 'program file was not replaced by upgrade'}

    Start-And-TestInstalledApplication $UpgradeVersion
    Invoke-Uninstall
    $Process.WaitForExit(15000) | Out-Null
    if (-not (Wait-LocalPortReleased -PortNumber $Port)) { throw 'uninstall did not release the API port' }
    $Process.Dispose(); $Process = $null
    if ((Test-Path -LiteralPath $InstallDir) -or $null -ne (Get-UninstallEntry) -or
        (Test-Path -LiteralPath $DesktopShortcut) -or
        (Test-Path -LiteralPath $StartMenuShortcut) -or
        (@(Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue)).Count -ne 0) {
        throw 'default uninstall left program, shortcut, registration, or firewall state behind'
    }
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        throw 'default uninstall did not preserve isolated user data'
    }

    Invoke-Install
    Assert-InstalledFiles
    Assert-UninstallRegistration $CurrentVersion | Out-Null
    Invoke-Uninstall -DeleteUserData
    if ((Test-Path -LiteralPath $UserDataDir) -or (Test-Path -LiteralPath $InstallDir) -or
        $null -ne (Get-UninstallEntry) -or
        (@(Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue)).Count -ne 0) {
        throw 'delete-data uninstall did not remove every isolated lifecycle artifact'
    }
    Write-Output "INSTALLER_LIFECYCLE_ROOT=$SmokeRoot"
    Write-Output 'INSTALLER_LIFECYCLE_RESULT=PASS'
}
finally {
    if ($LifecycleProcessStillRunning) {
        throw 'CRITICAL: installer or uninstaller process tree may still be running; destructive lifecycle cleanup was skipped; manual intervention required'
    }
    $cleanupErrors = [Collections.Generic.List[string]]::new()
    try { Stop-InstalledApplication } catch { $cleanupErrors.Add($_.Exception.Message) }
    try {
      $testEntry = Get-UninstallEntry
      if ($null -ne $testEntry -and $testEntry.InstallLocation -and
        (Test-PathInside -Path $testEntry.InstallLocation -Parent $SmokeParent)) {
        $uninstaller = Join-Path $testEntry.InstallLocation 'unins000.exe'
        if (Test-Path -LiteralPath $uninstaller -PathType Leaf) {
            try {
                Invoke-BoundedProcess -FilePath $uninstaller -Arguments @(
                    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                    "/USERDATADIR=$UserDataDir", '/DELETEUSERDATA=1'
                ) -TimeoutSeconds 300 -TimeoutMessage 'cleanup uninstaller timed out' | Out-Null
            }
            catch { $cleanupErrors.Add($_.Exception.Message) }
            if ($LifecycleProcessStillRunning) {
                throw 'CRITICAL: cleanup uninstaller may still be running; registry, firewall, shortcut, and tree cleanup are forbidden; manual intervention required'
            }
        }
        if ($null -ne (Get-UninstallEntry)) { Remove-IsolatedUninstallRegistration }
      }
    } catch { $cleanupErrors.Add("registry cleanup failed: $($_.Exception.Message)") }
    if ($LifecycleProcessStillRunning) {
        throw 'CRITICAL: cleanup uninstaller may still be running; destructive lifecycle cleanup was stopped; manual intervention required'
    }
    if ($SnapshotCaptured -and $FirewallMutationPossible) {
        try { Remove-IsolatedFirewallRules } catch { $cleanupErrors.Add("firewall cleanup failed: $($_.Exception.Message)") }
    }
    try { Restore-ShortcutAtomically $DesktopShortcut $DesktopExisted `
        $DesktopBackupSucceeded $DesktopBackup $DesktopBackupHash }
    catch { $cleanupErrors.Add("desktop shortcut restore failed: $($_.Exception.Message)") }
    try { Restore-ShortcutAtomically $StartMenuShortcut $StartMenuExisted `
        $StartMenuBackupSucceeded $StartMenuBackup $StartMenuBackupHash }
    catch { $cleanupErrors.Add("start menu shortcut restore failed: $($_.Exception.Message)") }
    try {
        if (-not $StartMenuGroupExisted -and (Test-Path -LiteralPath $StartMenuGroup -PathType Container) -and
            @(Get-ChildItem -LiteralPath $StartMenuGroup -Force).Count -eq 0) {
            Remove-Item -LiteralPath $StartMenuGroup -Force
        }
    }
    catch { $cleanupErrors.Add("start menu group cleanup failed: $($_.Exception.Message)") }
    try { Remove-SafeTree -Path $SmokeRoot } catch { $cleanupErrors.Add($_.Exception.Message) }
    if ($cleanupErrors.Count -gt 0) {
        throw "installer lifecycle cleanup failed: $($cleanupErrors -join '; ')"
    }
}
