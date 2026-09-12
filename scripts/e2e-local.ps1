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
$TrackedProcesses = [System.Collections.Generic.List[object]]::new()
$Checks = [System.Collections.Generic.List[object]]::new()
$Failure = $null
$Passed = $false
$RunStarted = [DateTimeOffset]::UtcNow
$Deadline = [Diagnostics.Stopwatch]::StartNew()

New-Item -ItemType Directory -Force -Path $ArtifactDirectory | Out-Null

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

function Start-TrackedProcess {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments
    )
    $token = [Guid]::NewGuid().ToString('N')
    $stdout = Join-Path $ArtifactDirectory "$Label-$token.stdout.log"
    $stderr = Join-Path $ArtifactDirectory "$Label-$token.stderr.log"
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -PassThru `
        -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $record = [pscustomobject]@{
        Label = $Label
        Id = $process.Id
        Process = $process
        Stdout = $stdout
        Stderr = $stderr
    }
    $TrackedProcesses.Add($record)
    return $record
}

function Stop-TrackedProcess {
    param([AllowNull()][object]$Record)
    if ($null -eq $Record) { return }
    $process = $Record.Process
    $process.Refresh()
    if (-not $process.HasExited) {
        Stop-Process -Id $Record.Id -Force -ErrorAction SilentlyContinue
        [void]$process.WaitForExit(5000)
    }
}

function Wait-Condition {
    param(
        [Parameter(Mandatory)][string]$Label,
        [Parameter(Mandatory)][scriptblock]$Condition,
        [int]$TimeoutSeconds = 30,
        [int]$PollMilliseconds = 500
    )
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $lastError = $null
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if ($Deadline.Elapsed.TotalSeconds -ge $OverallTimeoutSeconds) {
            throw "overall acceptance timeout reached while waiting for $Label"
        }
        try {
            if (& $Condition) { return }
        }
        catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Milliseconds $PollMilliseconds
    }
    if ($lastError) {
        throw "condition timed out: $Label ($lastError)"
    }
    throw "condition timed out: $Label"
}

function Invoke-MediaMtxApi {
    return Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:9997/v3/paths/list' `
        -TimeoutSec 2 -ErrorAction Stop
}

function Get-MediaMtxPath {
    param([Parameter(Mandatory)][string]$Name)
    $response = Invoke-MediaMtxApi
    return @($response.items | Where-Object { $_.name -eq $Name -and $_.ready -eq $true }) |
        Select-Object -First 1
}

function Invoke-Ffprobe {
    param([Parameter(Mandatory)][string]$Url)
    $record = Start-TrackedProcess -Label 'ffprobe' -FilePath $script:Ffprobe -Arguments @(
        '-v', 'error', '-rw_timeout', '3000000',
        '-show_entries', 'stream=codec_type,codec_name,width,height',
        '-of', 'json', $Url
    )
    if (-not $record.Process.WaitForExit(8000)) {
        Stop-TrackedProcess $record
        throw 'ffprobe timed out'
    }
    if ($record.Process.ExitCode -ne 0) {
        throw 'ffprobe could not inspect the stream'
    }
    $raw = Get-Content -LiteralPath $record.Stdout -Raw
    return $raw | ConvertFrom-Json
}

function Test-StreamProbe {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][int]$Width,
        [Parameter(Mandatory)][int]$Height
    )
    if ($null -eq (Get-MediaMtxPath -Name $Path)) { return $false }
    $probe = Invoke-Ffprobe -Url "rtmp://127.0.0.1:1935/$Path"
    $video = @($probe.streams | Where-Object { $_.codec_type -eq 'video' }) | Select-Object -First 1
    $audio = @($probe.streams | Where-Object { $_.codec_type -eq 'audio' }) | Select-Object -First 1
    return $null -ne $video -and $null -ne $audio -and
        $video.codec_name -eq 'h264' -and $audio.codec_name -eq 'aac' -and
        [int]$video.width -eq $Width -and [int]$video.height -eq $Height
}

function Add-Check {
    param([Parameter(Mandatory)][string]$Name)
    $Checks.Add([ordered]@{ name = $Name; passed = $true })
}

