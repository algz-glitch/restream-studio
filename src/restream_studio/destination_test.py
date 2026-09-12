"""Bounded, explicitly requested destination publication diagnostics."""

from __future__ import annotations

import asyncio
import re
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from restream_studio.domain import DestinationKind

_AUTH_FAILURE = re.compile(
    rb"(?:auth(?:entication|orization)?|access).{0,40}(?:fail|denied)", re.IGNORECASE
)
_MAX_STDERR = 64 * 1024


class ReaderPort(Protocol):
    async def read(self, limit: int = -1) -> bytes: ...


class ProcessPort(Protocol):
    @property
    def returncode(self) -> int | None: ...

    @property
    def stderr(self) -> ReaderPort | None: ...

    def kill(self) -> None: ...
    async def wait(self) -> int: ...


ProcessFactory = Callable[[tuple[str, ...]], Awaitable[ProcessPort]]
class WriterPort(Protocol):
    def close(self) -> None: ...
    async def wait_closed(self) -> None: ...


Connector = Callable[..., Awaitable[tuple[object, WriterPort]]]
DnsValidator = Callable[[DestinationKind, str], Awaitable[tuple[str, ...]]]
Waiter = Callable[[Awaitable[object], float], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class DestinationTestCommand:
    argv: tuple[str, ...] = field(repr=False)
    display_argv: tuple[str, ...]


def build_destination_test_command(server: str, stream_key: str) -> DestinationTestCommand:
    target = f"{server.rstrip('/')}/{stream_key}"
    display_target = f"{server.rstrip('/')}/********"
    prefix = (
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x180:r=10",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=stereo",
        "-t",
        "1",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-c:a",
        "aac",
        "-f",
        "flv",
    )
    return DestinationTestCommand(prefix + (target,), prefix + (display_target,))


async def _spawn(argv: tuple[str, ...]) -> ProcessPort:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )


class DestinationTester:
    def __init__(
        self,
        *,
        dns_validator: DnsValidator,
        connector: Connector = asyncio.open_connection,
        process_factory: ProcessFactory = _spawn,
        ssl_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
        waiter: Waiter = asyncio.wait_for,
        timeout_seconds: float = 4.0,
    ) -> None:
        self._dns_validator = dns_validator
        self._connector = connector
        self._process_factory = process_factory
        self._ssl_context_factory = ssl_context_factory
        self._waiter = waiter
        self._timeout = timeout_seconds

    async def test(self, kind: DestinationKind, server: str, stream_key: str) -> bool:
        try:
            addresses = await self._dns_validator(kind, server)
            await self._waiter(self._preflight(server, addresses[0]), self._timeout)
            return await self._publish(server, stream_key)
        except (OSError, TimeoutError, ValueError):
            return False

    async def _preflight(self, server: str, address: str) -> None:
        parsed = urlsplit(server)
        host = parsed.hostname
        if host is None:
            raise ValueError("invalid destination")
        use_tls = parsed.scheme == "rtmps"
        context = self._ssl_context_factory() if use_tls else None
        _reader, writer = await self._connector(
            address,
            parsed.port or (443 if use_tls else 1935),
            ssl=context,
            server_hostname=host if use_tls else None,
        )
        writer.close()
        await writer.wait_closed()

    async def _publish(self, server: str, stream_key: str) -> bool:
        command = build_destination_test_command(server, stream_key)
        process = await self._process_factory(command.argv)
        try:
            return bool(await self._waiter(self._wait_bounded(process), self._timeout))
        except TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.wait()
            return False

    @staticmethod
    async def _wait_bounded(process: ProcessPort) -> bool:
        stderr = b""
        if process.stderr is not None:
            stderr = await process.stderr.read(_MAX_STDERR + 1)
            if len(stderr) > _MAX_STDERR and process.returncode is None:
                process.kill()
        returncode = await process.wait()
        if _AUTH_FAILURE.search(stderr):
            return False
        return returncode == 0 and len(stderr) <= _MAX_STDERR
