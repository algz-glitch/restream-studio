[CmdletBinding()]
param(
    [string]$OutputDirectory = '',
    [switch]$Clean
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

$Npm = Resolve-Tool -Name 'npm.cmd' -EnvironmentName 'NPM_PATH' `
    -RepositoryCandidates @('tools\node\npm.cmd')
$Python = Resolve-Tool -Name 'python.exe' -EnvironmentName 'PYTHON_PATH' `
    -RepositoryCandidates @('.venv\Scripts\python.exe')

Push-Location $Root
try {
    Write-Output 'RUN=npm ci'
    Invoke-External $Npm @('ci', '--ignore-scripts', '--no-audit', '--no-fund')
    Write-Output 'RUN=npm run frontend:build'
    Invoke-External $Npm @('run', 'frontend:build')
}
finally { Pop-Location }
if (-not (Test-Path -LiteralPath $StaticIndex -PathType Leaf)) {
    throw 'frontend build output is missing after npm run frontend:build'
}

$Ffmpeg = Resolve-Tool -Name 'ffmpeg.exe' -EnvironmentName 'FFMPEG_PATH' `
    -RepositoryCandidates @('tools\ffmpeg\ffmpeg.exe', 'bin\ffmpeg.exe')
$Ffprobe = Resolve-Tool -Name 'ffprobe.exe' -EnvironmentName 'FFPROBE_PATH' `
    -RepositoryCandidates @('tools\ffmpeg\ffprobe.exe', 'bin\ffprobe.exe')

Remove-WorkspaceDirectory $InputDirectory
New-Item -ItemType Directory -Force -Path (Join-Path $InputDirectory 'licenses') | Out-Null
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
