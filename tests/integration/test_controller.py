from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Coroutine
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from restream_studio.config import AppPaths
from restream_studio.domain import (
    DestinationKind,
    MediaProbe,
    OutputState,
    ResolvedStream,
    SourceState,
)
from restream_studio.orchestration.controller import (
    ConfiguredDestination,
    ConfiguredSource,
    Controller,
    ControllerSnapshot,
    ControllerStateStore,
    OutputFailure,
    PersistedControllerState,
    StandbyMedia,
)
from restream_studio.persistence.database import Database
from restream_studio.source import (
    LiveSourceResolver,
    ResolverNetworkError,
    ResolverRateLimited,
    normalize_douyin_url,
)


def drive[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Drive fake-only coroutines without creating a Windows asyncio event loop."""
    try:
        while True:
            coroutine.send(None)
    except StopIteration as stopped:
        return cast(T, stopped.value)


class FakeClock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 1, 1, tzinfo=UTC)
        self.elapsed = 0.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.elapsed += seconds


class FakeSleeper:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.clock.advance(delay)


class FakeResolver:
    def __init__(self, results: list[ResolvedStream | Exception]) -> None:
        self.results = deque(results)
        self.calls: list[tuple[str, str | None]] = []

    async def resolve(self, room_identity: str, preferred_quality: str | None) -> ResolvedStream:
        self.calls.append((room_identity, preferred_quality))
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


class FakeProbe:
    def __init__(self, results: list[MediaProbe | Exception] | None = None) -> None:
        self.results = deque(results or [])
        self.urls: list[str] = []

    async def probe(self, url: str) -> MediaProbe:
        self.urls.append(url)
        if self.results:
            result = self.results.popleft()
            if isinstance(result, Exception):
                raise result
            return result
        return PROBE


@dataclass(frozen=True)
class FakeCommand:
    label: str


class FakeSupervisor:
    def __init__(self, identity: str, destination: DestinationKind) -> None:
        self.identity = identity
        self.destination = destination
        self.state = OutputState.STOPPED
        self.prepared: list[str] = []
        self.restarted: list[FakeCommand] = []
        self.stop_count = 0
        self.events: list[str] = []
        self.fail_next_restart = False
        self.restart_error: BaseException | None = None
        self.stop_error: BaseException | None = None

    def prepare_live(self, source_url: str, probe: MediaProbe) -> FakeCommand:
        assert probe is PROBE
        self.events.append("prepare-live")
        self.prepared.append(source_url)
        return FakeCommand("live")

    def prepare_standby(self, standby: StandbyMedia) -> FakeCommand:
        self.events.append("prepare-standby")
        self.prepared.append(standby.local_id)
        return FakeCommand("standby")

    async def restart(self, command: object) -> None:
        assert isinstance(command, FakeCommand)
        self.events.append(f"restart-{command.label}")
        if self.restart_error is not None:
            error, self.restart_error = self.restart_error, None
            raise error
        if self.fail_next_restart:
            self.fail_next_restart = False
            self.state = OutputState.ERROR
            raise RuntimeError("isolated restart failure")
        self.restarted.append(command)
        self.state = OutputState.LIVE

    async def stop(self) -> None:
        self.stop_count += 1
        if self.stop_error is not None:
            error, self.stop_error = self.stop_error, None
            raise error
        self.state = OutputState.STOPPED


class FakeStore:
    def __init__(self, loaded: PersistedControllerState | None = None) -> None:
        self.loaded = loaded
        self.load_calls: list[str] = []
        self.saved: list[PersistedControllerState] = []

    async def load(self, room_identity: str) -> PersistedControllerState | None:
        self.load_calls.append(room_identity)
        return self.loaded

    async def save(self, state: PersistedControllerState) -> None:
        self.saved.append(state)


PROBE = MediaProbe("h264", "aac", 1920, 1080, 30.0)
RAW_ROOM_URL = "https://LIVE.DOUYIN.COM/room-42?token=room-secret&from=copy"
CANONICAL_ROOM = normalize_douyin_url(RAW_ROOM_URL)


def stream(clock: FakeClock, token: str, *, live: bool = True, ttl: float = 300) -> ResolvedStream:
    return ResolvedStream(
        url=f"https://media.example/live.flv?sign={token}",
        acquired_at=clock.now(),
        expires_at=clock.now() + timedelta(seconds=ttl),
        room_id="room-42",
        is_live=live,
    )


def make_controller(
    resolver: LiveSourceResolver,
    *,
    probe: FakeProbe | None = None,
    clock: FakeClock | None = None,
    store: ControllerStateStore | None = None,
) -> tuple[Controller, FakeClock, FakeSleeper, FakeSupervisor, FakeSupervisor]:
    clock = clock or FakeClock()
    sleeper = FakeSleeper(clock)
    douyin = FakeSupervisor("primary", DestinationKind.DOUYIN)
    wechat = FakeSupervisor("secondary", DestinationKind.WECHAT)
    controller = Controller(
        source=ConfiguredSource(RAW_ROOM_URL, preferred_quality="origin"),
        resolver=resolver,
        media_probe=probe or FakeProbe(),
        destinations=(
            ConfiguredDestination("primary", douyin, enabled=True),
            ConfiguredDestination("secondary", wechat, enabled=True),
        ),
        clock=clock,
        sleeper=sleeper,
        standby=StandbyMedia("generated-safe-slate"),
        state_store=store,
    )
    return controller, clock, sleeper, douyin, wechat


def test_shutdown_stops_processes_without_persisting_desired_state() -> None:
    store = FakeStore(PersistedControllerState(CANONICAL_ROOM, True, ("primary",)))
    controller, _, _, douyin, wechat = make_controller(FakeResolver([]), store=store)

    drive(controller.initialize())
    store.saved.clear()
    drive(controller.shutdown())

    assert store.saved == []
    assert douyin.stop_count == 1
    assert wechat.stop_count == 1


def test_real_controller_store_round_trips_primary_secondary_identities(tmp_path: Path) -> None:
    paths = AppPaths.create(tmp_path / "controller-store")
    encrypt = lambda value: "cipher:" + value[::-1]
    decrypt = lambda value: value.removeprefix("cipher:")[::-1]
    with Database(
        paths.database_file,
        paths=paths,
        encrypt_secret=encrypt,
        decrypt_secret=decrypt,
    ) as database:
        database.set_destination(
            DestinationKind.DOUYIN,
            "rtmp://one.test/app",
            "one",
            controller_identity="primary",
        )
        database.set_destination(
            DestinationKind.WECHAT,
            "rtmp://two.test/app",
            "two",
            controller_identity="secondary",
        )
        first, _, _, _, _ = make_controller(FakeResolver([]), store=database)
        drive(first.initialize())
        drive(first.set_destination_enabled("secondary", False))

        second, _, _, _, _ = make_controller(FakeResolver([]), store=database)
        drive(second.initialize())
        restored = drive(second.snapshot())

    assert [(item.identity, item.enabled) for item in restored.outputs] == [
        ("primary", True),
        ("secondary", False),
    ]


def test_offline_source_stays_monitoring_without_starting_outputs() -> None:
    clock = FakeClock()
    controller, _, sleeper, first, second = make_controller(
        FakeResolver([stream(clock, "offline", live=False)]), clock=clock
    )

    drive(controller.poll_once())

    assert drive(controller.snapshot()).source_state is SourceState.MONITORING
    assert not first.restarted and not second.restarted
    assert sleeper.delays == [2.0]


def test_live_source_is_probed_before_two_enabled_outputs_start() -> None:
    clock = FakeClock()
    resolved = stream(clock, "fresh")
    probe = FakeProbe()
    controller, _, _, first, second = make_controller(
        FakeResolver([resolved]), probe=probe, clock=clock
    )

    drive(controller.poll_once())

    assert probe.urls == [resolved.url]
    assert [command.label for command in first.restarted] == ["live"]
    assert [command.label for command in second.restarted] == ["live"]
    assert drive(controller.snapshot()).source_state is SourceState.LIVE


def test_url_crossing_expiry_threshold_during_probe_is_discarded_and_reresolved() -> None:
    clock = FakeClock()

    class AdvancingProbe(FakeProbe):
        async def probe(self, url: str) -> MediaProbe:
            result = await super().probe(url)
            clock.advance(2)
            return result

    resolver = FakeResolver(
        [stream(clock, "expires-during-probe", ttl=31), stream(clock, "replacement")]
    )
    controller, _, _, first, second = make_controller(resolver, probe=AdvancingProbe(), clock=clock)

    drive(controller.poll_once())
    assert not first.restarted and not second.restarted
    assert drive(controller.snapshot()).source_failure is not None

    drive(controller.poll_once())

    assert len(resolver.calls) == 2
    assert first.restarted[-1].label == second.restarted[-1].label == "live"


def test_one_output_reconnecting_does_not_restart_or_stop_live_peer() -> None:
    clock = FakeClock()
    resolver = FakeResolver([stream(clock, "one"), stream(clock, "two")])
    controller, _, _, first, second = make_controller(resolver, clock=clock)
    drive(controller.poll_once())
    first.state = OutputState.RECONNECTING

    drive(controller.poll_once())

    snapshot = drive(controller.snapshot())
    assert snapshot.outputs[0].state is OutputState.RECONNECTING
    assert snapshot.outputs[1].state is OutputState.LIVE
    assert len(second.restarted) == 1
    assert second.stop_count == 0


def test_failed_output_restarts_only_on_new_source_and_auth_failure_stays_terminal() -> None:
    clock = FakeClock()
    controller, _, _, first, second = make_controller(
        FakeResolver(
            [
                stream(clock, "one"),
                stream(clock, "one"),
                stream(clock, "two"),
                stream(clock, "three"),
            ]
        ),
        clock=clock,
    )
    drive(controller.poll_once())
    first.state = OutputState.ERROR

    drive(controller.poll_once())
    assert len(first.restarted) == 1
    assert len(second.restarted) == 1

    drive(controller.poll_once())
    assert len(first.restarted) == 2
    assert len(second.restarted) == 1

    first.state = OutputState.AUTH_FAILED
    drive(controller.poll_once())
    assert len(first.restarted) == 2
    assert len(second.restarted) == 1


def test_source_loss_re_resolves_and_hits_standby_deadline_exactly() -> None:
    clock = FakeClock()
    failures = [ResolverNetworkError("gone") for _ in range(5)]
    resolver = FakeResolver([stream(clock, "initial"), *failures])
    controller, _, sleeper, _, _ = make_controller(resolver, clock=clock)
    drive(controller.poll_once())

    for _ in failures:
        drive(controller.poll_once())

    assert sleeper.delays == [3.0, 2.0, 5.0, 10.0, 20.0, 23.0]
    assert len(resolver.calls) == 6
    assert all(call == (RAW_ROOM_URL, "origin") for call in resolver.calls)
    assert clock.elapsed == 63.0
    assert drive(controller.snapshot()).source_state is SourceState.STANDBY


def test_sixty_seconds_source_failure_switches_each_output_to_safe_standby() -> None:
    clock = FakeClock()
    resolver = FakeResolver(
        [stream(clock, "initial"), ResolverNetworkError("lost"), ResolverNetworkError("lost")]
    )
    controller, _, _, first, second = make_controller(resolver, clock=clock)
    drive(controller.poll_once())
    drive(controller.poll_once())
    clock.advance(60)
    first.fail_next_restart = True

    drive(controller.poll_once())

    snapshot = drive(controller.snapshot())
    assert snapshot.source_state is SourceState.STANDBY
    assert first.events[-2:] == ["prepare-standby", "restart-standby"]
    assert second.events[-2:] == ["prepare-standby", "restart-standby"]
    assert second.restarted[-1].label == "standby"
    assert first.state is OutputState.ERROR


def test_failed_standby_destination_is_retried_without_restarting_healthy_peer() -> None:
    clock = FakeClock()
    resolver = FakeResolver(
        [
            stream(clock, "initial"),
            ResolverNetworkError("lost"),
            ResolverNetworkError("lost"),
            ResolverNetworkError("still-lost"),
        ]
    )
    controller, _, _, first, second = make_controller(resolver, clock=clock)
    drive(controller.poll_once())
    drive(controller.poll_once())
    clock.advance(60)
    first.fail_next_restart = True
    drive(controller.poll_once())
    healthy_restart_count = len(second.restarted)

    drive(controller.poll_once())

    assert first.restarted[-1].label == "standby"
    assert len(second.restarted) == healthy_restart_count


def test_arbitrary_restart_exception_is_isolated_and_recorded_without_secret() -> None:
    clock = FakeClock()
    controller, _, _, first, second = make_controller(
        FakeResolver([stream(clock, "fresh")]), clock=clock
    )
    secret = "rtmp://destination.example/app/stream-key"
    first.restart_error = Exception(secret)

    drive(controller.poll_once())

    snapshot = drive(controller.snapshot())
    assert second.restarted[-1].label == "live"
    assert snapshot.outputs[0].last_error is OutputFailure.RESTART
    assert secret not in repr(snapshot)


def test_standby_requires_two_spaced_successes_and_probe_failure_resets_count() -> None:
    clock = FakeClock()
    resolver = FakeResolver(
        [
            stream(clock, "initial"),
            ResolverNetworkError("lost"),
            ResolverNetworkError("lost"),
            stream(clock, "recovery-1"),
            stream(clock, "probe-fails"),
            stream(clock, "recovery-2"),
            stream(clock, "recovery-3"),
        ]
    )
    probe = FakeProbe([PROBE, PROBE, RuntimeError("bad media"), PROBE, PROBE])
    controller, _, _, first, second = make_controller(resolver, probe=probe, clock=clock)
    drive(controller.poll_once())
    drive(controller.poll_once())
    clock.advance(60)
    drive(controller.poll_once())
    assert drive(controller.snapshot()).source_state is SourceState.STANDBY

    drive(controller.poll_once())
    clock.advance(3)
    drive(controller.poll_once())
    clock.advance(3)
    drive(controller.poll_once())
    assert drive(controller.snapshot()).source_state is SourceState.STANDBY
    clock.advance(3)
    drive(controller.poll_once())

    assert drive(controller.snapshot()).source_state is SourceState.LIVE
    assert first.restarted[-1].label == second.restarted[-1].label == "live"


def test_expiring_url_is_never_probed_or_started() -> None:
    clock = FakeClock()
    probe = FakeProbe()
    controller, _, _, first, second = make_controller(
        FakeResolver([stream(clock, "nearly-expired", ttl=5)]), probe=probe, clock=clock
    )

    drive(controller.poll_once())

    assert probe.urls == []
    assert not first.restarted and not second.restarted
    assert drive(controller.snapshot()).source_state is SourceState.MONITORING


def test_rate_limit_uses_retry_after_instead_of_normal_backoff() -> None:
    controller, _, sleeper, _, _ = make_controller(FakeResolver([ResolverRateLimited(17)]))

    drive(controller.poll_once())

    assert sleeper.delays == [17.0]


def test_snapshot_is_immutable_and_safe_to_repr() -> None:
    clock = FakeClock()
    secret = "signed-source-token"
    controller, _, _, _, _ = make_controller(FakeResolver([stream(clock, secret)]), clock=clock)
    drive(controller.poll_once())

    snapshot = drive(controller.snapshot())

    assert isinstance(snapshot, ControllerSnapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.source_state = SourceState.ERROR  # type: ignore[misc]
    rendered = repr(snapshot)
    assert secret not in rendered
    assert "?" not in snapshot.room_identity
    assert "rtmp://" not in rendered


def test_restart_loads_only_desired_state_then_resolves_room_identity() -> None:
    persisted = PersistedControllerState(
        room_identity=CANONICAL_ROOM,
        desired_running=True,
        enabled_destinations=("secondary",),
    )
    store = FakeStore(persisted)
    clock = FakeClock()
    resolver = FakeResolver([stream(clock, "new-token")])
    controller, _, _, first, second = make_controller(resolver, clock=clock, store=store)

    drive(controller.initialize())
    before = drive(controller.snapshot())
    drive(controller.poll_once())

    assert before.source_state is SourceState.MONITORING
    assert store.load_calls == [CANONICAL_ROOM]
    assert resolver.calls == [(RAW_ROOM_URL, "origin")]
    assert not first.restarted and second.restarted[-1].label == "live"
    assert all(not hasattr(saved, "url") for saved in store.saved)


def test_room_identity_is_canonical_and_ephemeral_resolver_url_never_leaks() -> None:
    source = ConfiguredSource(RAW_ROOM_URL, preferred_quality="origin")
    controller, _, _, _, _ = make_controller(FakeResolver([]))
    snapshot = drive(controller.snapshot())
    persisted = PersistedControllerState(CANONICAL_ROOM, True, ("primary",))
    rendered = " ".join(
        (
            repr(source),
            repr(controller),
            repr(snapshot),
            repr(persisted),
            controller._safe_task_name(),
        )
    )

    assert source.room_identity == CANONICAL_ROOM
    assert source.resolver_url == RAW_ROOM_URL
    assert snapshot.room_identity == CANONICAL_ROOM
    assert "room-secret" not in rendered
    assert "?" not in rendered
    assert controller._safe_task_name().startswith("room-")


def test_stop_invalidates_poll_waiting_for_resolver_before_it_can_publish_live() -> None:
    class PauseOnce:
        def __await__(self) -> Any:
            yield "resolver-paused"

    class PausingResolver:
        async def resolve(
            self, room_identity: str, preferred_quality: str | None
        ) -> ResolvedStream:
            await PauseOnce()
            return stream(clock, "stale")

    clock = FakeClock()
    probe = FakeProbe()
    controller, _, _, first, second = make_controller(
        PausingResolver(),
        probe=probe,
        clock=clock,
    )
    polling = controller.poll_once()
    assert polling.send(None) == "resolver-paused"

    drive(controller.stop())
    drive(polling)

    snapshot = drive(controller.snapshot())
    assert snapshot.desired_running is False
    assert snapshot.source_state is SourceState.STOPPED
    assert probe.urls == []
    assert not first.restarted and not second.restarted


def test_stop_invalidates_poll_waiting_for_persisted_state_load() -> None:
    class PauseOnce:
        def __await__(self) -> Any:
            yield "store-paused"

    class PausingStore(FakeStore):
        async def load(self, room_identity: str) -> PersistedControllerState | None:
            self.load_calls.append(room_identity)
            await PauseOnce()
            return self.loaded

    store = PausingStore(PersistedControllerState(CANONICAL_ROOM, True, ("primary", "secondary")))
    resolver = FakeResolver([])
    controller, _, _, first, second = make_controller(resolver, store=store)
    polling = controller.poll_once()
    assert polling.send(None) == "store-paused"

    drive(controller.stop())
    drive(polling)

    snapshot = drive(controller.snapshot())
    assert snapshot.desired_running is False
    assert snapshot.source_state is SourceState.STOPPED
    assert resolver.calls == []
    assert first.stop_count == second.stop_count == 1


def test_disable_during_inflight_restart_cannot_publish_or_leave_output_live() -> None:
    class PauseOnce:
        def __await__(self) -> Any:
            yield "restart-paused"

    class PausingSupervisor(FakeSupervisor):
        async def restart(self, command: object) -> None:
            assert isinstance(command, FakeCommand)
            self.events.append(f"restart-{command.label}")
            await PauseOnce()
            self.restarted.append(command)
            self.state = OutputState.LIVE

    clock = FakeClock()
    first = PausingSupervisor("primary", DestinationKind.DOUYIN)
    second = FakeSupervisor("secondary", DestinationKind.WECHAT)
    controller = Controller(
        source=ConfiguredSource(RAW_ROOM_URL),
        resolver=FakeResolver([stream(clock, "fresh")]),
        media_probe=FakeProbe(),
        destinations=(
            ConfiguredDestination("primary", first),
            ConfiguredDestination("secondary", second),
        ),
        clock=clock,
        sleeper=FakeSleeper(clock),
        standby=StandbyMedia("safe-slate"),
    )
    polling = controller.poll_once()
    assert polling.send(None) == "restart-paused"

    drive(controller.set_destination_enabled("primary", False))
    drive(polling)

    snapshot = drive(controller.snapshot())
    assert snapshot.outputs[0].enabled is False
    assert snapshot.outputs[0].input.value == "NONE"
    assert first.state is OutputState.STOPPED


def test_cleanup_isolates_ordinary_stop_exception_and_records_safe_error() -> None:
    controller, _, _, first, second = make_controller(FakeResolver([]))
    secret = "rtmp://destination.example/app/stop-secret"
    first.stop_error = Exception(secret)

    drive(controller.stop())

    snapshot = drive(controller.snapshot())
    assert second.stop_count == 1
    assert snapshot.outputs[0].last_error is OutputFailure.STOP
    assert secret not in repr(snapshot)


def test_cleanup_finishes_later_destinations_then_propagates_cancelled_error() -> None:
    controller, _, _, first, second = make_controller(FakeResolver([]))
    first.stop_error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        drive(controller.stop())

    assert second.stop_count == 1


@pytest.mark.parametrize("failure", [ValueError("probe exploded"), Exception("probe broke")])
def test_unexpected_probe_exception_becomes_safe_error_and_monitoring_continues(
    failure: Exception,
) -> None:
    clock = FakeClock()
    secret_url = "https://media.example/live.flv?token=probe-secret"
    controller, _, sleeper, first, second = make_controller(
        FakeResolver([stream(clock, "source")]),
        probe=FakeProbe([type(failure)(f"failed {secret_url}")]),
        clock=clock,
    )

    drive(controller.poll_once())

    snapshot = drive(controller.snapshot())
    assert snapshot.source_state is SourceState.ERROR
    assert snapshot.source_failure is not None
    assert snapshot.error_detail is not None
    assert "https://" not in snapshot.error_detail and "probe-secret" not in repr(snapshot)
    assert sleeper.delays == [2.0]
    assert not first.restarted and not second.restarted


def test_unexpected_resolver_and_run_exceptions_are_safe_and_do_not_kill_monitoring() -> None:
    secret_url = "https://live.douyin.com/room-42?token=resolver-secret"
    controller, _, sleeper, first, second = make_controller(
        FakeResolver([Exception(f"resolver failed {secret_url}")])
    )

    drive(controller.poll_once())
    first_snapshot = drive(controller.snapshot())
    assert first_snapshot.source_state is SourceState.ERROR
    assert first_snapshot.error_detail is not None
    assert "https://" not in first_snapshot.error_detail
    assert "resolver-secret" not in repr(first_snapshot)

    attempts = 0

    async def fail_once_then_finish() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LookupError(f"run failed {secret_url}")
        controller._desired_running = False

    controller.poll_once = fail_once_then_finish  # type: ignore[method-assign]
    drive(controller._run())

    assert attempts == 2
    assert sleeper.delays == [2.0, 5.0]
    assert first.stop_count == second.stop_count == 1


def test_third_party_exception_message_is_never_exposed_in_snapshot() -> None:
    class ThirdPartyFailure(Exception):
        pass

    message = "TOPSECRET stream-key=hunter2 token=plain-secret arbitrary customer text"
    controller, _, _, _, _ = make_controller(FakeResolver([ThirdPartyFailure(message)]))

    drive(controller.poll_once())

    snapshot = drive(controller.snapshot())
    assert snapshot.source_state is SourceState.ERROR
    assert snapshot.error_detail == "unexpected_error"
    assert "TOPSECRET" not in repr(snapshot)
    assert "hunter2" not in repr(snapshot)
    assert "plain-secret" not in repr(snapshot)


def test_run_keeps_monitoring_when_error_backoff_itself_raises_once() -> None:
    class FlakySleeper(FakeSleeper):
        async def sleep(self, delay: float) -> None:
            self.delays.append(delay)
            if len(self.delays) == 1:
                raise OSError("sleep failed https://example.invalid/?token=sleep-secret")
            self.clock.advance(delay)

    controller, clock, _, first, second = make_controller(FakeResolver([]))
    sleeper = FlakySleeper(clock)
    controller._sleeper = sleeper
    drive(controller.initialize())
    attempts = 0

    async def fail_once_then_finish() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LookupError("poll failed")
        controller._desired_running = False

    controller.poll_once = fail_once_then_finish  # type: ignore[method-assign]

    drive(controller._run())

    assert attempts == 2
    assert sleeper.delays == [2.0]
    assert first.stop_count == second.stop_count == 1


def test_stop_awaits_done_task_and_retrieves_its_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DoneTask:
        retrieved = False

        def done(self) -> bool:
            return True

        def cancel(self) -> bool:
            raise AssertionError("done task must not be cancelled")

        def __await__(self) -> Any:
            self.retrieved = True
            raise RuntimeError("done task failed with https://example.invalid/?token=secret")
            yield

    task = DoneTask()
    controller, _, _, _, _ = make_controller(FakeResolver([]))
    controller._task = task  # type: ignore[assignment]
    monkeypatch.setattr(asyncio, "current_task", lambda: None)

    drive(controller.stop())

    snapshot = drive(controller.snapshot())
    assert task.retrieved is True
    assert snapshot.desired_running is False
    assert snapshot.source_state is SourceState.STOPPED


def test_stale_paused_save_cannot_overwrite_newer_desired_state() -> None:
    class PauseOnce:
        def __await__(self) -> Any:
            yield "old-save-paused"

    class RacingStore(FakeStore):
        calls = 0

        async def save(self, state: PersistedControllerState) -> None:
            self.calls += 1
            if self.calls == 1:
                await PauseOnce()
            self.saved.append(state)

    class NoopAsyncLock:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *args: object) -> None:
            return None

    store = RacingStore()
    controller, _, _, _, _ = make_controller(FakeResolver([]), store=store)
    controller._persistence_lock = NoopAsyncLock()  # type: ignore[assignment]
    old_save = controller.set_destination_enabled("primary", False)
    assert old_save.send(None) == "old-save-paused"

    drive(controller.set_destination_enabled("primary", True))
    drive(old_save)

    assert store.saved[-1].enabled_destinations == ("primary", "secondary")


def test_standby_rejects_network_urls_and_path_traversal() -> None:
    with pytest.raises(ValueError):
        StandbyMedia("https://evil.example/slate.mp4")
    with pytest.raises(ValueError):
        StandbyMedia("../outside.mp4")

    supervisor = FakeSupervisor("rtmp://live.example/app/secret", DestinationKind.DOUYIN)
    with pytest.raises(ValueError):
        ConfiguredDestination(supervisor.identity, supervisor)


def test_stop_does_not_swallow_caller_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    class OwnedTask:
        def done(self) -> bool:
            return False

        def cancel(self) -> bool:
            return True

        def __await__(self) -> Any:
            raise asyncio.CancelledError
            yield

    class CancellingCaller:
        def cancelling(self) -> int:
            return 1

    controller, _, _, _, _ = make_controller(FakeResolver([]))
    controller._task = OwnedTask()  # type: ignore[assignment]
    monkeypatch.setattr(asyncio, "current_task", lambda: CancellingCaller())

    with pytest.raises(asyncio.CancelledError):
        drive(controller.stop())