function Start-LiveSource {
    return Start-TrackedProcess -Label 'source-live' -FilePath $script:Ffmpeg -Arguments @(
        '-hide_banner', '-loglevel', 'warning', '-nostdin', '-re',
        '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30',
        '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
        '-profile:v', 'main', '-pix_fmt', 'yuv420p', '-r', '30',
        '-g', '60', '-keyint_min', '60', '-sc_threshold', '0',
        '-c:a', 'aac', '-b:a', '128k', '-ar', '48000', '-ac', '2',
        '-f', 'flv', 'rtmp://127.0.0.1:1935/source/main'
    )
}

function Start-LiveTarget {
    param([Parameter(Mandatory)][ValidateSet('douyin', 'wechat')][string]$Target)
    return Start-TrackedProcess -Label "target-$Target-live" -FilePath $script:Ffmpeg -Arguments @(
        '-hide_banner', '-loglevel', 'warning', '-nostdin',
        '-rw_timeout', '5000000', '-i', 'rtmp://127.0.0.1:1935/source/main',
        '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy',
        '-f', 'flv', "rtmp://127.0.0.1:1935/target/$Target"
    )
}

function Start-StandbyTarget {
    param([Parameter(Mandatory)][ValidateSet('douyin', 'wechat')][string]$Target)
    return Start-TrackedProcess -Label "target-$Target-standby" -FilePath $script:Ffmpeg -Arguments @(
        '-hide_banner', '-loglevel', 'warning', '-nostdin', '-re',
        '-f', 'lavfi', '-i', 'color=c=0x183153:size=320x180:rate=30',
        '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
        '-pix_fmt', 'yuv420p', '-r', '30', '-g', '60', '-keyint_min', '60',
        '-sc_threshold', '0', '-c:a', 'aac', '-b:a', '96k', '-ar', '48000', '-ac', '2',
        '-f', 'flv', "rtmp://127.0.0.1:1935/target/$Target"
    )
}

function ConvertTo-SafeMessage {
    param([Parameter(Mandatory)][string]$Message)
    $safe = $Message -replace 'rtmps?://\S+', '[local-stream]'
    $safe = $safe -replace [Regex]::Escape($Root), '[workspace]'
    return $safe
}

$mediaMtx = $null
$source = $null
$targetA = $null
$targetB = $null

