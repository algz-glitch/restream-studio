"""Deterministic orchestration for one configured live source."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol
from urllib.parse import urlsplit

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
    normalize_douyin_url,
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
    resolver_url: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        resolver_url = self.room_identity.strip()
        canonical = normalize_douyin_url(resolver_url)
        object.__setattr__(self, "room_identity", canonical)
        object.__setattr__(self, "resolver_url", resolver_url)


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "room_identity", normalize_douyin_url(self.room_identity))


class SourceFailure(StrEnum):
    OFFLINE = "OFFLINE"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK = "NETWORK"
    PROTOCOL = "PROTOCOL"
    PROBE = "PROBE"
    EXPIRED = "EXPIRED"
    UNEXPECTED = "UNEXPECTED"


class OutputInput(StrEnum):
    NONE = "NONE"
    LIVE = "LIVE"
    STANDBY = "STANDBY"


class OutputFailure(StrEnum):
    PREPARE = "PREPARE_FAILED"
    RESTART = "RESTART_FAILED"
    STOP = "STOP_FAILED"


@dataclass(frozen=True, slots=True)
class ControllerOutputSnapshot:
    identity: str
    destination: DestinationKind
    enabled: bool
    state: OutputState
    input: OutputInput
    last_error: OutputFailure | None


@dataclass(frozen=True, slots=True)
class ControllerSnapshot:
    room_identity: str
    desired_running: bool
    source_state: SourceState
    source_failure: SourceFailure | None
    error_detail: str | None
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
        if not destinations:
            raise ValueError("controller requires at least one destination supervisor")
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
        self._source_error_detail: str | None = None
        self._desired_running = False
        self._enabled = {item.identity: item.enabled for item in self._destinations}
        self._inputs = {item.identity: OutputInput.NONE for item in self._destinations}
        self._output_errors: dict[str, OutputFailure | None] = {
            item.identity: None for item in self._destinations
        }
        self._output_generations = {item.identity: 0 for item in self._destinations}
        self._output_source_fingerprints: dict[str, str | None] = {
            item.identity: None for item in self._destinations
        }
        self._first_failure_at: float | None = None
        self._failure_count = 0
        self._recovery_successes = 0
        self._last_recovery_success_at: float | None = None
        self._initialized = False
        self._initialize_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._cycle_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._persistence_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_generation = 0
        self._persistence_version = 0
        self._persisted_version = -1

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    async def initialize(self) -> None:
        async with self._initialize_lock:
            async with self._state_lock:
                if self._initialized:
                    return
                generation = self._lifecycle_generation
            restored = (
                await self._state_store.load(self._source.room_identity)
                if self._state_store is not None
                else None
            )
            async with self._state_lock:
                if generation != self._lifecycle_generation:
                    self._initialized = True
                    return
                if restored is not None and restored.room_identity == self._source.room_identity:
                    enabled = set(restored.enabled_destinations)
                    self._enabled = {identity: identity in enabled for identity in self._enabled}
                    self._desired_running = restored.desired_running
                else:
                    self._desired_running = True
                self._state = SourceState.MONITORING
                self._source_failure = None
                self._source_error_detail = None
                self._clear_resolved_recovery_state()
                self._initialized = True

    async def start(self) -> None:
        await self.initialize()
        async with self._lifecycle_lock:
            if self._task is not None and not self._task.done():
                return
            async with self._state_lock:
                if not self._desired_running:
                    self._persistence_version += 1
                self._desired_running = True
                self._lifecycle_generation += 1
                if self._state is SourceState.STOPPED:
                    self._state = SourceState.MONITORING
            await self._persist()
            self._task = asyncio.create_task(
                self._run(), name=f"source-controller-{self._safe_task_name()}"
            )
            self._task.add_done_callback(self._retrieve_task_exception)

    async def stop(self) -> None:
        await self._stop(persist=True)

    async def shutdown(self) -> None:
        """Stop child processes without changing the persisted operator intent."""
        await self._stop(persist=False)

    async def _stop(self, *, persist: bool) -> None:
        async with self._lifecycle_lock:
            task = self._task
            task_error_detail: str | None = None
            async with self._state_lock:
                if self._desired_running:
                    self._persistence_version += 1
                self._desired_running = False
                self._lifecycle_generation += 1
            if task is not None and task is not asyncio.current_task():
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    caller = asyncio.current_task()
                    if caller is not None and caller.cancelling():
                        raise
                except Exception as error:  # noqa: BLE001 - retrieve finished task failures
                    task_error_detail = self._safe_error(error)
            await self._stop_outputs()
            async with self._state_lock:
                self._state = SourceState.STOPPED
                self._source_failure = (
                    SourceFailure.UNEXPECTED if task_error_detail is not None else None
                )
                self._source_error_detail = task_error_detail
                self._clear_resolved_recovery_state()
                for identity in self._inputs:
                    self._inputs[identity] = OutputInput.NONE
            if persist:
                await self._persist()
            self._task = None

    async def cancel(self) -> None:
        await self.stop()

    async def poll_once(self) -> None:
        await self.initialize()
        async with self._cycle_lock:
            generation = await self._active_generation()
            if generation is None:
                return
            try:
                resolved = await self._resolver.resolve(
                    self._source.resolver_url, self._source.preferred_quality
                )
            except asyncio.CancelledError:
                raise
            except ResolverRateLimited as error:
                if not await self._poll_is_current(generation):
                    return
                delay = (
                    float(error.retry_after_seconds)
                    if error.retry_after_seconds is not None and error.retry_after_seconds > 0
                    else self._next_backoff()
                )
                if await self._record_failure(SourceFailure.RATE_LIMITED, generation):
                    await self._sleep_after_failure(delay, generation)
                return
            except ResolverNetworkError:
                await self._fail_and_wait(SourceFailure.NETWORK, generation)
                return
            except ResolverProtocolError:
                await self._fail_and_wait(SourceFailure.PROTOCOL, generation)
                return
            except Exception as error:  # noqa: BLE001 - unexpected adapters remain monitored
                await self._unexpected_and_wait(error, generation)
                return

            if not await self._poll_is_current(generation):
                return
            if not resolved.is_live:
                await self._fail_and_wait(SourceFailure.OFFLINE, generation)
                return
            if not self._url_is_fresh(resolved):
                await self._fail_and_wait(SourceFailure.EXPIRED, generation)
                return
            try:
                probe = await self._media_probe.probe(resolved.url)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - probe adapters may use custom exceptions
                await self._unexpected_and_wait(error, generation, SourceFailure.PROBE)
                return
            if not await self._poll_is_current(generation):
                return
            if not self._url_is_fresh(resolved):
                await self._fail_and_wait(SourceFailure.EXPIRED, generation)
                return
            await self._record_success(resolved, probe, generation)

    async def snapshot(self) -> ControllerSnapshot:
        async with self._state_lock:
            return ControllerSnapshot(
                room_identity=self._source.room_identity,
                desired_running=self._desired_running,
                source_state=self._state,
                source_failure=self._source_failure,
                error_detail=self._source_error_detail,
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
                        last_error=self._output_errors[item.identity],
                    )
                    for item in self._destinations
                ),
            )

    async def set_destination_enabled(self, identity: str, enabled: bool) -> None:
        selected = self._destination(identity)
        async with self._state_lock:
            if self._enabled[identity] is enabled:
                return
            self._enabled[identity] = enabled
            self._persistence_version += 1
            self._output_generations[identity] += 1
            if not enabled:
                self._inputs[identity] = OutputInput.NONE
                self._output_source_fingerprints[identity] = None
        if not enabled:
            await self._stop_destination(selected)
        await self._persist()

    async def _run(self) -> None:
        try:
            while True:
                async with self._state_lock:
                    if not self._desired_running:
                        return
                    generation = self._lifecycle_generation
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - controller must remain monitoring
                    try:
                        if await self._record_unexpected(error, generation):
                            await self._sleep_after_failure(self._next_backoff(), generation)
                    except asyncio.CancelledError:
                        raise
                    except Exception as recovery_error:  # noqa: BLE001
                        await self._record_safe_error_only(recovery_error, generation)
        finally:
            await self._stop_outputs()

    async def _fail_and_wait(self, failure: SourceFailure, generation: int) -> None:
        if await self._record_failure(failure, generation):
            await self._sleep_after_failure(self._next_backoff(), generation)

    async def _unexpected_and_wait(
        self,
        error: Exception,
        generation: int,
        failure: SourceFailure = SourceFailure.UNEXPECTED,
    ) -> None:
        if await self._record_unexpected(error, generation, failure):
            await self._sleep_after_failure(self._next_backoff(), generation)

    async def _record_unexpected(
        self,
        error: Exception,
        generation: int,
        failure: SourceFailure = SourceFailure.UNEXPECTED,
    ) -> bool:
        if not await self._record_failure(failure, generation):
            return False
        async with self._state_lock:
            if not self._is_current_unlocked(generation):
                return False
            if self._state is not SourceState.STANDBY:
                self._state = SourceState.ERROR
            self._source_error_detail = self._safe_error(error)
            return True

    async def _record_safe_error_only(self, error: Exception, generation: int) -> None:
        async with self._state_lock:
            if self._is_current_unlocked(generation):
                self._state = SourceState.ERROR
                self._source_failure = SourceFailure.UNEXPECTED
                self._source_error_detail = self._safe_error(error)

    async def _sleep_after_failure(self, requested_delay: float, generation: int) -> None:
        async with self._state_lock:
            if not self._is_current_unlocked(generation):
                return
            delay = requested_delay
            if (
                self._state in {SourceState.RECONNECTING, SourceState.ERROR}
                and self._standby is not None
                and self._first_failure_at is not None
                and any(value is OutputInput.LIVE for value in self._inputs.values())
            ):
                remaining = max(
                    0.0,
                    self._first_failure_at + self._standby_after - self._clock.monotonic(),
                )
                delay = min(delay, remaining)
        await self._sleeper.sleep(delay)
        if await self._poll_is_current(generation):
            await self._enter_standby_if_due(generation)

    async def _enter_standby_if_due(self, generation: int) -> None:
        async with self._state_lock:
            due = (
                self._is_current_unlocked(generation)
                and self._state in {SourceState.RECONNECTING, SourceState.ERROR}
                and self._standby is not None
                and self._first_failure_at is not None
                and any(value is OutputInput.LIVE for value in self._inputs.values())
                and self._clock.monotonic() - self._first_failure_at >= self._standby_after
            )
        if not due:
            return
        await self._switch_outputs(OutputInput.STANDBY, resolved=None, probe=None)
        async with self._state_lock:
            if self._is_current_unlocked(generation) and self._state in {
                SourceState.RECONNECTING,
                SourceState.ERROR,
            }:
                self._state = SourceState.STANDBY

    async def _record_failure(self, failure: SourceFailure, generation: int) -> bool:
        should_enter_standby = False
        should_retry_standby = False
        async with self._state_lock:
            if not self._is_current_unlocked(generation):
                return False
            now = self._clock.monotonic()
            if self._first_failure_at is None:
                self._first_failure_at = now
            self._failure_count += 1
            self._source_failure = failure
            self._source_error_detail = None
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
                if self._is_current_unlocked(generation):
                    self._state = SourceState.STANDBY
        return await self._poll_is_current(generation)

    async def _record_success(
        self, resolved: ResolvedStream, probe: MediaProbe, generation: int
    ) -> None:
        async with self._state_lock:
            if not self._is_current_unlocked(generation):
                return
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
            self._source_failure = None
            self._source_error_detail = None
            self._first_failure_at = None
            self._failure_count = 0
        if recovered:
            await self._switch_outputs(OutputInput.LIVE, resolved=resolved, probe=probe)
        async with self._state_lock:
            if not self._is_current_unlocked(generation):
                return
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
        source_fingerprint = (
            self._source_fingerprint(resolved.url) if resolved is not None else None
        )
        async with self._state_lock:
            planned = tuple(
                (item, self._output_generations[item.identity])
                for item in self._destinations
                if self._should_switch_unlocked(item, target, source_fingerprint)
            )
        prepared: list[tuple[ConfiguredDestination, int, object]] = []
        for item, generation in planned:
            try:
                if target is OutputInput.LIVE:
                    if resolved is None or probe is None:
                        raise RuntimeError("live input metadata is unavailable")
                    command = item.supervisor.prepare_live(resolved.url, probe)
                else:
                    if self._standby is None:
                        raise RuntimeError("standby media is unavailable")
                    command = item.supervisor.prepare_standby(self._standby)
            except Exception:  # noqa: BLE001 - destination isolation is the contract
                await self._record_output_error(item.identity, OutputFailure.PREPARE)
                continue
            prepared.append((item, generation, command))
        for item, generation, command in prepared:
            async with self._state_lock:
                current = (
                    self._enabled[item.identity]
                    and self._output_generations[item.identity] == generation
                    and self._should_switch_unlocked(item, target, source_fingerprint)
                )
            if not current:
                continue
            try:
                await item.supervisor.restart(command)
            except Exception:  # noqa: BLE001 - destination isolation is the contract
                await self._record_output_error(item.identity, OutputFailure.RESTART)
                continue
            async with self._state_lock:
                current = (
                    self._enabled[item.identity]
                    and self._output_generations[item.identity] == generation
                )
                if current:
                    self._inputs[item.identity] = target
                    self._output_errors[item.identity] = None
                    self._output_source_fingerprints[item.identity] = (
                        source_fingerprint if target is OutputInput.LIVE else None
                    )
            if not current:
                await self._stop_destination(item)

    async def _stop_outputs(self) -> None:
        async with self._state_lock:
            for item in self._destinations:
                self._output_generations[item.identity] += 1
                self._inputs[item.identity] = OutputInput.NONE
                self._output_source_fingerprints[item.identity] = None
        cancellation: asyncio.CancelledError | None = None
        for item in self._destinations:
            try:
                await item.supervisor.stop()
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
            except Exception:  # noqa: BLE001 - cleanup must continue for peer destinations
                await self._record_output_error(item.identity, OutputFailure.STOP)
            finally:
                async with self._state_lock:
                    self._inputs[item.identity] = OutputInput.NONE
        if cancellation is not None:
            raise cancellation

    async def _stop_destination(self, item: ConfiguredDestination) -> None:
        try:
            await item.supervisor.stop()
        except Exception:  # noqa: BLE001 - one destination cannot block controller cleanup
            await self._record_output_error(item.identity, OutputFailure.STOP)

    async def _record_output_error(self, identity: str, error: OutputFailure) -> None:
        async with self._state_lock:
            self._output_errors[identity] = error

    def _should_switch_unlocked(
        self,
        item: ConfiguredDestination,
        target: OutputInput,
        source_fingerprint: str | None,
    ) -> bool:
        identity = item.identity
        if not self._enabled[identity] or item.supervisor.state is OutputState.AUTH_FAILED:
            return False
        if target is OutputInput.STANDBY:
            return self._inputs[identity] is not OutputInput.STANDBY
        if self._inputs[identity] is not OutputInput.LIVE:
            return True
        return (
            item.supervisor.state in {OutputState.ERROR, OutputState.STOPPED}
            and self._output_source_fingerprints[identity] != source_fingerprint
        )

    async def _persist(self) -> None:
        if self._state_store is None:
            return
        repair_stale_write = False
        async with self._persistence_lock:
            while True:
                async with self._state_lock:
                    version = self._persistence_version
                    if not repair_stale_write and version <= self._persisted_version:
                        return
                    state = PersistedControllerState(
                        room_identity=self._source.room_identity,
                        desired_running=self._desired_running,
                        enabled_destinations=tuple(
                            identity for identity, enabled in self._enabled.items() if enabled
                        ),
                    )
                await self._state_store.save(state)
                async with self._state_lock:
                    if version == self._persistence_version:
                        self._persisted_version = max(self._persisted_version, version)
                        return
                    repair_stale_write = True

    async def _active_generation(self) -> int | None:
        async with self._state_lock:
            return self._lifecycle_generation if self._desired_running else None

    async def _poll_is_current(self, generation: int) -> bool:
        async with self._state_lock:
            return self._is_current_unlocked(generation)

    def _is_current_unlocked(self, generation: int) -> bool:
        return self._desired_running and self._lifecycle_generation == generation

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
        segment = urlsplit(self._source.room_identity).path.strip("/").split("/")[-1]
        safe_segment = re.sub(r"[^A-Za-z0-9_.-]", "_", segment)[:24] or "source"
        digest = hashlib.sha256(self._source.room_identity.encode()).hexdigest()[:12]
        return f"room-{safe_segment}-{digest}"

    @staticmethod
    def _source_fingerprint(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    @staticmethod
    def _safe_error(error: Exception) -> str:
        if isinstance(error, TimeoutError):
            return "timeout"
        if isinstance(error, ConnectionError):
            return "network_error"
        if isinstance(error, PermissionError):
            return "permission_error"
        if isinstance(error, OSError):
            return "io_error"
        if isinstance(error, ValueError):
            return "invalid_value"
        if isinstance(error, LookupError):
            return "lookup_error"
        if isinstance(error, RuntimeError):
            return "runtime_error"
        return "unexpected_error"

    @staticmethod
    def _retrieve_task_exception(task: asyncio.Task[None]) -> None:
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    def __repr__(self) -> str:
        return (
            f"Controller(room_identity={self._source.room_identity!r}, "
            f"state={self._state.value!r}, destinations={len(self._destinations)})"
        )
