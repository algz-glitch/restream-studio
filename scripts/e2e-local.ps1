[CmdletBinding()]
param(
    [ValidateRange(61, 120)]
    [int]$StandbyAfterSeconds = 61,
    [ValidateRange(120, 600)]
    [int]$OverallTimeoutSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$ArtifactDirectory = Join-Path $Root 'artifacts\e2e-local'
$ResultFile = Join-Path $ArtifactDirectory 'result.json'
$MediaMtxConfig = Join-Path $Root 'tools\mediamtx\mediamtx.yml'
$Harness = Join-Path $PSScriptRoot 'e2e_local_harness.py'
$RunStarted = [DateTimeOffset]::UtcNow
$Timer = [Diagnostics.Stopwatch]::StartNew()
$MediaMtx = $null

New-Item -ItemType Directory -Force -Path $ArtifactDirectory | Out-Null
Remove-Item -LiteralPath $ResultFile -Force -ErrorAction SilentlyContinue

function Resolve-Tool {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$EnvironmentName,
        [Parameter(Mandatory)][string[]]$RepositoryCandidates
    )

    $fromEnvironment = [Environment]::GetEnvironmentVariable($EnvironmentName)
    if ($fromEnvironment -and (Test-Path -LiteralPath $fromEnvironment -PathType Leaf)) {
        return (Resolve-Path -LiteralPath $fromEnvironment).Path
    }
    foreach ($candidate in $RepositoryCandidates) {
        $path = Join-Path $Root $candidate
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            return (Resolve-Path -LiteralPath $path).Path
        }
    }
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $command) {
        return $command.Source
    }
    throw "required tool is unavailable: $Name"
}

function Resolve-MediaMtx {
    try {
        return Resolve-Tool -Name 'mediamtx' -EnvironmentName 'MEDIAMTX_PATH' `
            -RepositoryCandidates @('tools\mediamtx\mediamtx.exe', 'bin\mediamtx.exe')
    }
    catch {
        throw 'required tool is unavailable: mediamtx'
    }
}

function Test-PortAvailable {
    param([Parameter(Mandatory)][int]$Port)
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try {
        $listener.Start()
    }
    catch {
        throw "required localhost port is already in use: $Port"
    }
    finally {
        $listener.Stop()
    }
}

function Wait-MediaMtx {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt 20) {
        try {
            $response = Invoke-RestMethod -Method Get `
                -Uri 'http://127.0.0.1:9997/v3/paths/list' -TimeoutSec 2
            if ($null -ne $response) { return }
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    throw 'MediaMTX API readiness timed out'
}

function ConvertTo-SafeMessage {
    param([Parameter(Mandatory)][string]$Message)
    $safe = $Message -replace 'rtmps?://\S+', '[local-stream]'
    return $safe -replace [Regex]::Escape($Root), '[workspace]'
}

function Write-BootstrapFailure {
    param([Parameter(Mandatory)][string]$Message)
    $result = [ordered]@{
        schema_version = 2
        status = 'failed'
        started_at_utc = $RunStarted.ToString('o')
        completed_at_utc = [DateTimeOffset]::UtcNow.ToString('o')
        duration_seconds = [Math]::Round($Timer.Elapsed.TotalSeconds, 3)
        paths = @('source/main', 'target/douyin', 'target/wechat')
        checks = @()
        application_snapshots = @()
        target_probes = @()
        error = ConvertTo-SafeMessage -Message $Message
    }
    $result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ResultFile -Encoding UTF8
}

try {
    $Ffmpeg = Resolve-Tool -Name 'ffmpeg' -EnvironmentName 'FFMPEG_PATH' `
        -RepositoryCandidates @('tools\ffmpeg\ffmpeg.exe', 'bin\ffmpeg.exe')
    $Ffprobe = Resolve-Tool -Name 'ffprobe' -EnvironmentName 'FFPROBE_PATH' `
        -RepositoryCandidates @('tools\ffmpeg\ffprobe.exe', 'bin\ffprobe.exe')
    $Python = Resolve-Tool -Name 'python' -EnvironmentName 'PYTHON_PATH' `
        -RepositoryCandidates @('.venv\Scripts\python.exe')
    $MediaMtxExecutable = Resolve-MediaMtx
    if (-not (Test-Path -LiteralPath $MediaMtxConfig -PathType Leaf)) {
        throw 'MediaMTX configuration is missing'
    }
    if (-not (Test-Path -LiteralPath $Harness -PathType Leaf)) {
        throw 'Python E2E harness is missing'
    }
    Test-PortAvailable -Port 1935
    Test-PortAvailable -Port 9997

    $token = [Guid]::NewGuid().ToString('N')
    $stdout = Join-Path $ArtifactDirectory "mediamtx-$token.stdout.log"
    $stderr = Join-Path $ArtifactDirectory "mediamtx-$token.stderr.log"
    $MediaMtx = Start-Process -FilePath $MediaMtxExecutable -ArgumentList @($MediaMtxConfig) `
        -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    Wait-MediaMtx

    & $Python $Harness `
        --ffmpeg $Ffmpeg `
        --ffprobe $Ffprobe `
        --result $ResultFile `
        --standby-after-seconds $StandbyAfterSeconds `
        --overall-timeout-seconds $OverallTimeoutSeconds
    if ($LASTEXITCODE -ne 0) {
        throw "application E2E harness failed with exit code $LASTEXITCODE"
    }
}
catch {
    if (-not (Test-Path -LiteralPath $ResultFile -PathType Leaf)) {
        Write-BootstrapFailure -Message $_.Exception.Message
    }
    Write-Error $_.Exception.Message
    exit 1
}
finally {
    if ($null -ne $MediaMtx) {
        $MediaMtx.Refresh()
        if (-not $MediaMtx.HasExited) {
            Stop-Process -Id $MediaMtx.Id -Force -ErrorAction SilentlyContinue
            [void]$MediaMtx.WaitForExit(5000)
        }
    }
}

Write-Output 'Local RTMP acceptance passed. Evidence: artifacts/e2e-local/result.json'
