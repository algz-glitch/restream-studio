[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ExecutablePath,
    [switch]$Elevated
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RuleName = 'RestreamStudio-Installed-Localhost'
$LegacyDisplayName = 'Restream Studio (Loopback TCP)'
$resolvedExecutable = [IO.Path]::GetFullPath($ExecutablePath)

if (-not [IO.Path]::IsPathFullyQualified($resolvedExecutable) -or
    [IO.Path]::GetFileName($resolvedExecutable) -ine 'RestreamStudio.exe') {
    throw 'firewall target must be an absolute RestreamStudio.exe path'
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdministrator) {
    if ($Elevated) { throw 'administrator elevation was not granted' }
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"",
        '-ExecutablePath', "`"$resolvedExecutable`"", '-Elevated'
    )
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments `
        -Verb RunAs -Wait -PassThru
    try {
        if ($process.ExitCode -ne 0) {
            throw "elevated firewall removal failed with exit code $($process.ExitCode)"
        }
    }
    finally { $process.Dispose() }
    exit 0
}

$candidates = @(
    @(Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue) +
    @(Get-NetFirewallRule -DisplayName $LegacyDisplayName -ErrorAction SilentlyContinue)
) | Sort-Object -Property Name -Unique
foreach ($rule in $candidates) {
    try { $application = $rule | Get-NetFirewallApplicationFilter -ErrorAction Stop }
    catch { continue }
    if ($application.Program -ieq $resolvedExecutable) {
        $rule | Remove-NetFirewallRule -ErrorAction Stop
    }
}

$remaining = @(
    @(Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue) +
    @(Get-NetFirewallRule -DisplayName $LegacyDisplayName -ErrorAction SilentlyContinue)
) | Sort-Object -Property Name -Unique
foreach ($rule in $remaining) {
    try { $application = $rule | Get-NetFirewallApplicationFilter -ErrorAction Stop }
    catch { continue }
    if ($application.Program -ieq $resolvedExecutable) {
        throw 'firewall rule removal verification failed'
    }
}
