"""Offline update helper: wait for the app, install, and restart."""

from __future__ import annotations

import argparse
import base64
import hashlib
import math
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import psutil  # type: ignore[import-untyped]

from .contracts import MAX_INSTALLER_BYTES

INSTALLER_ARGUMENTS = (
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/RESTARTAPPLICATIONS",
)
PID_WAIT_SECONDS = 120.0
_INSTALLER_NAME = re.compile(r"RestreamStudio-Setup-(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.exe\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class HelperArguments:
    pid: int
    installer: Path
    executable: Path
    expected_sha256: str
    expected_size: int
    process_created_at: float

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("PID must be a positive integer")
        if not self.installer.is_absolute() or not self.executable.is_absolute():
            raise ValueError("installer and executable paths must be absolute")
        if not self.installer.is_file():
            raise ValueError("installer must exist")
        if _INSTALLER_NAME.fullmatch(self.installer.name) is None:
            raise ValueError("installer name does not match the release contract")
        if self.executable.name.casefold() != "restreamstudio.exe".casefold():
            raise ValueError("executable basename must be RestreamStudio.exe")
        if (
            not isinstance(self.expected_sha256, str)
            or _SHA256.fullmatch(self.expected_sha256) is None
        ):
            raise ValueError("expected SHA-256 is invalid")
        if (
            type(self.expected_size) is not int
            or self.expected_size <= 0
            or self.expected_size > MAX_INSTALLER_BYTES
        ):
            raise ValueError("expected size is invalid")
        if (
            type(self.process_created_at) is not float
            or not math.isfinite(self.process_created_at)
            or self.process_created_at <= 0
        ):
            raise ValueError("process creation time is invalid")


def parse_arguments(arguments: Sequence[str]) -> HelperArguments:
    required = (
        "--pid",
        "--installer",
        "--executable",
        "--expected-sha256",
        "--expected-size",
        "--process-created-at",
    )
    if len(arguments) != 12 or any(arguments.count(option) != 1 for option in required):
        raise ValueError("helper arguments are invalid")
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    parser.add_argument("--pid", required=True)
    parser.add_argument("--installer", required=True)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-size", required=True)
    parser.add_argument("--process-created-at", required=True)
    try:
        values = parser.parse_args(list(arguments))
        if not str(values.pid).isascii() or not str(values.pid).isdecimal():
            raise ValueError("PID must be a positive integer")
        size = str(values.expected_size)
        if not size.isascii() or not size.isdecimal():
            raise ValueError("expected size is invalid")
        try:
            process_created_at = float(values.process_created_at)
        except ValueError:
            raise ValueError("process creation time is invalid") from None
        return HelperArguments(
            int(values.pid),
            Path(values.installer),
            Path(values.executable),
            str(values.expected_sha256),
            int(size),
            process_created_at,
        )
    except (argparse.ArgumentError, TypeError) as error:
        raise ValueError("helper arguments are invalid") from error


class ProcessPort(Protocol):
    def create_time(self) -> float: ...
    def wait(self, timeout: float) -> object: ...


ProcessFactory = Callable[[int], ProcessPort]
CleanupLauncher = Callable[[Sequence[str]], object]


def wait_for_process(
    pid: int,
    process_created_at: float,
    timeout: float,
    *,
    process_factory: ProcessFactory = psutil.Process,
) -> bool:
    try:
        process = process_factory(pid)
        if not math.isclose(
            process.create_time(), process_created_at, rel_tol=0.0, abs_tol=1e-6
        ):
            return True
        process.wait(timeout=timeout)
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        return False
    except psutil.Error:
        return False
    return True


def verify_installer(path: Path, expected_size: int, expected_sha256: str) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != expected_size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == expected_sha256
    except OSError:
        return False


def _run_installer(command: Sequence[str]) -> int:
    completed = subprocess.run(list(command), check=False)
    return completed.returncode


def _launch(command: Sequence[str]) -> object:
    return subprocess.Popen(list(command), close_fds=True)


def _launch_cleanup(command: Sequence[str]) -> object:
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        list(command),
        close_fds=True,
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def schedule_self_cleanup(
    executable: Path | None = None,
    *,
    pid: int | None = None,
    launcher: CleanupLauncher = _launch_cleanup,
) -> None:
    if executable is None:
        if not getattr(sys, "frozen", False):
            return
        executable = Path(sys.executable)
    executable = executable.resolve(strict=False)
    if not executable.name.startswith("RestreamStudioUpdateHelper-"):
        return
    process_id = os.getpid() if pid is None else pid
    if process_id <= 0:
        return
    quoted_file = str(executable).replace("'", "''")
    quoted_directory = str(executable.parent).replace("'", "''")
    cleanup = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"Wait-Process -Id {process_id};"
        "for($i=0;$i-lt 150;$i++){"
        f"Remove-Item -LiteralPath '{quoted_file}' -Force;"
        f"if(-not (Test-Path -LiteralPath '{quoted_file}')){{break}};"
        "Start-Sleep -Milliseconds 100};"
        f"Remove-Item -LiteralPath '{quoted_directory}' -Force"
    )
    encoded = base64.b64encode(cleanup.encode("utf-16le")).decode("ascii")
    powershell = Path(
        os.environ.get("SystemRoot", r"C:\Windows")
    ) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    launcher((str(powershell), "-NoProfile", "-EncodedCommand", encoded))


def run_helper(
    arguments: HelperArguments,
    *,
    wait_for_pid: Callable[[int, float, float], bool] = wait_for_process,
    run_installer: Callable[[Sequence[str]], int] = _run_installer,
    launch: Callable[[Sequence[str]], object] = _launch,
) -> int:
    if not wait_for_pid(
        arguments.pid, arguments.process_created_at, PID_WAIT_SECONDS
    ):
        return 2
    if not verify_installer(
        arguments.installer, arguments.expected_size, arguments.expected_sha256
    ):
        return 5
    result = run_installer([str(arguments.installer), *INSTALLER_ARGUMENTS])
    if result != 0:
        return 3
    if not arguments.executable.is_file():
        return 4
    launch([str(arguments.executable)])
    return 0


def main(
    arguments: Sequence[str] | None = None,
    *,
    parser: Callable[[Sequence[str]], HelperArguments] = parse_arguments,
    runner: Callable[[HelperArguments], int] = run_helper,
    cleanup_scheduler: Callable[[], None] = schedule_self_cleanup,
) -> int:
    try:
        try:
            parsed = parser(sys.argv[1:] if arguments is None else arguments)
        except (ValueError, SystemExit) as error:
            print(f"update helper: {error}", file=sys.stderr)
            return 2
        return runner(parsed)
    finally:
        try:
            cleanup_scheduler()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
