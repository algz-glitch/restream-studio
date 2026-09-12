[CmdletBinding()]
param([switch]$Elevated)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Executable = Join-Path $PSScriptRoot 'RestreamStudio.exe'
$RuleName = 'RestreamStudio-Installed-Localhost'

if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "RestreamStudio.exe is missing beside this setup script: $Executable"
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdministrator) {
    if ($Elevated) { throw 'Administrator elevation was not granted' }
    $escapedScript = $PSCommandPath.Replace('"', '""')
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$escapedScript`" -Elevated"
    $process = Start-Process -FilePath 'powershell.exe' -Verb RunAs `
        -ArgumentList $arguments -PassThru -Wait
    exit $process.ExitCode
}

$resolvedExecutable = (Resolve-Path -LiteralPath $Executable).Path
Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule -ErrorAction Stop
New-NetFirewallRule -Name $RuleName -DisplayName 'Restream Studio localhost only' `
    -Description 'Allows only this Restream Studio executable to use IPv4 loopback TCP.' `
    -Enabled True -Profile Any -Direction Inbound -Action Allow -Protocol TCP `
    -Program $resolvedExecutable -LocalAddress '127.0.0.1' -RemoteAddress '127.0.0.1' |
    Out-Null

$rule = Get-NetFirewallRule -Name $RuleName -ErrorAction Stop
$application = $rule | Get-NetFirewallApplicationFilter
$address = $rule | Get-NetFirewallAddressFilter
if (
    $rule.Enabled -ne 'True' -or
    $rule.Direction -ne 'Inbound' -or
    $rule.Action -ne 'Allow' -or
    $application.Program -ne $resolvedExecutable -or
    $address.LocalAddress -notcontains '127.0.0.1' -or
    $address.RemoteAddress -notcontains '127.0.0.1'
) {
    throw 'localhost firewall rule verification failed'
}

Write-Output "LOCALHOST_RULE=PASS"
Write-Output "PROGRAM=$resolvedExecutable"
