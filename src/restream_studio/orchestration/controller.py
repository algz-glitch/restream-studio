"""Deterministic orchestration for one configured live source."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol

from restream_studio.domain import (
    DestinationKind,
    MediaProbe,
    OutputState,
    ResolvedStream,
    SourceState,
)
from restream_studio.source import (
    LiveSourceResolver,
    ResolverNetworkError,
    ResolverProtocolError,
    ResolverRateLimited,
)

_SOURCE_BACKOFF: Final = (2.0, 5.0, 10.0, 20.0, 30.0)
_SAFE_IDENTIFIER: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class Sleeper(Protocol):
    async def sleep(self, delay: float) -> None: ...


class MediaProbePort(Protocol):
    async def probe(self, url: str) -> MediaProbe: ...


class PreparedOutputCommand(Protocol):
    """Opaque command prepared and owned by a destination adapter."""


class DestinationSupervisor(Protocol):
    identity: str
    destination: DestinationKind
    state: OutputState

    def prepare_live(self, source_url: str, probe: MediaProbe) -> PreparedOutputCommand: ...

    def prepare_standby(self, standby: StandbyMedia) -> PreparedOutputCommand: ...

    async def restart(self, command: object) -> None: ...

    async def stop(self) -> None: ...


class ControllerStateStore(Protocol):
    async def load(self, room_identity: str) -> PersistedControllerState | None: ...

    async def save(self, state: PersistedControllerState) -> None: ...


@dataclass(frozen=True, slots=True)
class ConfiguredSource:
    room_identity: str
    preferred_quality: str | None = None

    def __post_init__(self) -> None:
        if not self.room_identity or any(ord(character) < 32 for character in self.room_identity):
            raise ValueError("room identity is malformed")


@dataclass(frozen=True, slots=True)
class StandbyMedia:
    """A lookup identifier for a trusted local/generated standby input."""

    local_id: str

    def __post_init__(self) -> None:
        if _SAFE_IDENTIFIER.fullmatch(self.local_id) is None:
            raise ValueError("standby media must be a safe local identifier")


@dataclass(frozen=True, slots=True)
class ConfiguredDestination:
    identity: str
    supervisor: DestinationSupervisor = field(repr=False, compare=False)
    enabled: bool = True

    def __post_init__(self) -> None:
        if _SAFE_IDENTIFIER.fullmatch(self.identity) is None:
            raise ValueError("destination identity is malformed")
        if self.supervisor.identity != self.identity:
            raise ValueError("destination identity does not match supervisor")


@dataclass(frozen=True, slots=True)
class PersistedControllerState:
    """Deliberately excludes resolved URLs and destination credentials."""

    room_identity: str
    desired_running: bool
    enabled_destinations: tuple[str, ...]


class SourceFailure(StrEnum):
    OFFLINE = "OFFLINE"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK = "NETWORK"
    PROTOCOL = "PROTOCOL"
    PROBE = "PROBE"
    EXPIRED = "EXPIRED"


class OutputInput(StrEnum):
    NONE = "NONE"
    LIVE = "LIVE"
    STANDBY = "STANDBY"


@dataclass(frozen=True, slots=True)
class ControllerOutputSnapshot:
    identity: str
    destination: DestinationKind
    enabled: bool
    state: OutputState
    input: OutputInput


@dataclass(frozen=True, slots=True)
class ControllerSnapshot:
    room_identity: str
    desired_running: bool
    source_state: SourceState
    source_failure: SourceFailure | None
    recovery_successes: int
    outputs: tuple[ControllerOutputSnapshot, ...]


class _SystemClock:
    def now(self) -> datetime:
        from datetime import UTC

        return datetime.now(UTC)

    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()


class _AsyncioSleeper:
    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)


class Controller:
    """Own exactly one recovery task for one configured source."""

    def __init__(
        self,
        *,
        source: ConfiguredSource,
        resolver: LiveSourceResolver,
        media_probe: MediaProbePort,
        destinations: Sequence[ConfiguredDestination],
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        standby: StandbyMedia | None = None,
        state_store: ControllerStateStore | None = None,
        source_failure_standby_after: float = 60.0,
        standby_recovery_interval: float = 3.0,
        minimum_url_validity: float = 30.0,
        monitor_interval: float = 3.0,
    ) -> None:
        if len(destinations) < 2:
            raise ValueError("controller requires at least two destination supervisors")
        identities = tuple(destination.identity for destination in destinations)
        if len(set(identities)) != len(identities):
            raise ValueError("destination identities must be unique")
        if (
            min(
                source_failure_standby_after,
                standby_recovery_interval,
                minimum_url_validity,
                monitor_interval,
            )
            <= 0
        ):
            raise ValueError("controller timing values must be positive")
        self._source = source
        self._resolver = resolver
        self._media_probe = media_probe
        self._destinations = tuple(destinations)
        self._clock = clock or _SystemClock()
        self._sleeper = sleeper or _AsyncioSleeper()
        self._standby = standby
        self._state_store = state_store
        self._standby_after = source_failure_standby_after
        self._recovery_interval = standby_recovery_interval
        self._minimum_url_validity = minimum_url_validity
        self._monitor_interval = monitor_interval
        self._state = SourceState.STOPPED
        self._source_failure: SourceFailure | None = None
        self._desired_running = False
        self._enabled = {item.identity: item.enabled for item in self._destinations}
        self._inputs = {item.identity: OutputInput.NONE for item in self._destinations}
        self._first_failure_at: float | None = None
        self._failure_count = 0
        self._recovery_successes = 0
        self._last_recovery_success_at: float | None = None
        self._initialized = False
        self._initialize_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._cycle_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    async def initialize(self) -> None:
        async with self._initialize_lock:
            async with self._state_lock:
                if self._initialized:
                    return
            restored = (
                await self._state_store.load(self._source.room_identity)
                if self._state_store is not None
                else None
            )
            async with self._state_lock:
                if restored is not None and restored.room_identity == self._source.room_identity:
                    enabled = set(restored.enabled_destinations)
                    self._enabled = {identity: identity in enabled for identity in self._enabled}
                    self._desired_running = restored.desired_running
                else:
                    self._desired_running = True
                self._state = SourceState.MONITORING
                self._source_failure = None
                self._clear_resolved_recovery_state()
                self._initialized = True

    async def start(self) -> None:
        await self.initialize()
        async with self._lifecycle_lock:
            if self._task is not None and not self._task.done():
                return
            async with self._state_lock:
                self._desired_running = True
                if self._state is SourceState.STOPPED:
                    self._state = SourceState.MONITORING
            await self._persist()
            self._task = asyncio.create_task(
                self._run(), name=f"source-controller-{self._safe_task_name()}"
            )

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            task = self._task
            async with self._state_lock:
                self._desired_running = False
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    caller = asyncio.current_task()
                    if caller is not None and caller.cancelling():
                        raise
            await self._stop_outputs()
            async with self._state_lock:
                self._state = SourceState.STOPPED
                self._source_failure = None
                self._clear_resolved_recovery_state()
                for identity in self._inputs:
                    self._inputs[identity] = OutputInput.NONE
            await self._persist()
            self._task = None

    async def cancel(self) -> None:
        await self.stop()

    async def poll_once(self) -> None:
        await self.initialize()
        async with self._cycle_lock:
            try:
                resolved = await self._resolver.resolve(
                    self._source.room_identity, self._source.preferred_quality
                )
            except asyncio.CancelledError:
                raise
            except ResolverRateLimited as error:
                delay = (
                    float(error.retry_after_seconds)
                    if error.retry_after_seconds is not None and error.retry_after_seconds > 0
                    else self._next_backoff()
                )
                await self._record_failure(SourceFailure.RATE_LIMITED)
                await self._sleeper.sleep(delay)
                return
            except ResolverNetworkError:
                await self._fail_and_wait(SourceFailure.NETWORK)
                return
            except ResolverProtocolError:
                await self._fail_and_wait(SourceFailure.PROTOCOL)
                return

            if not resolved.is_live:
                await self._fail_and_wait(SourceFailure.OFFLINE)
                return
            if not self._url_is_fresh(resolved):
                await self._fail_and_wait(SourceFailure.EXPIRED)
                return
            try:
                probe = await self._media_probe.probe(resolved.url)
            except asyncio.CancelledError:
                raise
            except RuntimeError:
                await self._fail_and_wait(SourceFailure.PROBE)
                return
            await self._record_success(resolved, probe)

    async def snapshot(self) -> ControllerSnapshot:
        async with self._state_lock:
            return ControllerSnapshot(
                room_identity=self._source.room_identity,
                desired_running=self._desired_running,
                source_state=self._state,
                source_failure=self._source_failure,
                recovery_successes=self._recovery_successes,
                outputs=tuple(
                    ControllerOutputSnapshot(
                        identity=item.identity,
                        destination=item.supervisor.destination,
                        enabled=self._enabled[item.identity],
                        state=(
                            item.supervisor.state
                            if self._enabled[item.identity]
                            else OutputState.DISABLED
                        ),
                        input=self._inputs[item.identity],
                    )
                    for item in self._destinations
                ),
            )

    async def set_destination_enabled(self, identity: str, enabled: bool) -> None:
        selected = self._destination(identity)
        async with self._state_lock:
            self._enabled[identity] = enabled
            if not enabled:
                self._inputs[identity] = OutputInput.NONE
        if not enabled:
            await selected.supervisor.stop()
        await self._persist()

    async def _run(self) -> None:
        try:
            while True:
                async with self._state_lock:
                    if not self._desired_running:
                        return
                await self.poll_once()
        finally:
            await self._stop_outputs()

    async def _fail_and_wait(self, failure: SourceFailure) -> None:
        await self._record_failure(failure)
        await self._sleeper.sleep(self._next_backoff())

    async def _record_failure(self, failure: SourceFailure) -> None:
        should_enter_standby = False
        should_retry_standby = False
        async with self._state_lock:
            now = self._clock.monotonic()
            if self._first_failure_at is None:
                self._first_failure_at = now
            self._failure_count += 1
            self._source_failure = failure
            self._recovery_successes = 0
            self._last_recovery_success_at = None
            if self._state is SourceState.STANDBY:
                should_retry_standby = any(
                    self._enabled[identity] and value is not OutputInput.STANDBY
                    for identity, value in self._inputs.items()
                )
            elif any(value is OutputInput.LIVE for value in self._inputs.values()):
                self._state = SourceState.RECONNECTING
                should_enter_standby = (
                    self._standby is not None
                    and now - self._first_failure_at >= self._standby_after
                )
            else:
                self._state = SourceState.MONITORING
        if should_enter_standby or should_retry_standby:
            await self._switch_outputs(OutputInput.STANDBY, resolved=None, probe=None)
            async with self._state_lock:
                self._state = SourceState.STANDBY

    async def _record_success(self, resolved: ResolvedStream, probe: MediaProbe) -> None:
        async with self._state_lock:
            in_standby = self._state is SourceState.STANDBY
            now = self._clock.monotonic()
            if in_standby:
                if self._last_recovery_success_at is None:
                    self._recovery_successes = 1
                    self._last_recovery_success_at = now
                elif now - self._last_recovery_success_at >= self._recovery_interval:
                    self._recovery_successes += 1
                    self._last_recovery_success_at = now
                recovered = self._recovery_successes >= 2
            else:
                recovered = True
            all_live = all(
                not self._enabled[identity] or value is OutputInput.LIVE
                for identity, value in self._inputs.items()
            )
            self._source_failure = None
            self._first_failure_at = None
            self._failure_count = 0
        if recovered and (in_standby or not all_live):
            await self._switch_outputs(OutputInput.LIVE, resolved=resolved, probe=probe)
        async with self._state_lock:
            if recovered:
                self._state = SourceState.LIVE
                self._recovery_successes = 0
                self._last_recovery_success_at = None
            else:
                self._state = SourceState.STANDBY
        await self._sleeper.sleep(self._monitor_interval)

    async def _switch_outputs(
        self,
        target: OutputInput,
        *,
        resolved: ResolvedStream | None,
        probe: MediaProbe | None,
    ) -> None:
        prepared: list[tuple[ConfiguredDestination, object]] = []
        for item in self._destinations:
            if not self._enabled[item.identity] or self._inputs[item.identity] is target:
                continue
            try:
                if target is OutputInput.LIVE:
                    if resolved is None or probe is None:
                        raise RuntimeError("live input metadata is unavailable")
                    command = item.supervisor.prepare_live(resolved.url, probe)
                else:
                    if self._standby is None:
                        raise RuntimeError("standby media is unavailable")
                    command = item.supervisor.prepare_standby(self._standby)
            except asyncio.CancelledError:
                raise
            except (RuntimeError, ValueError):
                continue
            prepared.append((item, command))
        for item, command in prepared:
            try:
                await item.supervisor.restart(command)
            except asyncio.CancelledError:
                raise
            except RuntimeError:
                continue
            async with self._state_lock:
                self._inputs[item.identity] = target

    async def _stop_outputs(self) -> None:
        for item in self._destinations:
            try:
                await item.supervisor.stop()
            except asyncio.CancelledError:
                raise
            except RuntimeError:
                continue

    async def _persist(self) -> None:
        if self._state_store is None:
            return
        async with self._state_lock:
            state = PersistedControllerState(
                room_identity=self._source.room_identity,
                desired_running=self._desired_running,
                enabled_destinations=tuple(
                    identity for identity, enabled in self._enabled.items() if enabled
                ),
            )
        await self._state_store.save(state)

    def _url_is_fresh(self, resolved: ResolvedStream) -> bool:
        if resolved.expires_at is None:
            return True
        return (
            resolved.expires_at - self._clock.now()
        ).total_seconds() >= self._minimum_url_validity

    def _next_backoff(self) -> float:
        index = max(self._failure_count - 1, 0)
        return _SOURCE_BACKOFF[min(index, len(_SOURCE_BACKOFF) - 1)]

    def _clear_resolved_recovery_state(self) -> None:
        self._first_failure_at = None
        self._failure_count = 0
        self._recovery_successes = 0
        self._last_recovery_success_at = None

    def _destination(self, identity: str) -> ConfiguredDestination:
        for item in self._destinations:
            if item.identity == identity:
                return item
        raise KeyError(identity)

    def _safe_task_name(self) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", self._source.room_identity)[:64]

    def __repr__(self) -> str:
        return (
            f"Controller(room_identity={self._source.room_identity!r}, "
            f"state={self._state.value!r}, destinations={len(self._destinations)})"
        )
