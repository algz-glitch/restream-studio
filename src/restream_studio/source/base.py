from typing import Protocol, runtime_checkable

from restream_studio.domain.models import ResolvedStream


class ResolverError(Exception):
    """Base exception for failures contained by a live-source adapter."""


class ResolverRateLimited(ResolverError):
    """The source component requested that resolution be retried later."""

    def __init__(self, retry_after_seconds: int | None) -> None:
        super().__init__("live-source resolver rate limited")
        self.retry_after_seconds = retry_after_seconds


class ResolverProtocolError(ResolverError):
    """The source component is unavailable or returned an invalid contract."""


@runtime_checkable
class LiveSourceResolver(Protocol):
    async def resolve(self, url: str, preferred_quality: str | None) -> ResolvedStream: ...
