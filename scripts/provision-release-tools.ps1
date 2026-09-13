[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$Root = Split-Path -Parent $PSScriptRoot
$ToolsRoot = Join-Path $Root 'build\release-tools'
$DownloadRoot = Join-Path $ToolsRoot 'downloads'
$FfmpegUrl = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-09-12-13-12/ffmpeg-n8.1.2-52-g5a03dfa0f6-win64-gpl-8.1.zip'
$FfmpegSha256 = '8EBD7E82791B8F753ADE7FD6F2EACF8CE02127DFB1F25102D85154D1779CD2DE'
$MediaMtxUrl = 'https://github.com/bluenviron/mediamtx/releases/download/v1.21.0/mediamtx_v1.21.0_windows_amd64.zip'
$MediaMtxSha256 = '8A58A9B8C25EE99A96C23DC0A17F39ACE3072C01D2E148329073C64DDF83493D'
$MaxZipEntries = 4096

function Remove-ReleaseToolDirectory {
    param([Parameter(Mandatory)][string]$Path)
    $resolvedRoot = [IO.Path]::GetFullPath((Join-Path $Root 'build')) + [IO.Path]::DirectorySeparatorChar
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    if (-not $resolvedPath.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "refusing to remove path outside workspace build root: $resolvedPath"
    }
    if (Test-Path -LiteralPath $resolvedPath) { Remove-Item -LiteralPath $resolvedPath -Recurse -Force }
}

function Get-VerifiedArchive {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$Sha256,
        [Parameter(Mandatory)][string]$Destination,
        [Parameter(Mandatory)][long]$MaxDownloadBytes
    )
    $client = [System.Net.Http.HttpClient]::new()
    $response = $null
    $request = $null
    $source = $null
    $target = $null
    $downloadError = $null
    try {
        $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, $Uri)
        $response = $client.SendAsync(
            $request,
            [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()
        $response.EnsureSuccessStatusCode()
        # Reject an excessive Content-Length before reading, then enforce the same cap while streaming.
        $contentLength = $response.Content.Headers.ContentLength
        if ($null -ne $contentLength -and $contentLength -gt $MaxDownloadBytes) {
            throw "Content-Length exceeds MaxDownloadBytes for $Uri"
        }
        $source = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $target = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $buffer = New-Object byte[] (1024 * 1024)
        [long]$total = 0
        while (($read = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $total += $read
            if ($total -gt $MaxDownloadBytes) { throw "download exceeds MaxDownloadBytes for $Uri" }
            $target.Write($buffer, 0, $read)
        }
    }
    catch {
        $downloadError = $_
    }
    finally {
        if ($null -ne $target) { $target.Dispose() }
        if ($null -ne $source) { $source.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
        if ($null -ne $request) { $request.Dispose() }
        $client.Dispose()
    }
    if ($null -ne $downloadError) {
        if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Force }
        throw $downloadError
    }
    $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($actual -cne $Sha256) {
        Remove-Item -LiteralPath $Destination -Force
        throw "SHA256 mismatch for $Uri; expected $Sha256, got $actual"
    }
}

function Expand-VerifiedZip {
    param(
        [Parameter(Mandatory)][string]$Archive,
        [Parameter(Mandatory)][string]$Destination,
        [Parameter(Mandatory)][long]$MaxExpandedBytes
    )
    New-Item -ItemType Directory -Path $Destination | Out-Null
    $destinationRoot = [IO.Path]::GetFullPath($Destination) + [IO.Path]::DirectorySeparatorChar
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        if ($zip.Entries.Count -gt $MaxZipEntries) { throw 'ZipArchive exceeds MaxZipEntries' }
        [long]$expanded = 0
        foreach ($entry in $zip.Entries) {
            $relative = $entry.FullName.Replace('/', [IO.Path]::DirectorySeparatorChar)
            if ([IO.Path]::IsPathRooted($relative) -or $relative.Contains(':') -or
                @($relative.Split([IO.Path]::DirectorySeparatorChar)) -contains '..') {
                throw "ZipArchive path traversal entry rejected: $($entry.FullName)"
            }
            $unixType = (($entry.ExternalAttributes -shr 16) -band 0xF000)
            if ($unixType -eq 0xA000) { throw "ZipArchive symbolic link entry rejected: $($entry.FullName)" }
            $target = [IO.Path]::GetFullPath((Join-Path $Destination $relative))
            if (-not $target.StartsWith($destinationRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw "ZipArchive path traversal entry rejected: $($entry.FullName)"
            }
            $expanded += $entry.Length
            if ($expanded -gt $MaxExpandedBytes) { throw 'ZipArchive exceeds MaxExpandedBytes' }
            if ([string]::IsNullOrEmpty($entry.Name)) {
                New-Item -ItemType Directory -Force -Path $target | Out-Null
            }
            else {
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
                $input = $entry.Open()
                $output = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try { $input.CopyTo($output) } finally { $output.Dispose(); $input.Dispose() }
            }
        }
    }
    finally { $zip.Dispose() }
    foreach ($item in Get-ChildItem -LiteralPath $Destination -Recurse -Force) {
        $full = [IO.Path]::GetFullPath($item.FullName)
        if (-not $full.StartsWith($destinationRoot, [StringComparison]::OrdinalIgnoreCase) -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "expanded archive contains an unsafe ReparsePoint or path: $full"
        }
    }
}

function Write-EnvironmentPath {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$Path)
    [Environment]::SetEnvironmentVariable($Name, $Path)
    if ($env:GITHUB_ENV) { "$Name=$Path" | Out-File -LiteralPath $env:GITHUB_ENV -Encoding utf8 -Append }
    Write-Output "$Name=$Path"
}

function Resolve-SingleFile {
    param([string]$RootPath, [string]$Filter, [string]$Description)
    $matches = @(Get-ChildItem -LiteralPath $RootPath -Filter $Filter -Recurse -File)
    if ($matches.Count -ne 1) { throw "verified archive must contain exactly one $Description; found $($matches.Count)" }
    return $matches[0]
}

Remove-ReleaseToolDirectory -Path $ToolsRoot
New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null

$ffmpegArchive = Join-Path $DownloadRoot 'ffmpeg.zip'
$ffmpegExtract = Join-Path $ToolsRoot 'ffmpeg-extract'
Get-VerifiedArchive -Uri $FfmpegUrl -Sha256 $FfmpegSha256 -Destination $ffmpegArchive -MaxDownloadBytes (256MB)
Expand-VerifiedZip -Archive $ffmpegArchive -Destination $ffmpegExtract -MaxExpandedBytes (1536MB)
$ffmpegSource = Resolve-SingleFile $ffmpegExtract 'ffmpeg.exe' 'ffmpeg.exe'
$ffprobeSource = Resolve-SingleFile $ffmpegExtract 'ffprobe.exe' 'ffprobe.exe'
$ffmpegLicense = Get-ChildItem -LiteralPath $ffmpegExtract -Recurse -File |
    Where-Object { $_.Name -in @('LICENSE', 'LICENSE.txt', 'COPYING.GPLv3') } | Select-Object -First 1
if ($null -eq $ffmpegLicense) { throw 'verified FFmpeg archive did not contain a license file' }
$ffmpegRoot = Join-Path $ToolsRoot 'ffmpeg'
$ffmpegBin = Join-Path $ffmpegRoot 'bin'
New-Item -ItemType Directory -Force -Path $ffmpegBin | Out-Null
Copy-Item -LiteralPath $ffmpegSource.FullName -Destination (Join-Path $ffmpegBin 'ffmpeg.exe')
Copy-Item -LiteralPath $ffprobeSource.FullName -Destination (Join-Path $ffmpegBin 'ffprobe.exe')
Copy-Item -LiteralPath $ffmpegLicense.FullName -Destination (Join-Path $ffmpegRoot 'LICENSE')

$mediaMtxArchive = Join-Path $DownloadRoot 'mediamtx.zip'
$mediaMtxExtract = Join-Path $ToolsRoot 'mediamtx-extract'
Get-VerifiedArchive -Uri $MediaMtxUrl -Sha256 $MediaMtxSha256 -Destination $mediaMtxArchive -MaxDownloadBytes (64MB)
Expand-VerifiedZip -Archive $mediaMtxArchive -Destination $mediaMtxExtract -MaxExpandedBytes (256MB)
$mediaMtxSource = Resolve-SingleFile $mediaMtxExtract 'mediamtx.exe' 'mediamtx.exe'
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
Write-EnvironmentPath 'FFMPEG_PATH' $ffmpeg
Write-EnvironmentPath 'FFPROBE_PATH' $ffprobe
Write-EnvironmentPath 'MEDIAMTX_PATH' $mediaMtx
