[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Package = Join-Path $PSScriptRoot 'package.ps1'
$Distribution = Join-Path $Root 'dist\RestreamStudio'

function Invoke-External {
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$Arguments = @())
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$FilePath exited with code $LASTEXITCODE" }
}

function Invoke-Gate {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][scriptblock]$Action)
    try {
        & $Action
        Write-Output "$Name=PASS"
    }
    catch {
        Write-Output "$Name=FAIL"
        [Console]::Error.WriteLine("$Name failed: $($_.Exception.Message)")
        exit 1
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

function Assert-Distribution {
    $required = @(
        'RestreamStudio.exe',
        '_internal\restream_studio\static\index.html',
        '_internal\ffmpeg.exe',
        '_internal\ffprobe.exe',
        '_internal\defaults\default-standby.mp4',
        '_internal\licenses\THIRD-PARTY-NOTICES.txt',
        '_internal\licenses\FFmpeg-LICENSE.txt',
        '_internal\metadata\README.md',
        '_internal\metadata\pyproject.toml',
        '_internal\metadata\package.json',
        '_internal\metadata\package-lock.json'
    )
    foreach ($relative in $required) {
        if (-not (Test-Path -LiteralPath (Join-Path $Distribution $relative) -PathType Leaf)) {
            throw "distribution file is missing: $relative"
        }
    }
    $forbidden = Get-ChildItem -LiteralPath $Distribution -Recurse -Force | Where-Object {
        $relative = $_.FullName.Substring($Distribution.Length).TrimStart('\')
        $relative -match '(^|\\)(fixtures|cookies?|stream-keys?|\.git)(\\|$)' -or
        $_.Name -like '.env*' -or $_.Name -like '*.sqlite3' -or
        $_.Name -like '*.db' -or $_.Name -like '*.log' -or $_.Name -like '*.key'
    }
    if ($forbidden) {
        throw "forbidden distribution content: $($forbidden.FullName -join ', ')"
    }
}

function Test-PackageHealth {
    $executable = Join-Path $Distribution 'RestreamStudio.exe'
    $port = Get-DynamicPort
    $runtime = Join-Path $Root "artifacts\package-smoke-$([Guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Force -Path $runtime | Out-Null
    $stdout = Join-Path $runtime 'stdout.txt'
    $stderr = Join-Path $runtime 'stderr.txt'
    $oldPort = [Environment]::GetEnvironmentVariable('RESTREAM_STUDIO_PORT')
    $oldData = [Environment]::GetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR')
    $process = $null
    try {
        [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_PORT', [string]$port)
        [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR', (Join-Path $runtime 'data'))
        $process = Start-Process -FilePath $executable -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while ($timer.Elapsed.TotalSeconds -lt 30) {
            $process.Refresh()
            if ($process.HasExited) { throw "packaged executable exited with code $($process.ExitCode)" }
            try {
                $health = Invoke-RestMethod -Method Get `
                    -Uri "http://127.0.0.1:$port/health" -TimeoutSec 2
                if ($health.status -eq 'ok') { return }
            }
            catch { Start-Sleep -Milliseconds 250 }
        }
        throw 'packaged /health readiness timed out'
    }
    finally {
        [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_PORT', $oldPort)
        [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR', $oldData)
        if ($null -ne $process) {
            $process.Refresh()
            if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
        }
    }
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Write-Output 'PYTHON_LINT=FAIL'
    [Console]::Error.WriteLine('Python virtual environment is missing')
    exit 1
}
$NpmCommand = Get-Command npm.cmd -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -eq $NpmCommand) {
    Write-Output 'FRONTEND_DEPS=FAIL'
    [Console]::Error.WriteLine('npm is unavailable')
    exit 1
}
$Npm = $NpmCommand.Source

Push-Location $Root
try {
    Invoke-Gate -Name 'FRONTEND_DEPS' -Action {
        if (-not (Test-Path -LiteralPath (Join-Path $Root 'package-lock.json') -PathType Leaf)) {
            throw 'package-lock.json is missing; npm dependency closure is not reproducible'
        }
        Invoke-External $Npm @('ci', '--ignore-scripts', '--no-audit', '--no-fund')
    }
    Invoke-Gate -Name 'PYTHON_LINT' -Action {
        Invoke-External $Python @('-m', 'ruff', 'check', 'src', 'tests', 'scripts')
    }
    Invoke-Gate -Name 'PYTHON_TYPES' -Action {
        Invoke-External $Python @('-m', 'mypy', 'src', 'tests')
    }
    Invoke-Gate -Name 'PYTHON_TESTS' -Action {
        Invoke-External $Python @('-m', 'pytest', 'tests')
    }
    Invoke-Gate -Name 'FRONTEND_TYPES' -Action { Invoke-External $Npm @('run', 'typecheck') }
    Invoke-Gate -Name 'FRONTEND_LINT' -Action { Invoke-External $Npm @('run', 'lint') }
    Invoke-Gate -Name 'FRONTEND_TESTS' -Action { Invoke-External $Npm @('test') }
    Invoke-Gate -Name 'FRONTEND_BUILD' -Action { Invoke-External $Npm @('run', 'frontend:build') }
    Invoke-Gate -Name 'LOCAL_RTMP_E2E' -Action {
        $PowerShell = (Get-Command powershell.exe -CommandType Application -ErrorAction Stop).Source
        Invoke-External $PowerShell @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            (Join-Path $PSScriptRoot 'e2e-local.ps1')
        )
    }
    Invoke-Gate -Name 'PYINSTALLER_PACKAGE' -Action {
        & $Package -Clean
        if ($LASTEXITCODE -ne 0) { throw "package script exited with code $LASTEXITCODE" }
    }
    Invoke-Gate -Name 'PACKAGE_CONTENTS' -Action { Assert-Distribution }
    Invoke-Gate -Name 'PACKAGE_SMOKE' -Action { Test-PackageHealth }
}
finally { Pop-Location }
