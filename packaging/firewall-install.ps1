[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ExecutablePath,
    [switch]$Elevated
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RuleName = 'Restream Studio (Loopback TCP)'
$resolvedExecutable = [IO.Path]::GetFullPath($ExecutablePath)

if ([IO.Path]::GetFileName($resolvedExecutable) -ine 'RestreamStudio.exe') {
    throw 'firewall target must be RestreamStudio.exe'
}
if (-not (Test-Path -LiteralPath $resolvedExecutable -PathType Leaf)) {
    throw "firewall target is missing: $resolvedExecutable"
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
    if ($process.ExitCode -ne 0) {
        throw "elevated firewall installation failed with exit code $($process.ExitCode)"
    }
    exit 0
}

Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue |
    Where-Object {
        $application = $_ | Get-NetFirewallApplicationFilter
        $address = $_ | Get-NetFirewallAddressFilter
        $application.Program -ieq $resolvedExecutable -and
        $address.LocalAddress -contains '127.0.0.1' -and
        $address.RemoteAddress -contains '127.0.0.1'
    } |
    Remove-NetFirewallRule

New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow `
    -Program $resolvedExecutable -Protocol TCP -LocalAddress '127.0.0.1' `
    -RemoteAddress '127.0.0.1' -Profile Any -EdgeTraversalPolicy Block | Out-Null
