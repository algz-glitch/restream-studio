from __future__ import annotations

import asyncio
import ssl
from collections.abc import Coroutine
from typing import Any, cast

import pytest

from restream_studio.destination_test import (
    DestinationTester,
    ProcessPort,
    ReaderPort,
    build_destination_test_command,
)
from restream_studio.domain import DestinationKind

SECRET = "test-secret-never-display"


class FakeWriter:
    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class FakeProcess:
    def __init__(self, returncode: int | None = 0, stderr: bytes = b"") -> None:
        self.returncode: int | None = returncode
        self.stderr: ReaderPort | None = FakeReader(stderr)
        self.killed = False
        self.waited = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return cast(int, self.returncode)


class FakeReader:
    def __init__(self, value: bytes) -> None:
        self.value = value

    async def read(self, limit: int = -1) -> bytes:
        return self.value[:limit]


async def immediate(awaitable: Any, timeout: float) -> object:
    del timeout
    return await awaitable


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    iterator = coroutine.__await__()
    try:
        iterator.send(None)
    except StopIteration as stopped:
        return cast(T, stopped.value)
    raise AssertionError("fake coroutine unexpectedly suspended")


def test_probe_uses_validated_ip_sni_and_full_secret_only_in_subprocess_argv() -> None:
    connections: list[tuple[str, int, object, str | None]] = []
    commands: list[tuple[str, ...]] = []
    process = FakeProcess()
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    async def dns(kind: DestinationKind, server: str) -> tuple[str, ...]:
        assert kind is DestinationKind.DOUYIN
        assert server == "rtmps://publish.example/live"
        return ("203.0.113.10",)

    async def connect(
        host: str, port: int, *, ssl: object = None, server_hostname: str | None = None
    ) -> tuple[object, FakeWriter]:
        connections.append((host, port, ssl, server_hostname))
        return object(), FakeWriter()

    async def spawn(argv: tuple[str, ...]) -> ProcessPort:
        commands.append(argv)
        return process

    tester = DestinationTester(
        dns_validator=dns,
        connector=connect,
        process_factory=spawn,
        ssl_context_factory=lambda: tls_context,
        waiter=immediate,
    )
    assert run(
        tester.test(
            DestinationKind.DOUYIN,
            "rtmps://publish.example/live",
            SECRET,
        )
    )

    assert connections[0][0:2] == ("203.0.113.10", 443)
    assert isinstance(connections[0][2], ssl.SSLContext)
    assert connections[0][3] == "publish.example"
    assert SECRET in commands[0][-1]
    command = build_destination_test_command("rtmps://publish.example/live", SECRET)
    assert SECRET not in repr(command)
    assert SECRET not in " ".join(command.display_argv)


def test_probe_maps_auth_failure_to_safe_false_without_exposing_stderr() -> None:
    process = FakeProcess(1, b"Authentication failed for " + SECRET.encode())

    async def dns(kind: DestinationKind, server: str) -> tuple[str, ...]:
        del kind, server
        return ("203.0.113.10",)

    async def connect(
        host: str, port: int, *, ssl: object = None, server_hostname: str | None = None
    ) -> tuple[object, FakeWriter]:
        del host, port, ssl, server_hostname
        return object(), FakeWriter()

    async def spawn(argv: tuple[str, ...]) -> ProcessPort:
        assert SECRET in argv[-1]
        return process

    tester = DestinationTester(
        dns_validator=dns, connector=connect, process_factory=spawn, waiter=immediate
    )
    assert not run(
        tester.test(DestinationKind.DOUYIN, "rtmp://publish.example/live", SECRET)
    )
    assert process.waited


def test_probe_timeout_kills_and_reaps_child() -> None:
    process = FakeProcess(None)
    waits = 0

    async def dns(kind: DestinationKind, server: str) -> tuple[str, ...]:
        del kind, server
        return ("203.0.113.10",)

    async def connect(
        host: str, port: int, *, ssl: object = None, server_hostname: str | None = None
    ) -> tuple[object, FakeWriter]:
        del host, port, ssl, server_hostname
        return object(), FakeWriter()

    async def spawn(argv: tuple[str, ...]) -> ProcessPort:
        del argv
        return process

    async def timeout_publish(awaitable: Any, timeout: float) -> object:
        nonlocal waits
        del timeout
        waits += 1
        if waits == 1:
            return await awaitable
        awaitable.close()
        raise TimeoutError

    tester = DestinationTester(
        dns_validator=dns,
        connector=connect,
        process_factory=spawn,
        waiter=timeout_publish,
    )
    assert not run(
        tester.test(DestinationKind.DOUYIN, "rtmp://publish.example/live", SECRET)
    )
    assert process.killed
    assert process.waited


def test_request_cancellation_kills_and_reaps_child_before_propagating() -> None:
    process = FakeProcess(None)
    waits = 0

    async def dns(kind: DestinationKind, server: str) -> tuple[str, ...]:
        del kind, server
        return ("203.0.113.10",)

    async def connect(
        host: str, port: int, *, ssl: object = None, server_hostname: str | None = None
    ) -> tuple[object, FakeWriter]:
        del host, port, ssl, server_hostname
        return object(), FakeWriter()

    async def spawn(argv: tuple[str, ...]) -> ProcessPort:
        del argv
        return process

    async def cancel_publish(awaitable: Any, timeout: float) -> object:
        nonlocal waits
        del timeout
        waits += 1
        if waits == 1:
            return await awaitable
        awaitable.close()
        raise asyncio.CancelledError

    tester = DestinationTester(
        dns_validator=dns,
        connector=connect,
        process_factory=spawn,
        waiter=cancel_publish,
    )
    with pytest.raises(asyncio.CancelledError):
        run(tester.test(DestinationKind.DOUYIN, "rtmp://publish.example/live", SECRET))
    assert process.killed
    assert process.waited


def test_preflight_tries_sorted_addresses_until_one_succeeds_with_original_sni() -> None:
    attempted: list[tuple[str, str | None]] = []
    process = FakeProcess()
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    async def dns(kind: DestinationKind, server: str) -> tuple[str, ...]:
        del kind, server
        return ("203.0.113.20", "203.0.113.10")

    async def connect(
        host: str, port: int, *, ssl: object = None, server_hostname: str | None = None
    ) -> tuple[object, FakeWriter]:
        del port, ssl
        attempted.append((host, server_hostname))
        if host == "203.0.113.10":
            raise OSError("first address unavailable")
        return object(), FakeWriter()

    async def spawn(argv: tuple[str, ...]) -> ProcessPort:
        del argv
        return process

    tester = DestinationTester(
        dns_validator=dns,
        connector=connect,
        process_factory=spawn,
        ssl_context_factory=lambda: tls_context,
        waiter=immediate,
    )
    assert run(
        tester.test(DestinationKind.DOUYIN, "rtmps://publish.example/live", SECRET)
    )
    assert attempted == [
        ("203.0.113.10", "publish.example"),
        ("203.0.113.20", "publish.example"),
    ]
