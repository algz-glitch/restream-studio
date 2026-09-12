"""Lifecycle supervisor for one isolated RTMP destination process."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from restream_studio.domain import DestinationKind, OutputState
from restream_studio.media.process import AsyncProcess, ProcessSnapshot

_BACKOFF: Final = (2.0, 5.0, 10.0, 20.0, 30.0)
_TRANSITIONS: Final[Mapping[OutputState, frozenset[OutputState]]] = {
    OutputState.DISABLED: frozenset({OutputState.STOPPED}),
    OutputState.STOPPED: frozenset({OutputState.CONNECTING}),
    OutputState.CONNECTING: frozenset(
        {OutputState.LIVE, OutputState.RECONNECTING, OutputState.AUTH_FAILED, OutputState.ERROR, OutputState.STOPPED}
    ),
    OutputState.LIVE: frozenset(
        {OutputState.RECONNECTING, OutputState.AUTH_FAILED, OutputState.ERROR, OutputState.STOPPED}
    ),
    OutputState.RECONNECTING: frozenset(
        {OutputState.CONNECTING, OutputState.AUTH_FAILED, OutputState.ERROR, OutputState.STOPPED}
    ),
    OutputState.AUTH_FAILED: frozenset({OutputState.STOPPED, OutputState.CONNECTING}),
    OutputState.ERROR: frozenset({OutputState.STOPPED, OutputState.CONNECTING}),
}


@dataclass(frozen=True, slots=True)
class SupervisorSnapshot:
    destination: DestinationKind
    state: OutputState
    reconnect_count: int
    process: ProcessSnapshot | None


class OutputSupervisor:
    """Strictly owns one destination and at most one live child process."""

    def __init__(
        self,
        destination: DestinationKind,
        argv: Sequence[str],
        *,
        sensitive_values: Sequence[str] = (),
        stop_timeout: float = 1.0,
        retry_wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.destination = destination
        self._argv = tuple(argv)
        self._sensitive_values = tuple(sensitive_values)
        self._stop_timeout = stop_timeout
        self._retry_wait = retry_wait
        self._state = OutputState.STOPPED
        self._reconnect_count = 0
        self._process: AsyncProcess | None = None
        self._stop_requested = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self._runner: asyncio.Task[None] | None = None
        self._live = asyncio.Event()

    @property
    def state(self) -> OutputState:
        return self._state

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def process(self) -> AsyncProcess | None:
        return self._process

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def transition(self, target: OutputState) -> None:
        if target not in _TRANSITIONS[self._state]:
            raise ValueError(f"Illegal output transition: {self._state.value} -> {target.value}")
        self._state = target

    async def start(self) -> None:
        if self._runner is not None and not self._runner.done():
            await self._live.wait()
            return
        self._live.clear()
        self._runner = asyncio.create_task(self.run())
        live_wait = asyncio.create_task(self._live.wait())
        done, _ = await asyncio.wait({self._runner, live_wait}, return_when=asyncio.FIRST_COMPLETED)
        if self._runner in done:
            live_wait.cancel()
            await asyncio.gather(live_wait, return_exceptions=True)
            self._runner.result()

    async def run(self) -> None:
        current = asyncio.current_task()
        if self._runner is not None and self._runner is not current and not self._runner.done():
            raise RuntimeError("supervisor is already running")
        self._runner = current
        self._stop_requested.clear()
        self._live.clear()
        try:
            while not self._stop_requested.is_set():
                self.transition(OutputState.CONNECTING)
                process = AsyncProcess(self._argv, sensitive_values=self._sensitive_values)
                self._process = process
                try:
                    await process.start()
                except Exception:
                    self.transition(OutputState.ERROR)
                    raise
                if not process.started or self._stop_requested.is_set():
                    break
                self.transition(OutputState.LIVE)
                self._live.set()
                await process.wait()
                if self._stop_requested.is_set():
                    break
                if process.auth_failed:
                    self.transition(OutputState.AUTH_FAILED)
                    return
                self._reconnect_count += 1
                self.transition(OutputState.RECONNECTING)
                delay = _BACKOFF[min(self._reconnect_count - 1, len(_BACKOFF) - 1)]
                if await self._wait_for_retry(delay):
                    break
            self._to_stopped()
        except asyncio.CancelledError:
            await self._close_process()
            self._to_stopped()
            raise
        finally:
            if self._stop_requested.is_set():
                await self._close_process()
                self._to_stopped()

    async def _wait_for_retry(self, delay: float) -> bool:
        async def wait_delay() -> None:
            await self._retry_wait(delay)

        async def wait_stop() -> None:
            await self._stop_requested.wait()

        retry = asyncio.create_task(wait_delay())
        stopped = asyncio.create_task(wait_stop())
        retry.set_name("output-retry-delay")
        stopped.set_name("output-retry-stop")
        try:
            done, _ = await asyncio.wait({retry, stopped}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            return self._stop_requested.is_set()
        finally:
            retry.cancel()
            stopped.cancel()
            await asyncio.gather(retry, stopped, return_exceptions=True)

    async def stop(self) -> None:
        async with self._stop_lock:
            self._stop_requested.set()
            await self._close_process()
            runner = self._runner
            if runner is not None and runner is not asyncio.current_task() and not runner.done():
                await runner
            self._to_stopped()

    async def _close_process(self) -> None:
        process = self._process
        if process is not None:
            await process.stop(timeout=self._stop_timeout)

    def _to_stopped(self) -> None:
        if self._state is not OutputState.STOPPED:
            self.transition(OutputState.STOPPED)

    def snapshot(self) -> SupervisorSnapshot:
        return SupervisorSnapshot(
            destination=self.destination,
            state=self.state,
            reconnect_count=self.reconnect_count,
            process=self._process.snapshot() if self._process is not None else None,
        )

    def __repr__(self) -> str:
        return f"OutputSupervisor({self.snapshot()!r})"
