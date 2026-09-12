[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$LockFile = Join-Path $Root 'package-lock.json'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'run: py -3.12 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e .[dev]'
}
if (-not (Test-Path -LiteralPath $LockFile -PathType Leaf)) {
    throw 'package-lock.json is required before starting the frontend'
}
$Npm = (Get-Command npm.cmd -CommandType Application -ErrorAction Stop).Source
$frontend = $null
$oldData = [Environment]::GetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR')
[Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR', (Join-Path $Root 'runtime'))

function Set-ToolEnvironment {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$EnvironmentName)
    if ([Environment]::GetEnvironmentVariable($EnvironmentName)) { return }
    $repositoryTool = Join-Path $Root "tools\ffmpeg\$Name.exe"
    if (Test-Path -LiteralPath $repositoryTool -PathType Leaf) {
        [Environment]::SetEnvironmentVariable($EnvironmentName, $repositoryTool)
        return
    }
    $command = Get-Command "$Name.exe" -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $command) {
        [Environment]::SetEnvironmentVariable($EnvironmentName, $command.Source)
    }
}

Set-ToolEnvironment -Name 'ffmpeg' -EnvironmentName 'FFMPEG_PATH'
Set-ToolEnvironment -Name 'ffprobe' -EnvironmentName 'FFPROBE_PATH'

Push-Location $Root
try {
    & $Npm ci --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
    $frontend = Start-Process -FilePath $Npm -ArgumentList @('--prefix', 'frontend', 'run', 'dev') `
        -WorkingDirectory $Root -PassThru -WindowStyle Hidden
    & $Python -m restream_studio.main
    if ($LASTEXITCODE -ne 0) { throw "backend exited with code $LASTEXITCODE" }
}
finally {
    if ($null -ne $frontend) {
        $frontend.Refresh()
        if (-not $frontend.HasExited) { Stop-Process -Id $frontend.Id -Force }
    }
    [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR', $oldData)
    Pop-Location
}
