[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$ToolsRoot = Join-Path $Root 'build\release-tools'
$DownloadRoot = Join-Path $ToolsRoot 'downloads'
$FfmpegUrl = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-09-12-13-12/ffmpeg-n8.1.2-52-g5a03dfa0f6-win64-gpl-8.1.zip'
$FfmpegSha256 = '8EBD7E82791B8F753ADE7FD6F2EACF8CE02127DFB1F25102D85154D1779CD2DE'
$MediaMtxUrl = 'https://github.com/bluenviron/mediamtx/releases/download/v1.21.0/mediamtx_v1.21.0_windows_amd64.zip'
$MediaMtxSha256 = '8A58A9B8C25EE99A96C23DC0A17F39ACE3072C01D2E148329073C64DDF83493D'

function Remove-ReleaseToolDirectory {
    param([Parameter(Mandatory)][string]$Path)
    $resolvedRoot = [IO.Path]::GetFullPath((Join-Path $Root 'build')) + [IO.Path]::DirectorySeparatorChar
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    if (-not $resolvedPath.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "refusing to remove path outside workspace build root: $resolvedPath"
    }
    if (Test-Path -LiteralPath $resolvedPath) {
        Remove-Item -LiteralPath $resolvedPath -Recurse -Force
    }
}

function Get-VerifiedArchive {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$Sha256,
        [Parameter(Mandatory)][string]$Destination
    )
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $Destination
    $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($actual -cne $Sha256) {
        Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
        throw "SHA256 mismatch for $Uri; expected $Sha256, got $actual"
    }
}

function Write-EnvironmentPath {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$Path)
    [Environment]::SetEnvironmentVariable($Name, $Path)
    if ($env:GITHUB_ENV) {
        "$Name=$Path" | Out-File -LiteralPath $env:GITHUB_ENV -Encoding utf8 -Append
    }
    Write-Output "$Name=$Path"
}

function Resolve-SingleFile {
    param(
        [Parameter(Mandatory)][string]$RootPath,
        [Parameter(Mandatory)][string]$Filter,
        [Parameter(Mandatory)][string]$Description
    )
    $matches = @(Get-ChildItem -LiteralPath $RootPath -Filter $Filter -Recurse -File)
    if ($matches.Count -ne 1) {
        throw "verified archive must contain exactly one $Description; found $($matches.Count)"
    }
    return $matches[0]
}

Remove-ReleaseToolDirectory -Path $ToolsRoot
New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null

$ffmpegArchive = Join-Path $DownloadRoot 'ffmpeg.zip'
$ffmpegExtract = Join-Path $ToolsRoot 'ffmpeg-extract'
Get-VerifiedArchive -Uri $FfmpegUrl -Sha256 $FfmpegSha256 -Destination $ffmpegArchive
Expand-Archive -LiteralPath $ffmpegArchive -DestinationPath $ffmpegExtract
$ffmpegSource = Resolve-SingleFile -RootPath $ffmpegExtract -Filter 'ffmpeg.exe' `
    -Description 'ffmpeg.exe'
$ffprobeSource = Resolve-SingleFile -RootPath $ffmpegExtract -Filter 'ffprobe.exe' `
    -Description 'ffprobe.exe'
$ffmpegLicense = Get-ChildItem -LiteralPath $ffmpegExtract -Recurse -File |
    Where-Object { $_.Name -in @('LICENSE', 'LICENSE.txt', 'COPYING.GPLv3') } |
    Select-Object -First 1
if ($null -eq $ffmpegLicense) { throw 'verified FFmpeg archive did not contain a license file' }
$ffmpegRoot = Join-Path $ToolsRoot 'ffmpeg'
$ffmpegBin = Join-Path $ffmpegRoot 'bin'
New-Item -ItemType Directory -Force -Path $ffmpegBin | Out-Null
Copy-Item -LiteralPath $ffmpegSource.FullName -Destination (Join-Path $ffmpegBin 'ffmpeg.exe')
Copy-Item -LiteralPath $ffprobeSource.FullName -Destination (Join-Path $ffmpegBin 'ffprobe.exe')
Copy-Item -LiteralPath $ffmpegLicense.FullName -Destination (Join-Path $ffmpegRoot 'LICENSE')

$mediaMtxArchive = Join-Path $DownloadRoot 'mediamtx.zip'
$mediaMtxExtract = Join-Path $ToolsRoot 'mediamtx-extract'
Get-VerifiedArchive -Uri $MediaMtxUrl -Sha256 $MediaMtxSha256 -Destination $mediaMtxArchive
Expand-Archive -LiteralPath $mediaMtxArchive -DestinationPath $mediaMtxExtract
$mediaMtxSource = Resolve-SingleFile -RootPath $mediaMtxExtract -Filter 'mediamtx.exe' `
    -Description 'mediamtx.exe'
$mediaMtxRoot = Join-Path $ToolsRoot 'mediamtx'
New-Item -ItemType Directory -Force -Path $mediaMtxRoot | Out-Null
Copy-Item -LiteralPath $mediaMtxSource.FullName -Destination (Join-Path $mediaMtxRoot 'mediamtx.exe')

$ffmpeg = Join-Path $ffmpegBin 'ffmpeg.exe'
$ffprobe = Join-Path $ffmpegBin 'ffprobe.exe'
$mediaMtx = Join-Path $mediaMtxRoot 'mediamtx.exe'
& $ffmpeg -version | Select-Object -First 1
if ($LASTEXITCODE -ne 0) { throw 'provisioned ffmpeg failed its version probe' }
& $ffprobe -version | Select-Object -First 1
if ($LASTEXITCODE -ne 0) { throw 'provisioned ffprobe failed its version probe' }
& $mediaMtx --version
if ($LASTEXITCODE -ne 0) { throw 'provisioned MediaMTX failed its version probe' }

Write-EnvironmentPath -Name 'FFMPEG_PATH' -Path $ffmpeg
Write-EnvironmentPath -Name 'FFPROBE_PATH' -Path $ffprobe
Write-EnvironmentPath -Name 'MEDIAMTX_PATH' -Path $mediaMtx
