[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$LockFile = Join-Path $Root 'package-lock.json'
$ViteScript = Join-Path $Root 'frontend\node_modules\vite\bin\vite.js'
$FrontendPort = 5173

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'run: py -3.12 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e .[dev]'
}
if (-not (Test-Path -LiteralPath $LockFile -PathType Leaf)) {
    throw 'package-lock.json is required before starting the frontend'
}
$Npm = (Get-Command npm.cmd -CommandType Application -ErrorAction Stop).Source
$Node = (Get-Command node.exe -CommandType Application -ErrorAction Stop).Source
$frontend = $null
$backend = $null
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

function Test-LocalTcpPort {
    param([Parameter(Mandatory)][int]$Port)
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.BeginConnect([Net.IPAddress]::Loopback, $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(250)) { return $false }
        $client.EndConnect($pending)
        return $true
    }
    catch { return $false }
    finally { $client.Dispose() }
}

function Stop-ExactProcess {
    param([Diagnostics.Process]$Process)
    if ($null -eq $Process) { return }
    $Process.Refresh()
    if (-not $Process.HasExited) { Stop-Process -Id $Process.Id -Force }
}

Set-ToolEnvironment -Name 'ffmpeg' -EnvironmentName 'FFMPEG_PATH'
Set-ToolEnvironment -Name 'ffprobe' -EnvironmentName 'FFPROBE_PATH'

Push-Location $Root
try {
    & $Npm ci --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
    if (-not (Test-Path -LiteralPath $ViteScript -PathType Leaf)) {
        throw 'Vite entry point is missing after npm ci'
    }
    $frontend = Start-Process -FilePath $Node `
        -ArgumentList @($ViteScript, '--host', '127.0.0.1', '--port', [string]$FrontendPort, '--strictPort') `
        -WorkingDirectory $Root -PassThru -NoNewWindow

    $readiness = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-LocalTcpPort -Port $FrontendPort)) {
        $frontend.Refresh()
        if ($frontend.HasExited) { throw "Vite exited before readiness with code $($frontend.ExitCode)" }
        if ($readiness.Elapsed.TotalSeconds -ge 15) { throw 'Vite readiness timed out' }
        Start-Sleep -Milliseconds 100
    }
    Start-Sleep -Seconds 1
    $frontend.Refresh()
    if ($frontend.HasExited) { throw "Vite exited before readiness with code $($frontend.ExitCode)" }

    $backend = Start-Process -FilePath $Python -ArgumentList @('-m', 'restream_studio.main') `
        -WorkingDirectory $Root -PassThru -NoNewWindow
    while ($true) {
        $frontend.Refresh()
        $backend.Refresh()
        if ($frontend.HasExited) { throw "Vite exited with code $($frontend.ExitCode)" }
        if ($backend.HasExited) { throw "backend exited with code $($backend.ExitCode)" }
        Start-Sleep -Milliseconds 250
    }
}
finally {
    Stop-ExactProcess -Process $frontend
    Stop-ExactProcess -Process $backend
    [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR', $oldData)
    Pop-Location
}
