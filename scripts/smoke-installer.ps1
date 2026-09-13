[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Installer,
    [string]$WorkspaceRoot = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = if ($WorkspaceRoot) {
    [IO.Path]::GetFullPath($WorkspaceRoot)
}
else {
    Split-Path -Parent $PSScriptRoot
}
$Installer = (Resolve-Path -LiteralPath $Installer -ErrorAction Stop).Path
$SmokeParent = Join-Path $Root 'artifacts\smoke'
$SmokeRoot = Join-Path $SmokeParent "RestreamStudioInstallerSmoke-$([Guid]::NewGuid().ToString('N'))"
$InstallDir = Join-Path $SmokeRoot 'app'
$UserDataDir = Join-Path $SmokeRoot 'data'
$BackupDir = Join-Path $SmokeRoot 'backup'
$Distribution = Join-Path $Root 'dist\RestreamStudio'
$RuleName = 'RestreamStudio-Installed-Localhost'
$LegacyRuleDisplayName = 'Restream Studio (Loopback TCP)'
$UninstallRegistryPath = 'Software\Microsoft\Windows\CurrentVersion\Uninstall'
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'Restream Studio.lnk'
$StartMenuGroup = Join-Path ([Environment]::GetFolderPath('Programs')) 'Restream Studio'
$StartMenuShortcut = Join-Path $StartMenuGroup 'Restream Studio.lnk'
$Process = $null
$Port = 0
$InitialFirewall = @()
$InitialFirewallFingerprint = ''
$DesktopBackup = Join-Path $BackupDir 'desktop.lnk'
$StartMenuBackup = Join-Path $BackupDir 'start-menu.lnk'
$DesktopExisted = Test-Path -LiteralPath $DesktopShortcut -PathType Leaf
$StartMenuExisted = Test-Path -LiteralPath $StartMenuShortcut -PathType Leaf
$StartMenuGroupExisted = Test-Path -LiteralPath $StartMenuGroup -PathType Container

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-PathInside {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Parent)
    $fullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $fullParent = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    return $fullPath.Length -gt $fullParent.Length -and
        $fullPath.StartsWith("$fullParent\", [StringComparison]::OrdinalIgnoreCase)
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
}

function Remove-SafeTree {
    param([Parameter(Mandatory)][string]$Path)
    Assert-SafeLifecyclePath -Path $Path
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Invoke-NativeChecked {
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$Arguments = @())
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$FilePath exited with code $LASTEXITCODE" }
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
            $uninstall = $base.OpenSubKey($UninstallRegistryPath)
            if ($null -eq $uninstall) { continue }
            try {
                foreach ($name in $uninstall.GetSubKeyNames()) {
                    $key = $uninstall.OpenSubKey($name)
                    if ($null -eq $key) { continue }
                    try {
                        if ($key.GetValue('DisplayName') -ceq 'Restream Studio') {
                            return [pscustomobject]@{
                                View = $view
                                Name = $name
                                InstallLocation = [string]$key.GetValue('InstallLocation')
                                UninstallString = [string]$key.GetValue('UninstallString')
                            }
                        }
                    }
                    finally { $key.Dispose() }
                }
            }
            finally { $uninstall.Dispose() }
        }
        finally { $base.Dispose() }
    }
    return $null
}

function Get-ShortcutTarget {
    param([Parameter(Mandatory)][string]$Path)
    $shell = New-Object -ComObject WScript.Shell
    try { return [IO.Path]::GetFullPath($shell.CreateShortcut($Path).TargetPath) }
    finally { [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) | Out-Null }
}

function Get-FirewallSnapshot {
    $rules = @(@(Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue) +
        @(Get-NetFirewallRule -DisplayName $LegacyRuleDisplayName -ErrorAction SilentlyContinue) |
        Sort-Object Name -Unique)
    return @($rules | ForEach-Object {
        $application = $_ | Get-NetFirewallApplicationFilter -ErrorAction Stop
        $portFilter = $_ | Get-NetFirewallPortFilter -ErrorAction Stop
        $address = $_ | Get-NetFirewallAddressFilter -ErrorAction Stop
        [pscustomobject]@{
            Name = [string]$_.Name; DisplayName = [string]$_.DisplayName
            Description = [string]$_.Description; Group = [string]$_.Group
            Enabled = [string]$_.Enabled; Profile = [string]$_.Profile
            Direction = [string]$_.Direction; Action = [string]$_.Action
            EdgeTraversalPolicy = [string]$_.EdgeTraversalPolicy
            Program = [string]$application.Program; Protocol = [string]$portFilter.Protocol
            LocalPort = @($portFilter.LocalPort); RemotePort = @($portFilter.RemotePort)
            LocalAddress = @($address.LocalAddress); RemoteAddress = @($address.RemoteAddress)
        }
    })
}

function Remove-SmokeFirewallRules {
    @(Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue) |
        Remove-NetFirewallRule -ErrorAction Stop
    @(Get-NetFirewallRule -DisplayName $LegacyRuleDisplayName -ErrorAction SilentlyContinue) |
        Remove-NetFirewallRule -ErrorAction Stop
}

