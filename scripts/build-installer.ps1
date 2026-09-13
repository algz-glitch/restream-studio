[CmdletBinding()]
param(
    [switch]$Clean,
    [string]$SourceRoot = '',
    [switch]$ResolveVersionOnly,
    [switch]$ReuseVerifiedPackage,
    [string]$PackageManifest = '',
    [switch]$BuildSmokeFixtures
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = if ($SourceRoot) {
    (Resolve-Path -LiteralPath $SourceRoot -ErrorAction Stop).Path
}
else {
    Split-Path -Parent $PSScriptRoot
}
$PackageScript = Join-Path $Root 'scripts\package.ps1'
$InstallerScript = Join-Path $Root 'packaging\restream-studio.iss'
$InstallerDirectory = Join-Path $Root 'dist\installer'

function Get-ReleaseVersion {
    param([Parameter(Mandatory)][string]$RootPath)

    $issPath = Join-Path $RootPath 'packaging\restream-studio.iss'
    $packagePath = Join-Path $RootPath 'package.json'
    $pyprojectPath = Join-Path $RootPath 'pyproject.toml'
    foreach ($path in ($issPath, $packagePath, $pyprojectPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "release version source is missing: $path"
        }
    }

    $semverPattern = '(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    $issMatches = [Regex]::Matches(
        [IO.File]::ReadAllText($issPath),
        "(?m)^#define MyAppVersion `"($semverPattern)`"\s*$"
    )
    if ($issMatches.Count -ne 1) {
        throw 'restream-studio.iss must contain exactly one canonical MyAppVersion'
    }
    $version = $issMatches[0].Groups[1].Value

    $packageVersion = ([IO.File]::ReadAllText($packagePath) | ConvertFrom-Json).version
    if ($packageVersion -isnot [string] -or $packageVersion -cnotmatch "^$semverPattern$") {
        throw 'package.json version must be canonical SemVer'
    }
    $pyprojectMatches = [Regex]::Matches(
        [IO.File]::ReadAllText($pyprojectPath),
        "(?m)^version\s*=\s*`"($semverPattern)`"\s*$"
    )
    if ($pyprojectMatches.Count -ne 1) {
        throw 'pyproject.toml must contain exactly one canonical project version'
    }
    $pyprojectVersion = $pyprojectMatches[0].Groups[1].Value
    if ($packageVersion -cne $version -or $pyprojectVersion -cne $version) {
        throw 'release versions differ across .iss, package.json, and pyproject.toml'
    }
    return $version
}

$Version = Get-ReleaseVersion -RootPath $Root
$Installer = Join-Path $InstallerDirectory "RestreamStudio-Setup-$Version.exe"
$SmokeUpgradeVersion = '0.1.1'
$SmokeUpgradeInstaller = Join-Path $InstallerDirectory `
    "RestreamStudio-Setup-$SmokeUpgradeVersion.exe"
if ($ResolveVersionOnly) {
    Write-Output "RELEASE_VERSION=$Version"
    Write-Output "INSTALLER_PATH=$Installer"
    exit 0
}

function Invoke-External {
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$Arguments = @())
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$FilePath failed with exit code $LASTEXITCODE" }
}

function Assert-IsccVersion {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$SourceLabel
    )
    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $signature = Get-AuthenticodeSignature -LiteralPath $resolved
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
        $null -eq $signature.SignerCertificate -or
        $signature.SignerCertificate.Subject -notmatch '(^|,\s*)O=Pyrsys B\.V\.(,|$)') {
        throw "$SourceLabel does not have a valid trusted Pyrsys B.V. Authenticode signature"
    }
    $probeRoot = Join-Path $Root "build\iscc-version-$([Guid]::NewGuid().ToString('N'))"
    try {
        New-Item -ItemType Directory -Force -Path $probeRoot | Out-Null
        $probe = Join-Path $probeRoot 'version-probe.iss'
        @'
[Setup]
AppName=ISCC Version Probe
AppVersion=1.0
DefaultDirName={tmp}\ISCC-Version-Probe
PrivilegesRequired=lowest
Uninstallable=no
OutputDir=.
OutputBaseFilename=version-probe
'@ | Set-Content -LiteralPath $probe -Encoding UTF8
        $output = @(& $Path $probe 2>&1 | ForEach-Object { [string]$_ })
        $exitCode = $LASTEXITCODE
        $output | ForEach-Object { Write-Verbose $_ }
        $expected = 'Compiler engine version: Inno Setup 6.7.3'
        if ($exitCode -ne 0 -or $output -notcontains $expected) {
            throw "$SourceLabel must point to Inno Setup 6.7.3"
        }
        return $resolved
    }
    finally {
        if (Test-Path -LiteralPath $probeRoot) {
            Remove-Item -LiteralPath $probeRoot -Recurse -Force
        }
    }
}

function Find-RegisteredIscc {
    $hives = @(
        [Microsoft.Win32.RegistryHive]::CurrentUser,
        [Microsoft.Win32.RegistryHive]::LocalMachine
    )
    $views = @(
        [Microsoft.Win32.RegistryView]::Registry64,
        [Microsoft.Win32.RegistryView]::Registry32
    )
    foreach ($hive in $hives) {
        foreach ($view in $views) {
            $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, $view)
            try {
                $uninstall = $base.OpenSubKey(
                    'Software\Microsoft\Windows\CurrentVersion\Uninstall'
                )
                if ($null -eq $uninstall) { continue }
                try {
                    foreach ($subKeyName in $uninstall.GetSubKeyNames()) {
                        $entry = $uninstall.OpenSubKey($subKeyName)
                        if ($null -eq $entry) { continue }
                        try {
                            if ($entry.GetValue('DisplayVersion') -cne '6.7.3' -or
                                $entry.GetValue('Publisher') -cne 'jrsoftware.org') {
                                continue
                            }
                            $location = [string]$entry.GetValue('InstallLocation')
                            if (-not $location) { continue }
                            $candidate = Join-Path $location 'ISCC.exe'
                            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                                return $candidate
                            }
                        }
                        finally { $entry.Dispose() }
                    }
                }
                finally { $uninstall.Dispose() }
            }
            finally { $base.Dispose() }
        }
    }
    return $null
}

function Resolve-Iscc {
    $configured = [Environment]::GetEnvironmentVariable('ISCC_PATH')
    if ($configured) {
        if (-not (Test-Path -LiteralPath $configured -PathType Leaf)) {
            throw 'ISCC_PATH points to a missing file'
        }
        try {
            return Assert-IsccVersion -Path $configured -SourceLabel 'ISCC_PATH'
        }
        catch { throw 'ISCC_PATH must point to Inno Setup 6.7.3 with a valid trusted Pyrsys B.V. signature' }
    }

    $registered = Find-RegisteredIscc
    if ($registered) {
        return Assert-IsccVersion -Path $registered -SourceLabel 'registered ISCC.exe'
    }

    $winget = (Get-Command 'winget.exe' -CommandType Application -ErrorAction Stop).Source
    Invoke-External $winget @(
        'install', '--id', 'JRSoftware.InnoSetup', '--version', '6.7.3', '--exact',
        '--scope', 'user', '--silent', '--accept-package-agreements',
        '--accept-source-agreements', '--disable-interactivity'
    )
    $registered = Find-RegisteredIscc
    if ($registered) {
        return Assert-IsccVersion -Path $registered -SourceLabel 'winget registered ISCC.exe'
    }
    $installedCandidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    )
    foreach ($candidate in $installedCandidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return Assert-IsccVersion -Path $candidate -SourceLabel 'winget ISCC.exe'
        }
    }
    throw 'winget completed but Inno Setup 6.7.3 ISCC.exe was not found'
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

if ($ReuseVerifiedPackage -and -not $PackageManifest) {
    throw 'ReuseVerifiedPackage requires PackageManifest'
}
& $PackageScript -Clean:$Clean -ReuseVerifiedOutput:$ReuseVerifiedPackage `
    -PackageManifest $PackageManifest