try {
    $script:Ffmpeg = Resolve-Tool -Name 'ffmpeg' -EnvironmentName 'FFMPEG_PATH' `
        -RepositoryCandidates @('tools\ffmpeg\ffmpeg.exe', 'bin\ffmpeg.exe')
    $script:Ffprobe = Resolve-Tool -Name 'ffprobe' -EnvironmentName 'FFPROBE_PATH' `
        -RepositoryCandidates @('tools\ffmpeg\ffprobe.exe', 'bin\ffprobe.exe')
    $script:MediaMtx = Resolve-Tool -Name 'mediamtx' -EnvironmentName 'MEDIAMTX_PATH' `
        -RepositoryCandidates @('tools\mediamtx\mediamtx.exe', 'bin\mediamtx.exe')
    if (-not (Test-Path -LiteralPath $MediaMtxConfig -PathType Leaf)) {
        throw 'MediaMTX configuration is missing'
    }
    Test-PortAvailable -Port 1935
    Test-PortAvailable -Port 9997

    $mediaMtx = Start-TrackedProcess -Label 'mediamtx' -FilePath $script:MediaMtx `
        -Arguments @($MediaMtxConfig)
    Wait-Condition -Label 'MediaMTX API readiness' -TimeoutSeconds 20 -Condition {
        $null -ne (Invoke-MediaMtxApi)
    }

    $source = Start-LiveSource
    Wait-Condition -Label 'live source readiness' -TimeoutSeconds 30 -Condition {
        Test-StreamProbe -Path 'source/main' -Width 640 -Height 360
    }
    $targetA = Start-LiveTarget -Target 'douyin'
    $targetB = Start-LiveTarget -Target 'wechat'
    Wait-Condition -Label 'both initial targets' -TimeoutSeconds 35 -Condition {
        (Test-StreamProbe -Path 'target/douyin' -Width 640 -Height 360) -and
        (Test-StreamProbe -Path 'target/wechat' -Width 640 -Height 360)
    }
    Add-Check -Name 'initial_dual_target'

    Stop-TrackedProcess $targetB
    Wait-Condition -Label 'target B stopped' -TimeoutSeconds 15 -Condition {
        $null -eq (Get-MediaMtxPath -Name 'target/wechat')
    }
    Wait-Condition -Label 'target A isolation' -TimeoutSeconds 20 -Condition {
        Test-StreamProbe -Path 'target/douyin' -Width 640 -Height 360
    }
    Add-Check -Name 'target_isolation'
    $targetB = Start-LiveTarget -Target 'wechat'
    Wait-Condition -Label 'target B restart' -TimeoutSeconds 25 -Condition {
        Test-StreamProbe -Path 'target/wechat' -Width 640 -Height 360
    }

    Stop-TrackedProcess $source
    $lossTimer = [Diagnostics.Stopwatch]::StartNew()
    Wait-Condition -Label 'source path disappearance' -TimeoutSeconds 20 -Condition {
        $null -eq (Get-MediaMtxPath -Name 'source/main')
    }
    Wait-Condition -Label 'source outage longer than 60 seconds' `
        -TimeoutSeconds ($StandbyAfterSeconds + 15) -Condition {
        $lossTimer.Elapsed.TotalSeconds -ge $StandbyAfterSeconds -and
        $null -eq (Get-MediaMtxPath -Name 'source/main')
    }

    Stop-TrackedProcess $targetA
    Stop-TrackedProcess $targetB
    $targetA = Start-StandbyTarget -Target 'douyin'
    $targetB = Start-StandbyTarget -Target 'wechat'
    Wait-Condition -Label 'both standby targets' -TimeoutSeconds 35 -Condition {
        (Test-StreamProbe -Path 'target/douyin' -Width 320 -Height 180) -and
        (Test-StreamProbe -Path 'target/wechat' -Width 320 -Height 180)
    }
    Add-Check -Name 'standby_after_source_loss'

    $source = Start-LiveSource
    Wait-Condition -Label 'source recovery readiness' -TimeoutSeconds 30 -Condition {
        Test-StreamProbe -Path 'source/main' -Width 640 -Height 360
    }
    Stop-TrackedProcess $targetA
    Stop-TrackedProcess $targetB
    $targetA = Start-LiveTarget -Target 'douyin'
    $targetB = Start-LiveTarget -Target 'wechat'
    Wait-Condition -Label 'first dual-target recovery probe' -TimeoutSeconds 35 -Condition {
        (Test-StreamProbe -Path 'target/douyin' -Width 640 -Height 360) -and
        (Test-StreamProbe -Path 'target/wechat' -Width 640 -Height 360)
    }
    Add-Check -Name 'recovery_probe_1'
    Wait-Condition -Label 'second dual-target recovery probe' -TimeoutSeconds 20 -Condition {
        (Test-StreamProbe -Path 'target/douyin' -Width 640 -Height 360) -and
        (Test-StreamProbe -Path 'target/wechat' -Width 640 -Height 360)
    }
    Add-Check -Name 'recovery_probe_2'
    $Passed = $true
}
catch {
    $Failure = ConvertTo-SafeMessage -Message $_.Exception.Message
}
finally {
    for ($index = $TrackedProcesses.Count - 1; $index -ge 0; $index--) {
        Stop-TrackedProcess $TrackedProcesses[$index]
    }
    $result = [ordered]@{
        schema_version = 1
        status = $(if ($Passed) { 'passed' } else { 'failed' })
        started_at_utc = $RunStarted.ToString('o')
        completed_at_utc = [DateTimeOffset]::UtcNow.ToString('o')
        duration_seconds = [Math]::Round($Deadline.Elapsed.TotalSeconds, 3)
        paths = @('source/main', 'target/douyin', 'target/wechat')
        checks = @($Checks)
        error = $Failure
    }
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ResultFile -Encoding UTF8
}

if (-not $Passed) {
    Write-Error $Failure
    exit 1
}
Write-Output "Local RTMP acceptance passed. Evidence: artifacts/e2e-local/result.json"
