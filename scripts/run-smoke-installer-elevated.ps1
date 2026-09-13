[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Installer,
    [Parameter(Mandatory)][string]$UpgradeInstaller,
    [string]$WorkspaceRoot = (Split-Path -Parent $PSScriptRoot)
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$payload = [ordered]@{
    script = (Resolve-Path (Join-Path $PSScriptRoot 'smoke-installer.ps1')).Path
    installer = (Resolve-Path $Installer).Path
    upgrade = (Resolve-Path $UpgradeInstaller).Path
    workspace = (Resolve-Path $WorkspaceRoot).Path
} | ConvertTo-Json -Compress
$payload64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))
$command = @"
`$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$payload64'))|ConvertFrom-Json
try {
    & `$p.script -Installer `$p.installer -UpgradeInstaller `$p.upgrade -WorkspaceRoot `$p.workspace
    exit 0
}
catch {
    Write-Error `$_.Exception.Message
    exit 1
}
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
$powershell = (Get-Command powershell.exe -CommandType Application -ErrorAction Stop).Source
$broker = Start-Process -FilePath $powershell -Verb RunAs -ArgumentList @(
    '-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded
) -Wait -PassThru
try { exit $broker.ExitCode } finally { $broker.Dispose() }
