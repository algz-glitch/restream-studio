"""Asynchronous update orchestration with a small, secret-safe state surface."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from restream_studio import __version__

from .client import GitHubUpdateClient
from .contracts import (
    DownloadResult,
    DownloadStatus,
    UpdateCheckResult,
    UpdateErrorCode,
    UpdateManifest,
)

CHECK_INTERVAL = timedelta(hours=24)
_PERSISTED_ERROR_CODES = {item.value for item in UpdateErrorCode} | {
    "check_failed",
    "download_failed",
}


class UpdateStatus(StrEnum):
    IDLE = "idle"
    CHECKING = "checking"
    CURRENT = "current"
    AVAILABLE = "available"
    DOWNLOADING = "downloading"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UpdateSnapshot:
    status: UpdateStatus
    current_version: str
    available_version: str | None = None
    release_url: str | None = None
    last_checked_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        has_update = self.status in {
            UpdateStatus.AVAILABLE,
            UpdateStatus.DOWNLOADING,
            UpdateStatus.READY,
        }
        if has_update != (self.available_version is not None):
            raise ValueError("update states must contain exactly one available version")
        failed = self.status is UpdateStatus.FAILED
        if failed != (self.error_code is not None and self.error_message is not None):
            raise ValueError("failed states must contain exactly one public error")
        if not failed and (self.error_code is not None or self.error_message is not None):
            raise ValueError("non-failed states cannot contain errors")
        if not has_update and self.release_url is not None:
            raise ValueError("only update states can contain a release URL")


class UpdateOperationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


class UpdateClientPort(Protocol):
    def check_for_update(self, current_version: str) -> UpdateCheckResult: ...

    def download_installer_result(
        self, manifest: UpdateManifest, destination: Path
    ) -> DownloadResult: ...


Clock = Callable[[], datetime]
Launcher = Callable[[Sequence[str]], object]
ShutdownCallback = Callable[[], Awaitable[None] | None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _launch(command: Sequence[str]) -> object:
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    return subprocess.Popen(
        list(command),
        close_fds=True,
        creationflags=flags,
    )


def _default_helper_command() -> tuple[str, ...]:
    if getattr(sys, "frozen", False):
        return (str(Path(sys.executable).with_name("RestreamStudioUpdateHelper.exe")),)
    return (sys.executable, "-m", "restream_studio.update.helper")


def _default_shutdown() -> None:
    os.kill(os.getpid(), 15)


class UpdateService:
    def __init__(
        self,
        update_dir: Path,
        *,
        current_version: str = __version__,
        client: UpdateClientPort | None = None,
        clock: Clock = _utc_now,
        launcher: Launcher = _launch,
        shutdown_callback: ShutdownCallback = _default_shutdown,
        helper_command: Sequence[str] | None = None,
        executable: Path | None = None,
    ) -> None:
        self._update_dir = Path(update_dir).resolve(strict=False)
        self._state_file = self._update_dir / "state.json"
        self._current_version = current_version
        self._client = client or GitHubUpdateClient()
        self._clock = clock
        self._launcher = launcher
        self._shutdown_callback = shutdown_callback
        self._helper_command = tuple(helper_command or _default_helper_command())
        self._executable = Path(executable or sys.executable).resolve(strict=False)
        self._operation_lock = asyncio.Lock()
        self._background_task: asyncio.Task[None] | None = None
        self._shutdown_tasks: set[asyncio.Task[None]] = set()
        self._manifest: UpdateManifest | None = None
        self._installer: Path | None = None
        self._install_started = False
        self._state = self._load_state()

    def snapshot(self) -> UpdateSnapshot:
        return self._state

    async def check(self) -> UpdateSnapshot:
        return await self._run_check(force=True)

    async def automatic_check(self) -> UpdateSnapshot:
        return await self._run_check(force=False)

    async def _run_check(self, *, force: bool) -> UpdateSnapshot:
        now = self._aware_now()
        if (
            not force
            and self._state.last_checked_at is not None
            and now - self._state.last_checked_at < CHECK_INTERVAL
        ):
            return self._state
        if self._operation_lock.locked():
            raise UpdateOperationError("check_in_progress", "Update operation already in progress")
        async with self._operation_lock:
            self._state = UpdateSnapshot(UpdateStatus.CHECKING, self._current_version)
            try:
                result = await asyncio.to_thread(
                    self._client.check_for_update, self._current_version
                )
            except Exception:  # noqa: BLE001 - implementation details stay behind state API
                self._manifest = None
                self._installer = None
                self._state = UpdateSnapshot(
                    UpdateStatus.FAILED,
                    self._current_version,
                    last_checked_at=self._aware_now(),
                    error_code="check_failed",
                    error_message="Update check failed",
                )
                await asyncio.to_thread(self._persist_state)
                return self._state
            checked_at = self._aware_now()
            self._installer = None
            self._install_started = False
            if result.update_available and result.manifest is not None:
                self._manifest = result.manifest
                self._state = UpdateSnapshot(
                    UpdateStatus.AVAILABLE,
                    self._current_version,
                    available_version=str(result.manifest.version),
                    release_url=result.manifest.release_url,
                    last_checked_at=checked_at,
                )
            elif result.error_code is not None:
                self._manifest = None
                self._state = UpdateSnapshot(
                    UpdateStatus.FAILED,
                    self._current_version,
                    last_checked_at=checked_at,
                    error_code=result.error_code.value,
                    error_message="Update check failed",
                )
            else:
                self._manifest = None
                self._state = UpdateSnapshot(
                    UpdateStatus.CURRENT,
                    self._current_version,
                    last_checked_at=checked_at,
                )
            await asyncio.to_thread(self._persist_state)
            return self._state

    async def download(self) -> UpdateSnapshot:
        if self._operation_lock.locked() or self._state.status is UpdateStatus.DOWNLOADING:
            raise UpdateOperationError("download_in_progress", "Download already in progress")
        if self._state.status is not UpdateStatus.AVAILABLE or self._manifest is None:
            raise UpdateOperationError("update_check_required", "Check for an update first")
        async with self._operation_lock:
            selected = self._manifest
            destination = (
                self._update_dir / f"RestreamStudio-Setup-{selected.version}.exe"
            ).resolve(strict=False)
            self._state = UpdateSnapshot(
                UpdateStatus.DOWNLOADING,
                self._current_version,
                available_version=str(selected.version),
                release_url=selected.release_url,
                last_checked_at=self._state.last_checked_at,
            )
            self._update_dir.mkdir(parents=True, exist_ok=True)
            try:
                result = await asyncio.to_thread(
                    self._client.download_installer_result, selected, destination
                )
            except Exception:  # noqa: BLE001 - implementation details stay behind state API
                result = DownloadResult.failed(
                    selected, UpdateErrorCode.DOWNLOAD_FAILED, "download failed"
                )
            if result.status is DownloadStatus.SUCCESS and result.path is not None:
                self._installer = result.path.resolve(strict=False)
                self._state = UpdateSnapshot(
                    UpdateStatus.READY,
                    self._current_version,
                    available_version=str(selected.version),
                    release_url=selected.release_url,
                    last_checked_at=self._state.last_checked_at,
                )
            else:
                self._installer = None
                code = result.error_code
                self._state = UpdateSnapshot(
                    UpdateStatus.FAILED,
                    self._current_version,
                    last_checked_at=self._state.last_checked_at,
                    error_code=code.value if code is not None else "download_failed",
                    error_message="Update download failed",
                )
            await asyncio.to_thread(self._persist_state)
            return self._state

    async def install(self, *, current_pid: int) -> None:
        if self._operation_lock.locked() or self._install_started:
            raise UpdateOperationError("install_in_progress", "Installation already requested")
        if (
            self._state.status is not UpdateStatus.READY
            or self._manifest is None
            or self._installer is None
        ):
            raise UpdateOperationError("update_not_ready", "A verified installer is required")
        async with self._operation_lock:
            if current_pid <= 0:
                raise UpdateOperationError("invalid_process", "Application process is invalid")
            valid = await asyncio.to_thread(
                self._verify_installer, self._installer, self._manifest
            )
            if not valid:
                raise UpdateOperationError(
                    "installer_verification_failed", "Installer verification failed"
                )
            command = [
                *self._helper_command,
                "--pid",
                str(current_pid),
                "--installer",
                str(self._installer),
                "--executable",
                str(self._executable),
            ]
            try:
                self._launcher(command)
            except OSError as error:
                raise UpdateOperationError(
                    "helper_launch_failed", "Update helper could not be started"
                ) from error
            self._install_started = True
            task = asyncio.create_task(self._invoke_shutdown())
            self._shutdown_tasks.add(task)
            task.add_done_callback(self._shutdown_tasks.discard)

    async def _invoke_shutdown(self) -> None:
        result = self._shutdown_callback()
        if inspect.isawaitable(result):
            await result

    def start_background(self) -> None:
        if self._background_task is None or self._background_task.done():
            self._background_task = asyncio.create_task(self._background_check())

    async def _background_check(self) -> None:
        try:
            await self.automatic_check()
        except (UpdateOperationError, asyncio.CancelledError):
            raise
        except Exception:  # noqa: BLE001 - startup update failures cannot stop the app
            return

    async def shutdown(self) -> None:
        task = self._background_task
        self._background_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("update clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _verify_installer(path: Path, manifest: UpdateManifest) -> bool:
        expected_name = f"RestreamStudio-Setup-{manifest.version}.exe"
        try:
            if not path.is_absolute() or path.name != expected_name or not path.is_file():
                return False
            if path.stat().st_size != manifest.size:
                return False
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(64 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest() == manifest.sha256
        except OSError:
            return False

    def _persist_state(self) -> None:
        self._update_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "available_version": self._state.available_version,
            "current_version": self._state.current_version,
            "error_code": self._state.error_code,
            "last_checked_at": (
                self._state.last_checked_at.isoformat()
                if self._state.last_checked_at is not None
                else None
            ),
            "status": self._state.status.value,
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".state.", suffix=".json.tmp", dir=self._update_dir
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                json.dump(payload, output, ensure_ascii=True, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._state_file)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_state(self) -> UpdateSnapshot:
        idle = UpdateSnapshot(UpdateStatus.IDLE, self._current_version)
        try:
            payload = json.loads(self._state_file.read_text("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "available_version",
                "current_version",
                "error_code",
                "last_checked_at",
                "status",
            }:
                return idle
            if payload["current_version"] != self._current_version:
                return idle
            status = UpdateStatus(payload["status"])
            if status in {UpdateStatus.CHECKING, UpdateStatus.DOWNLOADING, UpdateStatus.READY}:
                return idle
            timestamp = payload["last_checked_at"]
            checked = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else None
            if checked is not None and (checked.tzinfo is None or checked.utcoffset() is None):
                return idle
            available = payload["available_version"]
            error_code = payload["error_code"]
            if status is UpdateStatus.AVAILABLE:
                if not isinstance(available, str) or error_code is not None:
                    return idle
                return UpdateSnapshot(
                    status,
                    self._current_version,
                    available_version=available,
                    last_checked_at=checked,
                )
            if status is UpdateStatus.FAILED:
                if (
                    not isinstance(error_code, str)
                    or error_code not in _PERSISTED_ERROR_CODES
                    or available is not None
                ):
                    return idle
                return UpdateSnapshot(
                    status,
                    self._current_version,
                    last_checked_at=checked,
                    error_code=error_code,
                    error_message="Update operation failed",
                )
            if status is UpdateStatus.CURRENT:
                if available is not None or error_code is not None:
                    return idle
                return UpdateSnapshot(status, self._current_version, last_checked_at=checked)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return idle
        return idle
