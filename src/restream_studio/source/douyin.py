import inspect
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit

from restream_studio.domain.models import ResolvedStream
from restream_studio.source.base import (
    ResolverError,
    ResolverProtocolError,
    ResolverRateLimited,
)
from restream_studio.source.url_normalizer import DouyinUrlValidationError, normalize_douyin_url

QUALITY_ORDER = ("origin", "blue", "ultra", "high", "standard", "smooth")
QUALITY_CODE_BY_NAME = {
    "origin": "OD",
    "blue": "BD",
    "ultra": "UHD",
    "high": "HD",
    "standard": "SD",
    "smooth": "LD",
}
QUALITY_NAME_BY_CODE = {
    "OD": "origin",
    "BD": "blue",
    "UHD": "ultra",
    "HD": "high",
    "SD": "standard",
    "LD": "smooth",
}
QUALITY_NAME_BY_SOURCE_KEY = {
    "ORIGIN": "origin",
    "OD": "origin",
    "BLUE": "blue",
    "BLUE_RAY": "blue",
    "BD": "blue",
    "ULTRA": "ultra",
    "UHD": "ultra",
    "FULL_HD": "ultra",
    "FULL_HD1": "ultra",
    "HIGH": "high",
    "HD": "high",
    "STANDARD": "standard",
    "SD": "standard",
    "SMOOTH": "smooth",
    "LD": "smooth",
}
ComponentResult = Mapping[str, object]
QualityCandidates = tuple[tuple[str, ...], tuple[str, ...]]
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
            if self._component is None:
                return await _resolve_with_streamget(normalized_url, preferred_quality)
            payload = await _invoke(self._component, normalized_url)
            return _map_payload(payload, preferred_quality)
        except ResolverError:
            raise
        except Exception as exc:
            raise ResolverProtocolError("Douyin component failed") from exc


def _default_component_factory() -> object:
    """Create StreamGet's supported Douyin client at the adapter boundary."""
    try:
        from streamget import DouyinLiveStream  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ResolverProtocolError("StreamGet Douyin component is unavailable") from exc
    return DouyinLiveStream()


async def _resolve_with_streamget(
    normalized_url: str, preferred_quality: str | None
) -> ResolvedStream:
    client = _default_component_factory()
    fetch_data = getattr(client, "fetch_web_stream_data", None)
    fetch_url = getattr(client, "fetch_stream_url", None)
    if not callable(fetch_data) or not callable(fetch_url):
        raise ResolverProtocolError("StreamGet Douyin component has an invalid API")
    web_data = await cast(Callable[[str], Awaitable[object]], fetch_data)(normalized_url)
    available_qualities = _streamget_quality_urls(web_data)
    quality_name = _select_streamget_quality(available_qualities, preferred_quality)
    if available_qualities:
        return _map_streamget_raw(
            web_data, normalized_url, quality_name, available_qualities[quality_name]
        )
    quality_code = QUALITY_CODE_BY_NAME[quality_name]
    stream_data = await cast(Callable[[object, str], Awaitable[object]], fetch_url)(
        web_data, quality_code
    )
    return _map_streamget(stream_data, normalized_url, quality_name, {})


def _map_streamget_raw(
    web_data: object,
    normalized_url: str,
    selected_quality: str,
    candidates: QualityCandidates,
) -> ResolvedStream:
    if not isinstance(web_data, Mapping):
        raise ResolverProtocolError("StreamGet web data is malformed")
    status = web_data.get("status")
    if status != 2:
        raise ResolverProtocolError("StreamGet live data status is malformed")
    anchor_name = _nonempty_string(web_data.get("anchor_name"), "StreamGet.anchor_name")
    room_id = _streamget_room_id(web_data, normalized_url)
    flv_urls, hls_urls = candidates
    if not flv_urls and not hls_urls:
        raise ResolverProtocolError("selected StreamGet quality has no media URL")
    return ResolvedStream(
        url=(flv_urls or hls_urls)[0],
        acquired_at=datetime.now(UTC),
        expires_at=None,
        room_id=room_id,
        anchor_name=anchor_name,
        is_live=True,
        selected_quality=selected_quality,
        flv_urls=flv_urls,
        hls_urls=hls_urls,
    )


