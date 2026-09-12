[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace RestreamStudio {
    public sealed class KillOnCloseJob : IDisposable {
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
        private const uint CREATE_SUSPENDED = 0x00000004;
        private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
        private const uint CREATE_NO_WINDOW = 0x08000000;
        private const uint STARTF_USESHOWWINDOW = 0x00000001;
        private const short SW_HIDE = 0;
        private IntPtr handle;

        [StructLayout(LayoutKind.Sequential)]
        private struct BasicLimitInformation {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedLimitInformation {
            public BasicLimitInformation BasicLimitInformation;
            public IoCounters IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct StartupInfo {
            public int Size;
            public string Reserved;
            public string Desktop;
            public string Title;
            public int X;
            public int Y;
            public int XSize;
            public int YSize;
            public int XCountChars;
            public int YCountChars;
            public int FillAttribute;
            public uint Flags;
            public short ShowWindow;
            public short Reserved2Size;
            public IntPtr Reserved2;
            public IntPtr StandardInput;
            public IntPtr StandardOutput;
            public IntPtr StandardError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ProcessInformation {
            public IntPtr Process;
            public IntPtr Thread;
            public uint ProcessId;
            public uint ThreadId;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(
            IntPtr job, int informationClass, IntPtr information, uint length);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CreateProcessW(
            string applicationName,
            StringBuilder commandLine,
            IntPtr processAttributes,
            IntPtr threadAttributes,
            bool inheritHandles,
            uint creationFlags,
            IntPtr environment,
            string currentDirectory,
            ref StartupInfo startupInfo,
            out ProcessInformation processInformation);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr thread);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateProcess(IntPtr process, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        public KillOnCloseJob() {
            handle = CreateJobObject(IntPtr.Zero, null);
            if (handle == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            var limits = new ExtendedLimitInformation();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            int size = Marshal.SizeOf(limits);
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try {
                Marshal.StructureToPtr(limits, buffer, false);
                if (!SetInformationJobObject(handle, 9, buffer, (uint)size))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            catch {
                CloseHandle(handle);
                handle = IntPtr.Zero;
                throw;
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }

        private static string QuoteCommandLineArgument(string argument) {
            if (argument.Length == 0) return "\"\"";
            if (argument.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '\"' }) < 0) return argument;

            var quoted = new StringBuilder(argument.Length + 2);
            quoted.Append('\"');
            int backslashes = 0;
            foreach (char character in argument) {
                if (character == '\\') {
                    backslashes++;
                    continue;
                }
                if (character == '\"') {
                    quoted.Append('\\', (backslashes * 2) + 1);
                    quoted.Append('\"');
                    backslashes = 0;
                    continue;
                }
                quoted.Append('\\', backslashes);
                quoted.Append(character);
                backslashes = 0;
            }
            quoted.Append('\\', backslashes * 2);
            quoted.Append('\"');
            return quoted.ToString();
        }

        private static StringBuilder BuildCommandLine(
            string executable, IEnumerable<string> arguments) {
            var commandLine = new StringBuilder(QuoteCommandLineArgument(executable));
            foreach (string argument in arguments) {
                commandLine.Append(' ');
                commandLine.Append(QuoteCommandLineArgument(argument));
            }
            return commandLine;
        }

        public Process StartProcessSuspended(
            string executable, string[] arguments, string workingDirectory) {
            if (handle == IntPtr.Zero) throw new ObjectDisposedException("KillOnCloseJob");
            if (String.IsNullOrWhiteSpace(executable))
                throw new ArgumentException("Executable is required.", "executable");
            if (arguments == null) throw new ArgumentNullException("arguments");
            if (String.IsNullOrWhiteSpace(workingDirectory))
                throw new ArgumentException("Working directory is required.", "workingDirectory");

            var startupInfo = new StartupInfo {
                Size = Marshal.SizeOf(typeof(StartupInfo)),
                Flags = STARTF_USESHOWWINDOW,
                ShowWindow = SW_HIDE
            };
            ProcessInformation processInfo = new ProcessInformation();
            Process managedProcess = null;
            bool created = false;
            try {
                uint creationFlags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW;
                if (!CreateProcessW(
                    executable,
                    BuildCommandLine(executable, arguments),
                    IntPtr.Zero,
                    IntPtr.Zero,
                    false,
                    creationFlags,
                    IntPtr.Zero,
                    workingDirectory,
                    ref startupInfo,
                    out processInfo))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                created = true;

                if (!AssignProcessToJobObject(handle, processInfo.Process))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                managedProcess = Process.GetProcessById(checked((int)processInfo.ProcessId));
                IntPtr managedHandle = managedProcess.Handle;
                if (managedHandle == IntPtr.Zero)
                    throw new Win32Exception("Managed process handle is unavailable.");
                if (ResumeThread(processInfo.Thread) == UInt32.MaxValue)
                    throw new Win32Exception(Marshal.GetLastWin32Error());

                return managedProcess;
            }
            catch {
                if (managedProcess != null) managedProcess.Dispose();
                if (created) {
                    TerminateJobObject(handle, 1);
                    TerminateProcess(processInfo.Process, 1);
                }
                throw;
            }
            finally {
                if (processInfo.Thread != IntPtr.Zero) CloseHandle(processInfo.Thread);
                if (processInfo.Process != IntPtr.Zero) CloseHandle(processInfo.Process);
            }
        }

        public void Dispose() {
            if (handle == IntPtr.Zero) return;
            CloseHandle(handle);
            handle = IntPtr.Zero;
        }
    }
}
'@

$Root = Split-Path -Parent $PSScriptRoot
$FrontendRoot = Join-Path $Root 'frontend'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$LockFile = Join-Path $Root 'package-lock.json'
$ViteCandidates = @(
    (Join-Path $Root 'node_modules\vite\bin\vite.js')
    (Join-Path $Root 'frontend\node_modules\vite\bin\vite.js')
)
$FrontendPort = 5173

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'run: py -3.12 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e .[dev]'
}
if (-not (Test-Path -LiteralPath $LockFile -PathType Leaf)) {
    throw 'package-lock.json is required before starting the frontend'
}
$Npm = (Get-Command npm.cmd -CommandType Application -ErrorAction Stop |
    Select-Object -First 1).Source
$Node = (Get-Command node.exe -CommandType Application -ErrorAction Stop |
    Select-Object -First 1).Source
$frontend = $null
$backend = $null
$ProcessJob = $null
$oldFFmpeg = [Environment]::GetEnvironmentVariable('FFMPEG_PATH')
$oldFFprobe = [Environment]::GetEnvironmentVariable('FFPROBE_PATH')
$oldData = [Environment]::GetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR')

function Set-ToolEnvironment {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$EnvironmentName)
    if ([Environment]::GetEnvironmentVariable($EnvironmentName)) { return }
    $repositoryTool = Join-Path $Root "tools\ffmpeg\$Name.exe"
    if (Test-Path -LiteralPath $repositoryTool -PathType Leaf) {
        [Environment]::SetEnvironmentVariable($EnvironmentName, $repositoryTool)
        return
    }
    $command = Get-Command "$Name.exe" -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $command) {
        [Environment]::SetEnvironmentVariable($EnvironmentName, $command.Source)
    }
}

function Test-FrontendHomepage {
    param([Parameter(Mandatory)][int]$Port)
    try {
        $response = Invoke-WebRequest -Method Get -UseBasicParsing `
            -Uri "http://127.0.0.1:$Port/" -TimeoutSec 2
        return $response.StatusCode -eq 200 -and
            $response.Content -match '<title>\s*Restream Studio\s*</title>'
    }
    catch { return $false }
}

function Stop-ExactProcess {
    param([Diagnostics.Process]$Process)
    if ($null -eq $Process) { return }
    try {
        $Process.Refresh()
        if (-not $Process.HasExited) { Stop-Process -Id $Process.Id -Force }
        if (-not $Process.WaitForExit(10000)) {
            throw "process $($Process.Id) did not exit"
        }
    }
    finally {
        $Process.Dispose()
    }
}

Push-Location $Root
try {
    $ProcessJob = [RestreamStudio.KillOnCloseJob]::new()
    [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR', (Join-Path $Root 'runtime'))
    Set-ToolEnvironment -Name 'ffmpeg' -EnvironmentName 'FFMPEG_PATH'
    Set-ToolEnvironment -Name 'ffprobe' -EnvironmentName 'FFPROBE_PATH'
    & $Npm ci --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
    $ViteScript = $ViteCandidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
    if ($null -eq $ViteScript) {
        throw 'Vite entry point is missing after npm ci'
    }
    $frontend = $ProcessJob.StartProcessSuspended(
        $Node,
        [string[]]@($ViteScript, '--host', '127.0.0.1', '--port', [string]$FrontendPort, '--strictPort'),
        $FrontendRoot
    )

    $readiness = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-FrontendHomepage -Port $FrontendPort)) {
        $frontend.Refresh()
        if ($frontend.HasExited) { throw "Vite exited before readiness with code $($frontend.ExitCode)" }
        if ($readiness.Elapsed.TotalSeconds -ge 15) { throw 'Vite readiness timed out' }
        Start-Sleep -Milliseconds 100
    }
    $frontend.Refresh()
    if ($frontend.HasExited) { throw "Vite exited before readiness with code $($frontend.ExitCode)" }

    $backend = $ProcessJob.StartProcessSuspended(
        $Python,
        [string[]]@('-m', 'restream_studio.main'),
        $Root
    )
    while ($true) {
        $frontend.Refresh()
        $backend.Refresh()
        if ($frontend.HasExited) { throw "Vite exited with code $($frontend.ExitCode)" }
        if ($backend.HasExited) { throw "backend exited with code $($backend.ExitCode)" }
        Start-Sleep -Milliseconds 250
    }
}
finally {
    try {
        try {
            Stop-ExactProcess -Process $frontend
        }
        finally {
            try {
                Stop-ExactProcess -Process $backend
            }
            finally {
                if ($null -ne $ProcessJob) { $ProcessJob.Dispose() }
            }
        }
    }
    finally {
        [Environment]::SetEnvironmentVariable('FFMPEG_PATH', $oldFFmpeg)
        [Environment]::SetEnvironmentVariable('FFPROBE_PATH', $oldFFprobe)
        [Environment]::SetEnvironmentVariable('RESTREAM_STUDIO_DATA_DIR', $oldData)
        Pop-Location
    }
}
