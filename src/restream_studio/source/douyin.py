import inspect
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from importlib import import_module
from typing import cast

from restream_studio.domain.models import ResolvedStream
from restream_studio.source.base import (
    ResolverError,
    ResolverProtocolError,
    ResolverRateLimited,
)
from restream_studio.source.url_normalizer import DouyinUrlValidationError, normalize_douyin_url

QUALITY_ORDER = ("origin", "blue", "ultra", "high", "standard", "smooth")
ComponentResult = Mapping[str, object]
SyncComponent = Callable[[str], ComponentResult]
AsyncComponent = Callable[[str], Awaitable[ComponentResult]]


class DouyinResolver:
    """Contain a replaceable Douyin parser behind the resolver contract."""

    def __init__(self, component: object | None = None) -> None:
        self._component = component

    async def resolve(self, url: str, preferred_quality: str | None) -> ResolvedStream:
        try:
            normalized_url = normalize_douyin_url(url)
        except DouyinUrlValidationError as exc:
            raise ResolverProtocolError("Douyin URL is invalid") from exc

        try:
            component = self._component if self._component is not None else _load_default_component()
            payload = await _invoke(component, normalized_url)
            return _map_payload(payload, preferred_quality)
        except ResolverError:
            raise
        except Exception as exc:
            raise ResolverProtocolError("Douyin component failed") from exc


def _load_default_component() -> object:
    """Load the selected optional ``douyin-live`` integration only at the boundary."""
    try:
        module = import_module("douyin_live")
        component = module.resolve
    except (ImportError, AttributeError) as exc:
        raise ResolverProtocolError("Douyin resolver component is unavailable") from exc
    if not callable(component):
        raise ResolverProtocolError("Douyin resolver component is unavailable")
    return component


async def _invoke(component: object, url: str) -> ComponentResult:
    candidate = getattr(component, "resolve", component)
    if not callable(candidate):
        raise ResolverProtocolError("Douyin component is not callable")

    if inspect.iscoroutinefunction(candidate):
        result = await cast(AsyncComponent, candidate)(url)
    else:
        result = cast(SyncComponent, candidate)(url)
        if inspect.isawaitable(result):
            result = await cast(Awaitable[ComponentResult], result)
    if not isinstance(result, Mapping):
        raise ResolverProtocolError("Douyin component returned a malformed payload")
    return result


def _map_payload(payload: ComponentResult, preferred_quality: str | None) -> ResolvedStream:
    _raise_component_error(payload)
    room = _mapping(payload.get("room"), "room")
    room_id = _nonempty_string(room.get("id"), "room.id")
    anchor = _mapping(room.get("anchor"), "room.anchor")
    anchor_name = _nonempty_string(anchor.get("nickname"), "room.anchor.nickname")
    status = room.get("status")
    if not isinstance(status, int) or isinstance(status, bool):
        raise ResolverProtocolError("Douyin payload field room.status is malformed")

    acquired_at = datetime.now(UTC)
    if status != 2:
        return ResolvedStream(
            url="",
            acquired_at=acquired_at,
            expires_at=None,
            room_id=room_id,
            anchor_name=anchor_name,
            is_live=False,
        )

    streams = _mapping(payload.get("streams"), "streams")
    selected_quality = _select_quality(streams, preferred_quality)
    selected = _mapping(streams.get(selected_quality), f"streams.{selected_quality}")
    flv_urls = _media_urls(selected.get("flv"), f"streams.{selected_quality}.flv")
    hls_urls = _media_urls(selected.get("hls"), f"streams.{selected_quality}.hls")
    if not flv_urls and not hls_urls:
        raise ResolverProtocolError("selected stream quality has no media URL")
    expires_at = _optional_datetime(payload.get("expires_at"), acquired_at)

    return ResolvedStream(
        url=(flv_urls or hls_urls)[0],
        acquired_at=acquired_at,
        expires_at=expires_at,
        room_id=room_id,
        anchor_name=anchor_name,
        is_live=True,
        selected_quality=selected_quality,
        flv_urls=flv_urls,
        hls_urls=hls_urls,
    )


def _raise_component_error(payload: ComponentResult) -> None:
    error = payload.get("error")
    if error is None:
        return
    error_mapping = _mapping(error, "error")
    if error_mapping.get("type") == "rate_limited":
        retry_after = error_mapping.get("retry_after_seconds")
        if retry_after is not None and (
            not isinstance(retry_after, int) or isinstance(retry_after, bool) or retry_after < 0
        ):
            raise ResolverProtocolError("rate-limit retry value is malformed")
        raise ResolverRateLimited(retry_after)
    raise ResolverProtocolError("Douyin component reported an error")


def _select_quality(streams: Mapping[str, object], preferred_quality: str | None) -> str:
    if preferred_quality in streams:
        return cast(str, preferred_quality)
    for quality in QUALITY_ORDER:
        if quality in streams:
            return quality
    raise ResolverProtocolError("live Douyin payload has no supported stream quality")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ResolverProtocolError(f"Douyin payload field {field} is malformed")
    return cast(Mapping[str, object], value)


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResolverProtocolError(f"Douyin payload field {field} is malformed")
    return value


def _media_urls(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    values = (value,) if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)) or not values:
        raise ResolverProtocolError(f"Douyin payload field {field} is malformed")
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise ResolverProtocolError(f"Douyin payload field {field} is malformed")
    return tuple(cast(list[str] | tuple[str, ...], values))


def _optional_datetime(value: object, acquired_at: datetime) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ResolverProtocolError("Douyin payload field expires_at is malformed")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ResolverProtocolError("Douyin payload field expires_at is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed <= acquired_at:
        raise ResolverProtocolError("Douyin payload field expires_at is malformed")
    return parsed
