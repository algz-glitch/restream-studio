[CmdletBinding()]
param(
    [string]$OutputDirectory = '',
    [switch]$Clean,
    [switch]$ReuseVerifiedOutput,
    [string]$PackageManifest = '',
    [switch]$ReuseFrontend,
    [string]$FrontendManifest = '',
    [switch]$SmokeBuild,
    [string]$VersionOverride = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $Root 'dist' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$BuildDirectory = Join-Path $Root 'build\pyinstaller'
$InputDirectory = Join-Path $Root 'build\packaging-input'
$StaticIndex = Join-Path $Root 'src\restream_studio\static\index.html'
$Spec = Join-Path $Root 'packaging\restream-studio.spec'
$LockFile = Join-Path $Root 'package-lock.json'
$TreeManifestTool = Join-Path $Root 'scripts\write-tree-manifest.py'
$SourceVersion = ([IO.File]::ReadAllText((Join-Path $Root 'package.json')) | ConvertFrom-Json).version

function Get-NextPatchVersion {
    param([Parameter(Mandatory)][string]$Version)
    if ($Version -cnotmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') {
        throw 'source version must be canonical SemVer'
    }
    $nextPatch = [Numerics.BigInteger]::Parse($Matches[3]) + 1
    return "$($Matches[1]).$($Matches[2]).$nextPatch"
}

$ProjectVersion = $SourceVersion
if ($VersionOverride) {
    $expectedOverride = Get-NextPatchVersion $SourceVersion
    if (-not $SmokeBuild -or $VersionOverride -cne $expectedOverride) {
        throw 'VersionOverride must equal the smoke-only next patch version'
    }
    $ProjectVersion = $VersionOverride
}
elseif ($SmokeBuild) { throw 'SmokeBuild requires VersionOverride' }
$CurrentCommit = ''
if ($ReuseVerifiedOutput -or $ReuseFrontend) {
    $CurrentCommit = (& git -C $Root rev-parse --verify HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $CurrentCommit -notmatch '^[0-9a-f]{40}$') {
        throw 'verified reuse requires a valid current git commit identity'
    }
}

function Resolve-Tool {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$EnvironmentName,
        [Parameter(Mandatory)][string[]]$RepositoryCandidates
    )
    $configured = [Environment]::GetEnvironmentVariable($EnvironmentName)
    if ($configured) {
        if (Test-Path -LiteralPath $configured -PathType Leaf) {
            return (Resolve-Path -LiteralPath $configured).Path
        }
        throw "$EnvironmentName points to a missing file"
    }
    foreach ($candidate in $RepositoryCandidates) {
        $path = Join-Path $Root $candidate
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            return (Resolve-Path -LiteralPath $path).Path
        }
    }
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $command) { return $command.Source }
    throw "required tool is unavailable: $Name"
}

function Invoke-External {
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$Arguments = @())
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function Remove-WorkspaceDirectory {
    param([Parameter(Mandatory)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    $allowed = [IO.Path]::GetFullPath((Join-Path $Root 'build')) + [IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($allowed, [StringComparison]::OrdinalIgnoreCase)) {
        throw "refusing to remove directory outside the workspace build root: $full"
    }
    if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Recurse -Force }
}

if (-not (Test-Path -LiteralPath $LockFile -PathType Leaf)) {
    throw 'package-lock.json is required; generate it with npm, never by hand'
}

if ($ReuseVerifiedOutput) {
    $verifiedCommit = [Environment]::GetEnvironmentVariable('RESTREAM_STUDIO_VERIFIED_COMMIT')
    if ($LASTEXITCODE -ne 0 -or $verifiedCommit -notmatch '^[0-9a-f]{40}$' -or
        $CurrentCommit -cne $verifiedCommit) {
        throw 'verified package reuse requires the exact current git commit identity'
    }
    if (-not $PackageManifest -or -not (Test-Path -LiteralPath $PackageManifest -PathType Leaf)) {
        throw 'verified package reuse requires PackageManifest'
    }
    $verifiedDistribution = Join-Path $OutputDirectory 'RestreamStudio'
    $manifestPython = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $manifestPython -PathType Leaf)) {
        throw 'verified package reuse requires the repository Python environment'
    }
    Invoke-External $manifestPython @(
        $TreeManifestTool, '--root', $verifiedDistribution, '--kind', 'package',
        '--commit', $CurrentCommit, '--version', $ProjectVersion,
        '--output', $PackageManifest, '--verify-existing'
    )
    Write-Output "REUSED_VERIFIED_PACKAGE=$verifiedDistribution"
    exit 0
}

$Npm = Resolve-Tool -Name 'npm.cmd' -EnvironmentName 'NPM_PATH' `
    -RepositoryCandidates @('tools\node\npm.cmd')
$Python = Resolve-Tool -Name 'python.exe' -EnvironmentName 'PYTHON_PATH' `
    -RepositoryCandidates @('.venv\Scripts\python.exe')

