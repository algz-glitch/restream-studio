"""Live runtime assembly and atomic controller replacement."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from dataclasses import replace
from ipaddress import ip_address
from urllib.parse import urlsplit

from restream_studio.destination_test import DestinationTester
from restream_studio.domain import DestinationKind, MediaProbe, OutputState, SourceState
from restream_studio.media.ffmpeg_commands import FfmpegCommand, build_ffmpeg_command
from restream_studio.media.ffprobe import probe_media
from restream_studio.orchestration.controller import (
    ConfiguredDestination,
    ConfiguredSource,
    Controller,
    ControllerSnapshot,
    MediaProbePort,
    SourceFailure,
    StandbyMedia,
)
from restream_studio.outputs.supervisor import OutputSupervisor
from restream_studio.persistence.database import Database, RuntimeDestination
from restream_studio.source import DouyinResolver, LiveSourceResolver


class RuntimeBuildError(RuntimeError):
    pass


AddressResolver = Callable[[str, int], Awaitable[tuple[str, ...]]]


async def resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        answers = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except OSError:
        raise RuntimeBuildError("destination host resolution failed") from None
    addresses = tuple({str(answer[4][0]) for answer in answers})
    if not addresses:
        raise RuntimeBuildError("destination host resolution returned no addresses")
    return addresses


async def validate_destination_dns(
    kind: DestinationKind,
    server: str,
    *,
    resolver: AddressResolver = resolve_addresses,
) -> tuple[str, ...]:
    parsed = urlsplit(server)
    host = parsed.hostname
    if host is None:
        raise RuntimeBuildError("destination host is invalid")
    lowered = host.casefold().rstrip(".")
    if kind is DestinationKind.LOCAL_TEST and lowered == "localhost":
        return ("127.0.0.1", "::1")
    try:
        literal = ip_address(lowered)
    except ValueError:
        addresses = await resolver(host, parsed.port or (443 if parsed.scheme == "rtmps" else 1935))
    else:
        addresses = (str(literal),)
    try:
        parsed_addresses = tuple(ip_address(item) for item in addresses)
    except ValueError:
        raise RuntimeBuildError("destination DNS response is invalid") from None
    if kind is DestinationKind.LOCAL_TEST:
        if not all(address.is_loopback for address in parsed_addresses):
            raise RuntimeBuildError("local test destination must resolve to loopback")
    elif not all(address.is_global for address in parsed_addresses):
        raise RuntimeBuildError("destination must resolve only to public addresses")
    ordered = sorted(parsed_addresses, key=lambda address: (address.version, int(address)))
    return tuple(str(address) for address in ordered)


class _Probe:
    async def probe(self, url: str) -> MediaProbe:
        return await probe_media(url)


class _DestinationAdapter:
    def __init__(
        self,
        config: RuntimeDestination,
        *,
        dns_validator: Callable[[DestinationKind, str], Awaitable[tuple[str, ...]]],
    ) -> None:
        self.identity = config.controller_identity
        self.destination = config.kind
        self._server = config.base_server
        self._key = config.stream_key
        self._dns_validator = dns_validator
        self._supervisor: OutputSupervisor | None = None
        self._last_command: FfmpegCommand | None = None
        self._state = OutputState.STOPPED

    @property
    def state(self) -> OutputState:
        return self._supervisor.state if self._supervisor is not None else self._state

    @state.setter
    def state(self, value: OutputState) -> None:
        self._state = value

    def prepare_live(self, source_url: str, probe: MediaProbe) -> FfmpegCommand:
        target = f"{self._server.rstrip('/')}/{self._key}"
        return build_ffmpeg_command(source_url, target, self.destination, probe)

    def prepare_standby(self, standby: StandbyMedia) -> FfmpegCommand:
        del standby
        raise RuntimeBuildError("standby output is not configured")

    async def restart(self, command: object) -> None:
        if not isinstance(command, FfmpegCommand):
            raise RuntimeBuildError("output command is invalid")
        await self._dns_validator(self.destination, self._server)
        await self.stop()
        self._last_command = command
        self._supervisor = OutputSupervisor(
            self.destination, command.argv, sensitive_values=(self._key,)
        )
        await self._supervisor.start()

    async def reconnect(self) -> None:
        if self._last_command is None:
            raise RuntimeBuildError("destination has not started")
        await self.restart(self._last_command)

    async def stop(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.stop()


class RuntimeManager:
    """Build controllers only from complete persisted configuration."""

    def __init__(
        self,
        database: Database,
        *,
        resolver_factory: Callable[[], LiveSourceResolver] = DouyinResolver,
        probe_factory: Callable[[], MediaProbePort] = _Probe,
        destination_tester: DestinationTester | None = None,
    ) -> None:
        self._database = database
        self._resolver_factory = resolver_factory
        self._probe_factory = probe_factory
        self._destination_tester = destination_tester or DestinationTester(
            dns_validator=validate_destination_dns
        )
        self._controller: Controller | None = None
        self._adapters: dict[DestinationKind, _DestinationAdapter] = {}
        self._lock = asyncio.Lock()
        self._startup_failed = False
        self._configuration_blocked = False

    async def initialize(self) -> None:
        await self._apply_configuration(suppress_resume_failure=True)

    async def apply_configuration(self) -> None:
        await self._apply_configuration(suppress_resume_failure=False)

    async def _apply_configuration(self, *, suppress_resume_failure: bool) -> None:
        async with self._lock:
            source = self._database.get_source()
            destinations = [
                runtime
                for item in self._database.list_destinations()
                if (runtime := self._database.get_destination_runtime(item.kind)) is not None
            ]
            new_controller: Controller | None = None
            new_adapters: dict[DestinationKind, _DestinationAdapter] = {}
            has_enabled_destination = any(item.enabled for item in destinations)
            if source is not None and destinations:
                for item in destinations:
                    new_adapters[item.kind] = _DestinationAdapter(
                        item, dns_validator=validate_destination_dns
                    )
                configured = tuple(
                    ConfiguredDestination(item.controller_identity, new_adapters[item.kind], item.enabled)
                    for item in destinations
                )
                new_controller = Controller(
                    source=ConfiguredSource(source.room_identity, source.preferred_quality),
                    resolver=self._resolver_factory(),
                    media_probe=self._probe_factory(),
                    destinations=configured,
                    state_store=self._database,
                )
                await new_controller.initialize()
            restored_running = (
                (await new_controller.snapshot()).desired_running
                if new_controller is not None
                else False
            )
            old = self._controller
            was_running = False
            if old is not None:
                was_running = (await old.snapshot()).desired_running
                try:
                    await old.shutdown()
                except BaseException:
                    if new_controller is not None:
                        await new_controller.shutdown()
                    raise
            self._controller = new_controller
            self._adapters = new_adapters
            self._startup_failed = False
            self._configuration_blocked = False
            requested_running = was_running or restored_running
            if requested_running and not has_enabled_destination:
                if new_controller is not None:
                    await new_controller.stop()
                if source is not None:
                    self._database.set_source(
                        source.room_identity, source.preferred_quality, False
                    )
                self._configuration_blocked = True
            elif requested_running and new_controller is not None:
                try:
                    await new_controller.start()
                except Exception:
                    await new_controller.shutdown()
                    self._startup_failed = True
                    if not suppress_resume_failure:
                        raise

    async def start(self) -> None:
        async with self._lock:
            controller = self._controller
            if controller is None:
                raise RuntimeBuildError("runtime is not configured")
            if not any(item.enabled for item in self._database.list_destinations()):
                source = self._database.get_source()
                if source is not None:
                    self._database.set_source(
                        source.room_identity, source.preferred_quality, False
                    )
                self._configuration_blocked = True
                raise RuntimeBuildError("runtime has no enabled destination")
            await controller.start()
            self._startup_failed = False
            self._configuration_blocked = False

    async def stop(self) -> None:
        async with self._lock:
            controller = self._controller
            if controller is not None:
                await controller.stop()
            self._startup_failed = False
            self._configuration_blocked = False

    async def shutdown(self) -> None:
        async with self._lock:
            controller = self._controller
            if controller is not None:
                await controller.shutdown()

    async def snapshot(self) -> ControllerSnapshot:
        controller = self._controller
        if controller is not None:
            snapshot = await controller.snapshot()
            if self._startup_failed or self._configuration_blocked:
                return replace(
                    snapshot,
                    desired_running=False,
                    source_state=SourceState.ERROR,
                    source_failure=SourceFailure.UNEXPECTED,
                    error_detail=None,
                )
            return snapshot
        source = self._database.get_source()
        return ControllerSnapshot(
            source.room_identity if source else "https://live.douyin.com/unconfigured",
            False,
            SourceState.ERROR if self._configuration_blocked else SourceState.STOPPED,
            SourceFailure.UNEXPECTED if self._configuration_blocked else None,
            None,
            0,
            (),
        )

    async def set_destination_enabled(self, identity: str, enabled: bool) -> None:
        controller = self._controller
        if controller is not None:
            await controller.set_destination_enabled(identity, enabled)

    async def reconnect_destination(self, kind: DestinationKind) -> None:
        async with self._lock:
            adapter = self._adapters.get(kind)
            if adapter is None:
                raise RuntimeBuildError("destination runtime is not configured")
            await adapter.reconnect()

    async def test_destination(self, kind: DestinationKind, server: str, key: str) -> bool:
        return await self._destination_tester.test(kind, server, key)

    def clear(self) -> None:
        self._controller = None
        self._adapters = {}
        self._startup_failed = False
        self._configuration_blocked = False
