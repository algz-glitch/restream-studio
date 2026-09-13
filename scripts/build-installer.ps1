[CmdletBinding()]
param([switch]$Clean)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$PackageScript = Join-Path $PSScriptRoot 'package.ps1'
$InstallerScript = Join-Path $Root 'packaging\restream-studio.iss'
$InstallerDirectory = Join-Path $Root 'dist\installer'
$Installer = Join-Path $InstallerDirectory 'RestreamStudio-Setup-0.1.0.exe'

function Invoke-External {
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$Arguments = @())
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$FilePath failed with exit code $LASTEXITCODE" }
}

function Resolve-Iscc {
    $configured = [Environment]::GetEnvironmentVariable('ISCC_PATH')
    if ($configured) {
        if (Test-Path -LiteralPath $configured -PathType Leaf) {
            return (Resolve-Path -LiteralPath $configured).Path
        }
        throw 'ISCC_PATH points to a missing file'
    }

    $pinnedLocalPath = 'G:\Apps\Inno\ISCC.exe'
    if (Test-Path -LiteralPath $pinnedLocalPath -PathType Leaf) {
        return (Resolve-Path -LiteralPath $pinnedLocalPath).Path
    }

    $command = Get-Command 'ISCC.exe' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $command) { return $command.Source }
    throw 'ISCC.exe is unavailable; set ISCC_PATH or install Inno Setup explicitly'
}

if ($Clean -and (Test-Path -LiteralPath $InstallerDirectory)) {
    $resolvedRoot = [IO.Path]::GetFullPath((Join-Path $Root 'dist')) +
        [IO.Path]::DirectorySeparatorChar
    $resolvedInstallerDirectory = [IO.Path]::GetFullPath($InstallerDirectory)
    if (-not $resolvedInstallerDirectory.StartsWith(
        $resolvedRoot, [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'refusing to clean an installer directory outside dist'
    }
    Remove-Item -LiteralPath $resolvedInstallerDirectory -Recurse -Force
}

& $PackageScript -Clean:$Clean
if ($LASTEXITCODE -ne 0) { throw "package.ps1 failed with exit code $LASTEXITCODE" }

$distribution = Join-Path $Root 'dist\RestreamStudio'
foreach ($required in ('RestreamStudio.exe', 'RestreamStudioUpdateHelper.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $distribution $required) -PathType Leaf)) {
        throw "packaged executable is missing: $required"
    }
}
$updateHelper = Join-Path $distribution 'RestreamStudioUpdateHelper.exe'
$helperProcess = Start-Process -FilePath $updateHelper -ArgumentList '--invalid' `
    -WindowStyle Hidden -Wait -PassThru
try {
    if ($helperProcess.ExitCode -ne 2) {
        throw "packaged update helper smoke failed with exit code $($helperProcess.ExitCode)"
    }
}
finally { $helperProcess.Dispose() }

$iscc = Resolve-Iscc
New-Item -ItemType Directory -Force -Path $InstallerDirectory | Out-Null
Invoke-External $iscc @($InstallerScript)
if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
    throw "installer output is missing: $Installer"
}

$hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "INSTALLER_PATH=$Installer"
Write-Output "SHA256=$hash"
