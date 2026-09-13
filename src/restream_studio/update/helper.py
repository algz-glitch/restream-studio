"""Offline update helper: wait for the app, install, and restart."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import psutil  # type: ignore[import-untyped]

INSTALLER_ARGUMENTS = (
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/RESTARTAPPLICATIONS",
)
PID_WAIT_SECONDS = 120.0
_INSTALLER_NAME = re.compile(r"RestreamStudio-Setup-(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.exe\Z")


@dataclass(frozen=True, slots=True)
class HelperArguments:
    pid: int
    installer: Path
    executable: Path

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


def parse_arguments(arguments: Sequence[str]) -> HelperArguments:
    required = ("--pid", "--installer", "--executable")
    if len(arguments) != 6 or any(arguments.count(option) != 1 for option in required):
        raise ValueError("helper arguments are invalid")
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    parser.add_argument("--pid", required=True)
    parser.add_argument("--installer", required=True)
    parser.add_argument("--executable", required=True)
    try:
        values = parser.parse_args(list(arguments))
        if not str(values.pid).isascii() or not str(values.pid).isdecimal():
            raise ValueError("PID must be a positive integer")
        return HelperArguments(
            int(values.pid), Path(values.installer), Path(values.executable)
        )
    except argparse.ArgumentError as error:
        raise ValueError("helper arguments are invalid") from error


def wait_for_process(pid: int, timeout: float) -> bool:
    try:
        process = psutil.Process(pid)
        process.wait(timeout=timeout)
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        return False
    return True


def _run_installer(command: Sequence[str]) -> int:
    completed = subprocess.run(list(command), check=False)
    return completed.returncode


def _launch(command: Sequence[str]) -> object:
    return subprocess.Popen(list(command), close_fds=True)


def run_helper(
    arguments: HelperArguments,
    *,
    wait_for_pid: Callable[[int, float], bool] = wait_for_process,
    run_installer: Callable[[Sequence[str]], int] = _run_installer,
    launch: Callable[[Sequence[str]], object] = _launch,
) -> int:
    if not wait_for_pid(arguments.pid, PID_WAIT_SECONDS):
        return 2
    result = run_installer([str(arguments.installer), *INSTALLER_ARGUMENTS])
    if result != 0:
        return 3
    if not arguments.executable.is_file():
        return 4
    launch([str(arguments.executable)])
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        parsed = parse_arguments(sys.argv[1:] if arguments is None else arguments)
    except (ValueError, SystemExit) as error:
        print(f"update helper: {error}", file=sys.stderr)
        return 2
    return run_helper(parsed)


if __name__ == "__main__":
    raise SystemExit(main())