function Restore-FirewallSnapshot {
    Remove-SmokeFirewallRules
    foreach ($rule in $InitialFirewall) {
        $arguments = @{
            Name = $rule.Name; DisplayName = $rule.DisplayName; Enabled = $rule.Enabled
            Profile = $rule.Profile; Direction = $rule.Direction; Action = $rule.Action
            EdgeTraversalPolicy = $rule.EdgeTraversalPolicy; Program = $rule.Program
            Protocol = $rule.Protocol; LocalPort = $rule.LocalPort; RemotePort = $rule.RemotePort
            LocalAddress = $rule.LocalAddress; RemoteAddress = $rule.RemoteAddress
        }
        if ($rule.Description) { $arguments.Description = $rule.Description }
        if ($rule.Group) { $arguments.Group = $rule.Group }
        New-NetFirewallRule @arguments -ErrorAction Stop | Out-Null
    }
    $restored = @(Get-FirewallSnapshot) | ConvertTo-Json -Depth 5 -Compress
    if ($restored -cne $InitialFirewallFingerprint) {
        throw 'targeted firewall state was not restored exactly'
    }
}

function Assert-InstalledFiles {
    $sourceFiles = @(Get-ChildItem -LiteralPath $Distribution -File -Recurse)
    if ($sourceFiles.Count -eq 0) { throw 'packaged distribution is empty' }
    foreach ($source in $sourceFiles) {
        $relative = $source.FullName.Substring($Distribution.Length).TrimStart('\')
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
    $entry = Get-UninstallEntry
    if ($null -eq $entry) { throw 'HKCU uninstall registration is missing' }
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
    Invoke-NativeChecked -FilePath $Installer -Arguments @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        "/DIR=$InstallDir", "/USERDATADIR=$UserDataDir"
    )
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
    Invoke-NativeChecked -FilePath $uninstaller -Arguments $arguments
}

function Restore-Shortcut {
    param([string]$Path, [bool]$Existed, [string]$Backup)
    if (Test-Path -LiteralPath $Path -PathType Leaf) { Remove-Item -LiteralPath $Path -Force }
    if ($Existed) { Copy-Item -LiteralPath $Backup -Destination $Path -Force }
}

Assert-SafeLifecyclePath -Path $SmokeRoot
if (-not (Test-IsAdministrator)) {
    throw 'installer lifecycle smoke requires an elevated PowerShell session for real firewall checks'
}
if (-not (Test-Path -LiteralPath $Distribution -PathType Container)) {
    throw 'packaged distribution is missing; build the installer first'
}
if ($null -ne (Get-UninstallEntry)) {
    throw 'an existing Restream Studio uninstall registration would make this smoke non-isolated'
}

try {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    if ($DesktopExisted) { Copy-Item -LiteralPath $DesktopShortcut -Destination $DesktopBackup }
    if ($StartMenuExisted) { Copy-Item -LiteralPath $StartMenuShortcut -Destination $StartMenuBackup }
    $InitialFirewall = @(Get-FirewallSnapshot)
    $InitialFirewallFingerprint = $InitialFirewall | ConvertTo-Json -Depth 5 -Compress

    Invoke-Install
    Assert-InstalledFiles
    Assert-Shortcuts
    Assert-UninstallRegistration | Out-Null
    Assert-FirewallRule
    Start-And-TestInstalledApplication
    Stop-InstalledApplication

    New-Item -ItemType Directory -Path $UserDataDir -Force | Out-Null
    $marker = Join-Path $UserDataDir 'installer-smoke-marker.json'
    [ordered]@{ marker = 'preserve-across-upgrade'; created_utc = [DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding utf8
    $markerHash = (Get-FileHash -LiteralPath $marker -Algorithm SHA256).Hash

    Invoke-Install
    Assert-InstalledFiles
    Assert-Shortcuts
    Assert-UninstallRegistration | Out-Null
    Assert-FirewallRule
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf) -or
        (Get-FileHash -LiteralPath $marker -Algorithm SHA256).Hash -cne $markerHash) {
        throw 'isolated configuration marker was not preserved by the same-version upgrade'
    }

    Start-And-TestInstalledApplication
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
    Assert-UninstallRegistration | Out-Null
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
    $cleanupErrors = [Collections.Generic.List[string]]::new()
    try { Stop-InstalledApplication } catch { $cleanupErrors.Add($_.Exception.Message) }
    $testEntry = Get-UninstallEntry
    if ($null -ne $testEntry -and $testEntry.InstallLocation -and
        (Test-PathInside -Path $testEntry.InstallLocation -Parent $SmokeParent)) {
        $uninstaller = Join-Path $testEntry.InstallLocation 'unins000.exe'
        if (Test-Path -LiteralPath $uninstaller -PathType Leaf) {
            try {
                Invoke-NativeChecked -FilePath $uninstaller -Arguments @(
                    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                    "/USERDATADIR=$UserDataDir", '/DELETEUSERDATA=1'
                )
            }
            catch { $cleanupErrors.Add($_.Exception.Message) }
        }
    }
    try { Restore-FirewallSnapshot } catch { $cleanupErrors.Add("firewall restore failed: $($_.Exception.Message)") }
    try {
        Restore-Shortcut -Path $DesktopShortcut -Existed $DesktopExisted -Backup $DesktopBackup
        Restore-Shortcut -Path $StartMenuShortcut -Existed $StartMenuExisted -Backup $StartMenuBackup
        if (-not $StartMenuGroupExisted -and (Test-Path -LiteralPath $StartMenuGroup -PathType Container) -and
            @(Get-ChildItem -LiteralPath $StartMenuGroup -Force).Count -eq 0) {
            Remove-Item -LiteralPath $StartMenuGroup -Force
        }
    }
    catch { $cleanupErrors.Add("shortcut restore failed: $($_.Exception.Message)") }
    try { Remove-SafeTree -Path $SmokeRoot } catch { $cleanupErrors.Add($_.Exception.Message) }
    if ($cleanupErrors.Count -gt 0) {
        throw "installer lifecycle cleanup failed: $($cleanupErrors -join '; ')"
    }
}
