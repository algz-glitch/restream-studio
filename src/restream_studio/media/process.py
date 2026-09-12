"""Shell-free asynchronous child-process lifecycle and FFmpeg progress capture."""

from __future__ import annotations

import asyncio
import ctypes
import math
import os
import re
import signal
import subprocess
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from restream_studio.security.redaction import redact

_PROGRESS_KEYS: Final = frozenset({"fps", "bitrate", "speed", "out_time", "progress"})
_TIME_PATTERN: Final = re.compile(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$")
_AUTH_FAILURE_PATTERN: Final = re.compile(
    r"(?ix)(?:"
    r"\bauthentication\s+failed\b|"
    r"\bauthorization\s+failed\b|"
    r"\bunauthorized\b|"
    r"\binvalid\s+stream\s+key\b|"
    r"\bpublish\s+denied\b|"
    r"\bhttp(?:/\d(?:\.\d)?)?\s+403\b|"
    r"\bserver\s+returned\s+403\b|"
    r"\b403\s+forbidden\b"
    r")"
)
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: Final = 9
_PROCESS_ASSIGN_RIGHTS: Final = 0x0001 | 0x0100 | 0x1000


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsJob:
    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle

    @classmethod
    def attach(cls, pid: int) -> _WindowsJob | None:
        if os.name != "nt":
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        raw_job_handle = kernel32.CreateJobObjectW(None, None)
        job_handle = int(raw_job_handle or 0)
        if not job_handle:
            return None
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            job_handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        raw_process_handle = kernel32.OpenProcess(_PROCESS_ASSIGN_RIGHTS, False, pid)
        process_handle = int(raw_process_handle or 0)
        if not configured or not process_handle:
            if process_handle:
                kernel32.CloseHandle(process_handle)
            kernel32.CloseHandle(job_handle)
            return None
        try:
            assigned = kernel32.AssignProcessToJobObject(job_handle, process_handle)
        finally:
            kernel32.CloseHandle(process_handle)
        if not assigned:
            kernel32.CloseHandle(job_handle)
            return None
        return cls(job_handle)

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(handle)


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    pid: int | None
    running: bool
    returncode: int | None
    was_killed: bool
    stderr_tail: tuple[str, ...]
    metrics: Mapping[str, float | str]


class ProcessStartError(RuntimeError):
    """A child could not be started without exposing its arguments."""


class AsyncProcess:
    """Own exactly one subprocess and always drain/reap it."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        sensitive_values: Sequence[str] = (),
        max_tail_entries: int = 50,
        max_line_chars: int = 512,
        max_tail_bytes: int = 16_384,
    ) -> None:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("argv must contain non-empty strings")
        if min(max_tail_entries, max_line_chars, max_tail_bytes) <= 0:
            raise ValueError("tail limits must be positive")
        self._argv = tuple(argv)
        self._sensitive_values = tuple(
            sorted((value for value in sensitive_values if value), key=len, reverse=True)
        )
        self._max_tail_entries = max_tail_entries
        self._max_line_chars = max_line_chars
        self._max_tail_bytes = max_tail_bytes
        self._tail: deque[str] = deque()
        self._tail_bytes = 0
        self._metrics: dict[str, float | str] = {}
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._finalize_lock = asyncio.Lock()
        self._was_killed = False
        self._stop_requested = False
        self._auth_failed = False
        self._process_group_id: int | None = None
        self._windows_job: _WindowsJob | None = None
        self._tree_cleanup_done = False
        self._tree_cleanup_lock = asyncio.Lock()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def returncode(self) -> int | None:
        return self._process.returncode if self._process is not None else None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def was_killed(self) -> bool:
        return self._was_killed

    @property
    def auth_failed(self) -> bool:
        return self._auth_failed

    @property
    def started(self) -> bool:
        return self._process is not None

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        return tuple(self._tail)

    @property
    def metrics(self) -> dict[str, float | str]:
        return dict(self._metrics)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._process is not None:
                raise RuntimeError("process instances may only be started once")
            if self._stop_requested:
                return
            try:
                if os.name == "nt":
                    self._process = await asyncio.create_subprocess_exec(
                        *self._argv,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
                else:
                    self._process = await asyncio.create_subprocess_exec(
                        *self._argv,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                    )
            except (OSError, ValueError):
                raise ProcessStartError("output process could not be started") from None
            self._process_group_id = self._process.pid
            if os.name == "nt":
                self._windows_job = _WindowsJob.attach(self._process.pid)
            self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self) -> None:
        process = self._require_process()
        assert process.stderr is not None
        buffered = bytearray()
        discarding = False
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                if buffered and not discarding:
                    self._capture_line(bytes(buffered))
                return
            for byte in chunk:
                if byte == 10:
                    if not discarding:
                        self._capture_line(bytes(buffered).rstrip(b"\r"))
                    buffered.clear()
                    discarding = False
                elif not discarding:
                    buffered.append(byte)
                    if len(buffered) > max(4096, self._max_line_chars * 4):
                        self._capture_line(bytes(buffered))
                        buffered.clear()
                        discarding = True

    def _capture_line(self, raw: bytes) -> None:
        decoded = raw.decode("utf-8", errors="replace")
        if _AUTH_FAILURE_PATTERN.search(decoded) is not None:
            self._auth_failed = True
        self._capture_metric(decoded)
        self._append_tail(self._sanitize(decoded))

    def _sanitize(self, value: str) -> str:
        sanitized = str(redact(value))
        for secret in self._sensitive_values:
            sanitized = sanitized.replace(secret, "***")
        return sanitized

    def _append_tail(self, line: str) -> None:
        bounded = line[: self._max_line_chars]
        encoded_size = len(bounded.encode("utf-8"))
        if encoded_size > self._max_tail_bytes:
            while len(bounded.encode("utf-8")) > self._max_tail_bytes:
                bounded = bounded[:-1]
            encoded_size = len(bounded.encode("utf-8"))
        self._tail.append(bounded)
        self._tail_bytes += encoded_size
        while len(self._tail) > self._max_tail_entries or self._tail_bytes > self._max_tail_bytes:
            removed = self._tail.popleft()
            self._tail_bytes -= len(removed.encode("utf-8"))

    def _capture_metric(self, line: str) -> None:
        if len(line) > 1024 or "=" not in line:
            return
        key, raw_value = line.split("=", 1)
        if key not in _PROGRESS_KEYS or len(raw_value) > 128:
            return
        parsed: float | str | None
        if key == "progress":
            parsed = raw_value if raw_value in {"continue", "end"} else None
            metric_key = key
        elif key == "out_time":
            parsed = _parse_time(raw_value)
            metric_key = "out_time_seconds"
        else:
            suffix = "kbits/s" if key == "bitrate" else "x" if key == "speed" else ""
            number = raw_value.removesuffix(suffix)
            maximum = 100_000_000.0 if key == "bitrate" else 1000.0
            parsed = _bounded_float(number, maximum=maximum)
            metric_key = "bitrate_kbps" if key == "bitrate" else key
        if parsed is not None:
            self._metrics[metric_key] = parsed

    async def wait(self) -> int:
        process = self._require_process()
        returncode = await process.wait()
        await self._cleanup_process_tree()
        await self._finalize_reader()
        return returncode

    async def _finalize_reader(self) -> None:
        async with self._finalize_lock:
            if self._stderr_task is not None:
                await self._stderr_task
                self._stderr_task = None

    async def stop(self, *, timeout: float = 5.0) -> int:
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        self._stop_requested = True
        async with self._stop_lock, self._lifecycle_lock:
            if self._process is None:
                return 0
            process = self._process
            try:
                if process.returncode is None:
                    self._gentle_stop()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=timeout)
                    except TimeoutError:
                        self._was_killed = True
                        await self.kill()
                        await process.wait()
                await self._cleanup_process_tree()
                await self._finalize_reader()
                assert process.returncode is not None
                return process.returncode
            except asyncio.CancelledError:
                await asyncio.shield(self.kill())
                if process.returncode is None:
                    process.kill()
                await asyncio.shield(process.wait())
                await asyncio.shield(self._cleanup_process_tree())
                await asyncio.shield(self._finalize_reader())
                raise

    def _gentle_stop(self) -> None:
        process = self._require_process()
        try:
            if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
                process.send_signal(signal.CTRL_BREAK_EVENT)
            elif os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)  # type: ignore[attr-defined]
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

    async def kill(self) -> None:
        process = self._require_process()
        try:
            if os.name != "nt":
                await self._cleanup_process_tree()
            else:
                tree_stopped = await self._cleanup_process_tree()
                if not tree_stopped and process.returncode is None:
                    process.kill()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await asyncio.shield(process.wait())
            raise
        except (OSError, ProcessLookupError):
            if process.returncode is None:
                process.kill()

    async def _cleanup_process_tree(self) -> bool:
        async with self._tree_cleanup_lock:
            if self._tree_cleanup_done:
                return True
            group_id = self._process_group_id
            if group_id is None:
                return False
            if os.name != "nt":
                try:
                    os.killpg(group_id, signal.SIGKILL)  # type: ignore[attr-defined]
                except ProcessLookupError:
                    pass
                self._tree_cleanup_done = True
                return True
            if self._windows_job is not None:
                self._windows_job.close()
                self._windows_job = None
                self._tree_cleanup_done = True
                await asyncio.sleep(0)
                return True
            taskkill_returncode = await self._run_taskkill(group_id)
            if taskkill_returncode != 0:
                return False
            self._tree_cleanup_done = True
            return True

    @staticmethod
    async def _run_taskkill(pid: int) -> int:
        taskkill = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(int(pid)),
            "/T",
            "/F",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        waiter = asyncio.create_task(taskkill.wait())
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=5.0)
        except TimeoutError:
            taskkill.kill()
            return await asyncio.shield(waiter)
        except asyncio.CancelledError:
            taskkill.kill()
            await asyncio.shield(waiter)
            raise
        finally:
            if not waiter.done():
                taskkill.kill()
                await asyncio.shield(waiter)

    def snapshot(self) -> ProcessSnapshot:
        return ProcessSnapshot(
            pid=self.pid,
            running=self.running,
            returncode=self.returncode,
            was_killed=self.was_killed,
            stderr_tail=self.stderr_tail,
            metrics=self.metrics,
        )

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise RuntimeError("process has not been started")
        return self._process

    def __repr__(self) -> str:
        return f"AsyncProcess({self.snapshot()!r})"


def _bounded_float(value: str, *, maximum: float) -> float | None:
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) and 0 <= parsed <= maximum else None


def _parse_time(value: str) -> float | None:
    matched = _TIME_PATTERN.fullmatch(value)
    if matched is None:
        return None
    hours, minutes, seconds = matched.groups()
    if int(minutes) >= 60 or float(seconds) >= 60:
        return None
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