def _map_streamget(
    stream_data: object,
    normalized_url: str,
    requested_quality: str,
    available_qualities: Mapping[str, QualityCandidates],
) -> ResolvedStream:
    acquired_at = datetime.now(UTC)
    is_live = _streamget_field(stream_data, "is_live")
    if not isinstance(is_live, bool):
        raise ResolverProtocolError("StreamGet field is_live is malformed")
    anchor_name = _nonempty_string(
        _streamget_field(stream_data, "anchor_name"), "StreamGet.anchor_name"
    )
    room_id = _streamget_room_id(stream_data, normalized_url)
    if not is_live:
        return ResolvedStream(
            url="",
            acquired_at=acquired_at,
            expires_at=None,
            room_id=room_id,
            anchor_name=anchor_name,
            is_live=False,
        )

    flv_urls = _media_urls(_streamget_field(stream_data, "flv_url"), "StreamGet.flv_url")
    hls_urls = _media_urls(
        _streamget_field(stream_data, "m3u8_url"), "StreamGet.m3u8_url"
    )
    if not flv_urls and not hls_urls:
        raise ResolverProtocolError("StreamGet live result has no media URL")
    selected_quality = _actual_streamget_quality(
        stream_data, flv_urls + hls_urls, requested_quality, available_qualities
    )
    return ResolvedStream(
        url=(flv_urls or hls_urls)[0],
        acquired_at=acquired_at,
        expires_at=None,
        room_id=room_id,
        anchor_name=anchor_name,
        is_live=True,
        selected_quality=selected_quality,
        flv_urls=flv_urls,
        hls_urls=hls_urls,
    )


def _streamget_quality_urls(web_data: object) -> dict[str, QualityCandidates]:
    if not isinstance(web_data, Mapping):
        return {}
    stream_url = web_data.get("stream_url", web_data.get("streamUrl"))
    if not isinstance(stream_url, Mapping):
        return {}
    result: dict[str, tuple[list[str], list[str]]] = {}
    map_names = (
        ("flv_pull_url", 0),
        ("flvPullUrl", 0),
        ("hls_pull_url_map", 1),
        ("hlsPullUrlMap", 1),
    )
    for map_name, candidate_index in map_names:
        url_map = stream_url.get(map_name)
        if not isinstance(url_map, Mapping):
            continue
        for source_key, url in url_map.items():
            if not isinstance(source_key, str) or not isinstance(url, str) or not url:
                continue
            normalized_key = source_key.strip().upper().replace("-", "_").replace(" ", "_")
            quality_name = QUALITY_NAME_BY_SOURCE_KEY.get(normalized_key)
            if quality_name is not None:
                candidates = result.setdefault(quality_name, ([], []))
                candidates[candidate_index].append(url)
    return {
        quality: (tuple(flv_urls), tuple(hls_urls))
        for quality, (flv_urls, hls_urls) in result.items()
    }


def _select_streamget_quality(
    available_qualities: Mapping[str, QualityCandidates], preferred_quality: str | None
) -> str:
    if not available_qualities:
        return preferred_quality if preferred_quality in QUALITY_CODE_BY_NAME else "origin"
    if preferred_quality in available_qualities:
        return cast(str, preferred_quality)
    for quality in QUALITY_ORDER:
        if quality in available_qualities:
            return quality
    raise ResolverProtocolError("StreamGet data has no supported stream quality")


def _actual_streamget_quality(
    stream_data: object,
    returned_urls: tuple[str, ...],
    requested_quality: str,
    available_qualities: Mapping[str, QualityCandidates],
) -> str:
    for quality in QUALITY_ORDER:
        candidates = available_qualities.get(quality, ((), ()))
        if any(url in candidates[0] + candidates[1] for url in returned_urls):
            return quality
    actual_code = _streamget_field(stream_data, "quality")
    if isinstance(actual_code, str):
        reported = QUALITY_NAME_BY_CODE.get(actual_code.upper())
        if reported in available_qualities or not available_qualities:
            return cast(str, reported)
    return requested_quality


def _streamget_field(stream_data: object, field: str) -> object:
    if isinstance(stream_data, Mapping):
        return stream_data.get(field)
    return getattr(stream_data, field, None)


def _streamget_room_id(stream_data: object, normalized_url: str) -> str:
    for field in ("room_id", "web_rid"):
        value = _streamget_field(stream_data, field)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
            return str(value)
    extra = _streamget_field(stream_data, "extra")
    if isinstance(extra, Mapping):
        for field in ("room_id", "web_rid"):
            value = extra.get(field)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
                return str(value)
    live_url = _streamget_field(stream_data, "live_url")
    candidate = live_url if isinstance(live_url, str) else normalized_url
    segment = urlsplit(candidate).path.strip("/").split("/")[-1]
    if segment:
        return segment
    raise ResolverProtocolError("StreamGet result has no room identifier")


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