if ($ReuseFrontend) {
    if (-not $FrontendManifest -or -not (Test-Path -LiteralPath $FrontendManifest -PathType Leaf)) {
        throw 'ReuseFrontend requires FrontendManifest'
    }
    Invoke-External $Python @(
        $TreeManifestTool, '--root', (Split-Path -Parent $StaticIndex), '--kind', 'frontend',
        '--commit', $CurrentCommit, '--version', $ProjectVersion,
        '--output', $FrontendManifest, '--verify-existing'
    )
}
else {
    Push-Location $Root
    try {
        Write-Output 'RUN=npm ci'
        Invoke-External $Npm @('ci', '--ignore-scripts', '--no-audit', '--no-fund')
        Write-Output 'RUN=npm run frontend:build'
        Invoke-External $Npm @('run', 'frontend:build')
    }
    finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath $StaticIndex -PathType Leaf)) {
    throw 'frontend build output is missing after npm run frontend:build'
}

$Ffmpeg = Resolve-Tool -Name 'ffmpeg.exe' -EnvironmentName 'FFMPEG_PATH' `
    -RepositoryCandidates @('tools\ffmpeg\ffmpeg.exe', 'bin\ffmpeg.exe')
$Ffprobe = Resolve-Tool -Name 'ffprobe.exe' -EnvironmentName 'FFPROBE_PATH' `
    -RepositoryCandidates @('tools\ffmpeg\ffprobe.exe', 'bin\ffprobe.exe')

Remove-WorkspaceDirectory $InputDirectory
New-Item -ItemType Directory -Force -Path (Join-Path $InputDirectory 'licenses') | Out-Null
[IO.File]::WriteAllText((Join-Path $InputDirectory 'version.txt'), $ProjectVersion,
    [Text.Encoding]::ASCII)
Copy-Item -LiteralPath $Ffmpeg -Destination (Join-Path $InputDirectory 'ffmpeg.exe')
Copy-Item -LiteralPath $Ffprobe -Destination (Join-Path $InputDirectory 'ffprobe.exe')
Copy-Item -LiteralPath (Join-Path $Root 'packaging\licenses\THIRD-PARTY-NOTICES.txt') `
    -Destination (Join-Path $InputDirectory 'licenses\THIRD-PARTY-NOTICES.txt')

$FfmpegRoot = Split-Path -Parent (Split-Path -Parent $Ffmpeg)
$FfmpegLicense = Join-Path $FfmpegRoot 'LICENSE'
if (-not (Test-Path -LiteralPath $FfmpegLicense -PathType Leaf)) {
    throw 'the selected FFmpeg distribution does not include its LICENSE file'
}
Copy-Item -LiteralPath $FfmpegLicense `
    -Destination (Join-Path $InputDirectory 'licenses\FFmpeg-LICENSE.txt')

$Standby = Join-Path $InputDirectory 'default-standby.mp4'
Invoke-External $Ffmpeg @(
    '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
    '-f', 'lavfi', '-i', 'color=c=0x111827:s=1280x720:r=30:d=5',
    '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo:d=5',
    '-map_metadata', '-1', '-metadata', 'creation_time=1970-01-01T00:00:00Z',
    '-c:v', 'libx264', '-preset', 'medium', '-profile:v', 'main',
    '-pix_fmt', 'yuv420p', '-r', '30', '-g', '60', '-keyint_min', '60',
    '-sc_threshold', '0', '-c:a', 'aac', '-b:a', '128k', '-ar', '48000',
    '-ac', '2', '-t', '5', '-fflags', '+bitexact', '-flags:v', '+bitexact',
    '-flags:a', '+bitexact', '-movflags', '+faststart', $Standby
)

$previousInput = [Environment]::GetEnvironmentVariable('RESTREAM_STUDIO_PACKAGE_INPUT')
[Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_PACKAGE_INPUT', $InputDirectory)
try {
    $arguments = @(
        '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--distpath', $OutputDirectory, '--workpath', $BuildDirectory, $Spec
    )
    Invoke-External $Python $arguments
}
finally {
    [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_PACKAGE_INPUT', $previousInput)
}

$Executable = Join-Path $OutputDirectory 'RestreamStudio\RestreamStudio.exe'
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "PyInstaller directory package is missing: $Executable"
}
$Distribution = Split-Path -Parent $Executable
$UpdateHelper = Join-Path $Distribution 'RestreamStudioUpdateHelper.exe'
if (-not (Test-Path -LiteralPath $UpdateHelper -PathType Leaf)) {
    throw "PyInstaller update helper is missing: $UpdateHelper"
}
Copy-Item -LiteralPath (Join-Path $Root 'packaging\Enable-Localhost.ps1') `
    -Destination (Join-Path $Distribution 'Enable-Localhost.ps1') -Force
Copy-Item -LiteralPath (Join-Path $Root 'packaging\Enable-Localhost.cmd') `
    -Destination (Join-Path $Distribution 'Enable-Localhost.cmd') -Force
Write-Output "PACKAGE_PATH=$([IO.Path]::GetDirectoryName($Executable))"
Write-Output "UPDATE_HELPER_PATH=$UpdateHelper"
