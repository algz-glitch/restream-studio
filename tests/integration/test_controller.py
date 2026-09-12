from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Coroutine
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

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
    PersistedControllerState,
    StandbyMedia,
)
from restream_studio.source import ResolverNetworkError, ResolverRateLimited


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
        if self.fail_next_restart:
            self.fail_next_restart = False
            self.state = OutputState.ERROR
            raise RuntimeError("isolated restart failure")
        self.restarted.append(command)
        self.state = OutputState.LIVE

    async def stop(self) -> None:
        self.stop_count += 1
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


def stream(clock: FakeClock, token: str, *, live: bool = True, ttl: float = 300) -> ResolvedStream:
    return ResolvedStream(
        url=f"https://media.example/live.flv?sign={token}",
        acquired_at=clock.now(),
        expires_at=clock.now() + timedelta(seconds=ttl),
        room_id="room-42",
        is_live=live,
    )


def make_controller(
    resolver: FakeResolver,
    *,
    probe: FakeProbe | None = None,
    clock: FakeClock | None = None,
    store: FakeStore | None = None,
) -> tuple[Controller, FakeClock, FakeSleeper, FakeSupervisor, FakeSupervisor]:
    clock = clock or FakeClock()
    sleeper = FakeSleeper(clock)
    douyin = FakeSupervisor("primary", DestinationKind.DOUYIN)
    wechat = FakeSupervisor("secondary", DestinationKind.WECHAT)
    controller = Controller(
        source=ConfiguredSource("room-42", preferred_quality="origin"),
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


def test_source_loss_re_resolves_with_explicit_backoff() -> None:
    clock = FakeClock()
    failures = [ResolverNetworkError("gone") for _ in range(5)]
    resolver = FakeResolver([stream(clock, "initial"), *failures])
    controller, _, sleeper, _, _ = make_controller(resolver, clock=clock)
    drive(controller.poll_once())

    for _ in failures:
        drive(controller.poll_once())

    assert sleeper.delays == [3.0, 2.0, 5.0, 10.0, 20.0, 30.0]
    assert len(resolver.calls) == 6
    assert all(call == ("room-42", "origin") for call in resolver.calls)


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
    assert "rtmp://" not in rendered and "https://" not in rendered


def test_restart_loads_only_desired_state_then_resolves_room_identity() -> None:
    persisted = PersistedControllerState(
        room_identity="room-42", desired_running=True, enabled_destinations=("secondary",)
    )
    store = FakeStore(persisted)
    clock = FakeClock()
    resolver = FakeResolver([stream(clock, "new-token")])
    controller, _, _, first, second = make_controller(resolver, clock=clock, store=store)

    drive(controller.initialize())
    before = drive(controller.snapshot())
    drive(controller.poll_once())

    assert before.source_state is SourceState.MONITORING
    assert store.load_calls == ["room-42"]
    assert resolver.calls == [("room-42", "origin")]
    assert not first.restarted and second.restarted[-1].label == "live"
    assert all(not hasattr(saved, "url") for saved in store.saved)


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
