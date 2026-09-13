[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Installer,
    [Parameter(Mandatory)][string]$UpgradeInstaller,
    [string]$WorkspaceRoot = (Split-Path -Parent $PSScriptRoot)
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$smokeScript = (Resolve-Path (Join-Path $PSScriptRoot 'smoke-installer.ps1')).Path
$resolvedInstaller = (Resolve-Path -LiteralPath $Installer).Path
$resolvedUpgradeInstaller = (Resolve-Path -LiteralPath $UpgradeInstaller).Path
$resolvedWorkspace = (Resolve-Path -LiteralPath $WorkspaceRoot).Path
$payload = [ordered]@{
    script = $smokeScript
    installer = $resolvedInstaller
    upgrade = $resolvedUpgradeInstaller
    workspace = $resolvedWorkspace
} | ConvertTo-Json -Compress
$payload64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))
if (Test-IsAdministrator) {
    try {
        & $smokeScript -Installer $resolvedInstaller `
            -UpgradeInstaller $resolvedUpgradeInstaller -WorkspaceRoot $resolvedWorkspace
        exit 0
    }
    catch {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }
}
$command = @"
`$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$payload64'))|ConvertFrom-Json
try {
    & `$p.script -Installer `$p.installer -UpgradeInstaller `$p.upgrade -WorkspaceRoot `$p.workspace
    exit 0
}
catch {
    [Console]::Error.WriteLine(`$_.Exception.Message)
    exit 1
}
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
$powershell = (Get-Command powershell.exe -CommandType Application -ErrorAction Stop).Source
$broker = Start-Process -FilePath $powershell -Verb RunAs -ArgumentList @(
    '-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded
) -Wait -PassThru
try { exit $broker.ExitCode } finally { $broker.Dispose() }
