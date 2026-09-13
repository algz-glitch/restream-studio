[CmdletBinding()]
param([switch]$SkipInstallerLifecycle)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Package = Join-Path $PSScriptRoot 'package.ps1'
$BuildInstaller = Join-Path $PSScriptRoot 'build-installer.ps1'
$SmokeInstallerBroker = Join-Path $PSScriptRoot 'run-smoke-installer-elevated.ps1'
$Distribution = Join-Path $Root 'dist\RestreamStudio'
$TreeManifestTool = Join-Path $Root 'scripts\write-tree-manifest.py'
$ManifestDirectory = Join-Path $Root 'artifacts\release-verification'
$FrontendManifest = Join-Path $ManifestDirectory 'frontend-build-manifest.json'
$PackageManifest = Join-Path $ManifestDirectory 'package-manifest.json'
$CurrentCommit = (& git -C $Root rev-parse --verify HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $CurrentCommit -notmatch '^[0-9a-f]{40}$') {
    throw 'current git commit identity is invalid'
}
New-Item -ItemType Directory -Force -Path $ManifestDirectory | Out-Null

function Get-ProjectVersion {
    $semver = '(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    $pyproject = [IO.File]::ReadAllText((Join-Path $Root 'pyproject.toml'))
    $matches = [Regex]::Matches($pyproject, "(?m)^version\s*=\s*`"($semver)`"\s*$")
    if ($matches.Count -ne 1) {
        throw 'pyproject.toml must contain exactly one canonical project version'
    }
    $version = $matches[0].Groups[1].Value
    $packageVersion = ([IO.File]::ReadAllText((Join-Path $Root 'package.json')) |
        ConvertFrom-Json).version
    if ($packageVersion -isnot [string] -or $packageVersion -cne $version) {
        throw 'package.json version must exactly match pyproject.toml'
    }
    return $version
}

$ProjectVersion = Get-ProjectVersion
$Installer = Join-Path $Root "dist\installer\RestreamStudio-Setup-$ProjectVersion.exe"
$SmokeUpgradeInstaller = ''

function Invoke-External {
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$Arguments = @())
    & $FilePath @Arguments | ForEach-Object { [Console]::Error.WriteLine([string]$_) }
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

function Test-LocalPortAvailable {
    param([Parameter(Mandatory)][int]$Port)
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try {
        $listener.Start()
        return $true
    }
    catch { return $false }
    finally { $listener.Stop() }
}

function Wait-LocalPortReleased {
    param([Parameter(Mandatory)][int]$Port, [int]$TimeoutSeconds = 10)
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if (Test-LocalPortAvailable -Port $Port) { return $true }
        Start-Sleep -Milliseconds 100
    }
    return $false
}

function Assert-Distribution {
    $required = @(
        'RestreamStudio.exe',
        'RestreamStudioUpdateHelper.exe',
        'Enable-Localhost.ps1',
        'Enable-Localhost.cmd',
        '_internal\restream_studio\static\index.html',
        '_internal\restream_studio\version.txt',
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
                $homepage = Invoke-WebRequest -Method Get -UseBasicParsing `
                    -Uri "http://127.0.0.1:$port/" -TimeoutSec 2
                if (
                    $health.status -eq 'ok' -and
                    $health.app -eq 'restream-studio' -and
                    $health.version -eq $ProjectVersion -and
                    $homepage.StatusCode -eq 200 -and
                    $homepage.Content -match '<title>\s*Restream Studio\s*</title>'
                ) { return }
            }
            catch { Start-Sleep -Milliseconds 250 }
        }
        throw 'packaged /health readiness timed out'
    }
    finally {
        try {
            [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_PORT', $oldPort)
            [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR', $oldData)
        }
        finally {
            if ($null -ne $process) {
                try {
                    $process.Refresh()
                    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
                    if (-not $process.WaitForExit(10000)) {
                        throw 'packaged executable did not exit after Stop-Process'
                    }
                    if (-not (Wait-LocalPortReleased -Port $port)) {
                        throw "packaged port was not released: $port"
                    }
                }
                finally {
                    $process.Dispose()
                }
            }
        }
    }
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Write-Output 'PYTHON_LINT=FAIL'
    [Console]::Error.WriteLine('Python virtual environment is missing')
    exit 1
}
$Npm = ''

Push-Location $Root
try {
    Invoke-Gate -Name 'PYTHON_LINT' -Action {
        Invoke-External $Python @('-m', 'ruff', 'check', 'src', 'tests', 'scripts')
    }
    Invoke-Gate -Name 'PYTHON_TYPES' -Action {
        Invoke-External $Python @('-m', 'mypy', 'src', 'tests', 'scripts')
    }
    Invoke-Gate -Name 'FRONTEND_TYPES' -Action {
        try {
            $NpmCommand = Get-Command npm.cmd -CommandType Application -ErrorAction Stop |
                Select-Object -First 1
            $script:Npm = $NpmCommand.Source
            if (-not (Test-Path -LiteralPath (Join-Path $Root 'package-lock.json') -PathType Leaf)) {
                throw 'package-lock.json is missing; npm dependency closure is not reproducible'
            }
            Invoke-External $Npm @('ci', '--ignore-scripts', '--no-audit', '--no-fund')
        }
        catch {
            [Console]::Error.WriteLine("FRONTEND_DEPS blocker: $($_.Exception.Message)")
            throw
        }
        Invoke-External $Npm @('run', 'typecheck')
    }
    Invoke-Gate -Name 'FRONTEND_LINT' -Action { Invoke-External $Npm @('run', 'lint') }
    Invoke-Gate -Name 'FRONTEND_TESTS' -Action { Invoke-External $Npm @('test') }
    Invoke-Gate -Name 'FRONTEND_BUILD' -Action {
        Invoke-External $Npm @('run', 'frontend:build')
        Invoke-External $Python @(
            $TreeManifestTool, '--root', (Join-Path $Root 'src\restream_studio\static'),
            '--kind', 'frontend', '--commit', $CurrentCommit, '--version', $ProjectVersion,
            '--output', $FrontendManifest
        )
        Write-Output "FRONTEND_MANIFEST_PATH=$FrontendManifest"
    }
    Invoke-Gate -Name 'PYTHON_TESTS' -Action {
        $oldFrontendBuilt = [Environment]::GetEnvironmentVariable(
            'RESTREAM_STUDIO_FRONTEND_ALREADY_BUILT'
        )
        try {
            [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_FRONTEND_ALREADY_BUILT', '1')
            Invoke-External $Python @('-m', 'pytest', 'tests')
        }
        finally {
            [Environment]::SetEnvironmentVariable(
                'RESTREAM_STUDIO_FRONTEND_ALREADY_BUILT', $oldFrontendBuilt
            )
        }
    }
    Invoke-Gate -Name 'LOCAL_RTMP_E2E' -Action {
        $PowerShell = (Get-Command powershell.exe -CommandType Application -ErrorAction Stop).Source
        Invoke-External $PowerShell @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            (Join-Path $PSScriptRoot 'e2e-local.ps1')
        )
    }
    Invoke-Gate -Name 'PACKAGE_SMOKE' -Action {
        & $Package -Clean -ReuseFrontend -FrontendManifest $FrontendManifest |
            ForEach-Object { [Console]::Error.WriteLine([string]$_) }
        if ($LASTEXITCODE -ne 0) { throw "package script exited with code $LASTEXITCODE" }
        Assert-Distribution
        Test-PackageHealth
        Invoke-External $Python @(
            $TreeManifestTool, '--root', $Distribution, '--kind', 'package',
            '--commit', $CurrentCommit, '--version', $ProjectVersion,
            '--output', $PackageManifest
        )
        Write-Output "PACKAGE_MANIFEST_PATH=$PackageManifest"
    }
    Invoke-Gate -Name 'INSTALLER_BUILD' -Action {
        $oldVerifiedCommit = [Environment]::GetEnvironmentVariable(
            'RESTREAM_STUDIO_VERIFIED_COMMIT'
        )
        try {
            [Environment]::SetEnvironmentVariable(
                'RESTREAM_STUDIO_VERIFIED_COMMIT', $CurrentCommit
            )
            $upgradePaths = [Collections.Generic.List[string]]::new()
            & $BuildInstaller -Clean -ReuseVerifiedPackage `
                -PackageManifest $PackageManifest -BuildSmokeFixtures |
                ForEach-Object {
                    $line = [string]$_
                    [Console]::Error.WriteLine($line)
                    if ($line -like 'SMOKE_UPGRADE_INSTALLER_PATH=*') {
                        $upgradePaths.Add($line)
                    }
                }
            if ($LASTEXITCODE -ne 0) {
                throw "build-installer.ps1 exited with code $LASTEXITCODE"
            }
            if ($upgradePaths.Count -ne 1) {
                throw 'build-installer.ps1 did not emit exactly one smoke upgrade installer path'
            }
            $candidate = $upgradePaths[0].Substring(
                'SMOKE_UPGRADE_INSTALLER_PATH='.Length
            )
            $script:SmokeUpgradeInstaller = (
                Resolve-Path -LiteralPath $candidate -ErrorAction Stop
            ).Path
        }
        finally {
            [Environment]::SetEnvironmentVariable(
                'RESTREAM_STUDIO_VERIFIED_COMMIT', $oldVerifiedCommit
            )
        }
        if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
            throw "installer output is missing: $Installer"
        }
        Write-Output "INSTALLER_PATH=$Installer"
    }
    if ($SkipInstallerLifecycle) {
        Write-Output 'INSTALLER_LIFECYCLE=SKIP'
        [Console]::Error.WriteLine(
            'INSTALLER_LIFECYCLE skipped by explicit developer diagnostic override'
        )
    }
    else {
        Invoke-Gate -Name 'INSTALLER_LIFECYCLE' -Action {
            $PowerShell = (Get-Command powershell.exe -CommandType Application -ErrorAction Stop).Source
            Invoke-External $PowerShell @(
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $SmokeInstallerBroker,
                '-Installer', $Installer, '-UpgradeInstaller', $SmokeUpgradeInstaller,
                '-WorkspaceRoot', $Root
            )
        }
    }
}
finally { Pop-Location }