if ($LASTEXITCODE -ne 0) { throw "package.ps1 failed with exit code $LASTEXITCODE" }

$distribution = Join-Path $Root 'dist\RestreamStudio'
foreach ($required in ('RestreamStudio.exe', 'RestreamStudioUpdateHelper.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $distribution $required) -PathType Leaf)) {
        throw "packaged executable is missing: $required"
    }
}
$updateHelper = Join-Path $distribution 'RestreamStudioUpdateHelper.exe'
$helperSmokeDirectory = Join-Path $Root "build\RestreamStudioUpdateHelper-smoke-$([Guid]::NewGuid().ToString('N'))"
$helperProcess = $null
try {
    New-Item -ItemType Directory -Force -Path $helperSmokeDirectory | Out-Null
    $copiedUpdateHelper = Join-Path $helperSmokeDirectory `
        "RestreamStudioUpdateHelper-smoke-$([Guid]::NewGuid().ToString('N')).exe"
    Copy-Item -LiteralPath $updateHelper -Destination $copiedUpdateHelper
    $helperProcess = Start-Process -FilePath $copiedUpdateHelper -ArgumentList '--invalid' `
        -WindowStyle Hidden -Wait -PassThru
    if ($helperProcess.ExitCode -ne 2) {
        throw "packaged update helper smoke failed with exit code $($helperProcess.ExitCode)"
    }
    $cleanupDeadline = [DateTime]::UtcNow.AddSeconds(15)
    while ((Test-Path -LiteralPath $helperSmokeDirectory) -and
        [DateTime]::UtcNow -lt $cleanupDeadline) {
        Start-Sleep -Milliseconds 100
    }
    if (Test-Path -LiteralPath $helperSmokeDirectory) {
        throw 'packaged update helper did not clean its unique runtime directory'
    }
}
finally {
    if ($null -ne $helperProcess) { $helperProcess.Dispose() }
    if (Test-Path -LiteralPath $helperSmokeDirectory) {
        Remove-Item -LiteralPath $helperSmokeDirectory -Recurse -Force
    }
}

$iscc = Resolve-Iscc
New-Item -ItemType Directory -Force -Path $InstallerDirectory | Out-Null
Invoke-External $iscc @($InstallerScript)
if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
    throw "installer output is missing: $Installer"
}

$hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "INSTALLER_PATH=$Installer"
Write-Output "SHA256=$hash"
if ($BuildSmokeFixtures) {
    if ($Version -cne '0.1.0') {
        throw 'smoke upgrade fixture is defined only for the 0.1.0 release baseline'
    }
    Invoke-External $iscc @(
        '/DMyAppVersion=0.1.1', '/DSmokeTestBuild=1', $InstallerScript
    )
    if (-not (Test-Path -LiteralPath $SmokeUpgradeInstaller -PathType Leaf)) {
        throw "smoke upgrade installer is missing: $SmokeUpgradeInstaller"
    }
    $smokeHash = (Get-FileHash -LiteralPath $SmokeUpgradeInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Output "SMOKE_UPGRADE_INSTALLER_PATH=$SmokeUpgradeInstaller"
    Write-Output "SMOKE_UPGRADE_SHA256=$smokeHash"
}
