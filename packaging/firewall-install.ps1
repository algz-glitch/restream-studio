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
            throw "elevated firewall installation failed with exit code $($process.ExitCode)"
        }
    }
    finally { $process.Dispose() }
    exit 0
}

$hadValid = $false
foreach ($candidate in @(Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue)) {
    $candidateApplication = $candidate | Get-NetFirewallApplicationFilter
    $candidatePort = $candidate | Get-NetFirewallPortFilter
    $candidateAddress = $candidate | Get-NetFirewallAddressFilter
    if (
        $candidate.Direction -eq 'Inbound' -and $candidate.Action -eq 'Allow' -and
        $candidate.Enabled -eq 'True' -and
        $candidateApplication.Program -ieq $resolvedExecutable -and
        $candidatePort.Protocol -eq 'TCP' -and
        @($candidateAddress.LocalAddress).Count -eq 1 -and
        @($candidateAddress.LocalAddress)[0] -eq '127.0.0.1' -and
        @($candidateAddress.RemoteAddress).Count -eq 1 -and
        @($candidateAddress.RemoteAddress)[0] -eq '127.0.0.1'
    ) { $hadValid = $true }
}

try {
    Get-NetFirewallRule -DisplayName $LegacyDisplayName -ErrorAction SilentlyContinue |
        Where-Object Name -ne $RuleName | Remove-NetFirewallRule -ErrorAction Stop
    Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction Stop
    New-NetFirewallRule -Name $RuleName -DisplayName 'Restream Studio Installed localhost' `
        -Direction Inbound -Action Allow -Enabled True -Program $resolvedExecutable `
        -Protocol TCP -LocalAddress '127.0.0.1' -RemoteAddress '127.0.0.1' `
        -Profile Any -EdgeTraversalPolicy Block | Out-Null

    $rules = @(Get-NetFirewallRule -Name $RuleName -ErrorAction Stop)
    if ($rules.Count -ne 1) { throw 'firewall rule count verification failed' }
    $rule = $rules[0]
    $application = $rule | Get-NetFirewallApplicationFilter
    $port = $rule | Get-NetFirewallPortFilter
    $address = $rule | Get-NetFirewallAddressFilter
    $exact = (
        $rule.Direction -eq 'Inbound' -and $rule.Action -eq 'Allow' -and
        $rule.Enabled -eq 'True' -and
        $application.Program -ieq $resolvedExecutable -and
        $port.Protocol -eq 'TCP' -and
        @($address.LocalAddress).Count -eq 1 -and
        @($address.LocalAddress)[0] -eq '127.0.0.1' -and
        @($address.RemoteAddress).Count -eq 1 -and
        @($address.RemoteAddress)[0] -eq '127.0.0.1'
    )
    if (-not $exact) { throw 'firewall rule scope verification failed' }
}
catch {
    Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule
    if ($hadValid) {
        New-NetFirewallRule -Name $RuleName `
            -DisplayName 'Restream Studio Installed localhost' `
            -Direction Inbound -Action Allow -Enabled True -Program $resolvedExecutable `
            -Protocol TCP -LocalAddress '127.0.0.1' -RemoteAddress '127.0.0.1' `
            -Profile Any -EdgeTraversalPolicy Block | Out-Null
    }
    throw
}
