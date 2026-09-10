import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import Never, cast
from urllib.parse import parse_qs, urlsplit

from restream_studio.domain.models import ResolvedStream
from restream_studio.source.base import (
    ResolverError,
    ResolverNetworkError,
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
    "HD1": "high",
    "STANDARD": "standard",
    "SD": "standard",
    "SD1": "standard",
    "SMOOTH": "smooth",
    "LD": "smooth",
    "SD2": "smooth",
}
ComponentResult = Mapping[str, object]
QualityCandidates = tuple[tuple[str, ...], tuple[str, ...]]
SyncComponent = Callable[[str], ComponentResult]
AsyncComponent = Callable[[str], Awaitable[ComponentResult]]
LOGGER = logging.getLogger(__name__)


class DouyinResolver:
    """Contain a replaceable Douyin parser behind the resolver contract."""

    def __init__(self, component: object | None = None, candidate_probe: object | None = None) -> None:
        self._component = component
        self._candidate_probe = candidate_probe

    async def resolve(self, url: str, preferred_quality: str | None) -> ResolvedStream:
        try:
            normalized_url = normalize_douyin_url(url)
        except DouyinUrlValidationError as exc:
            raise ResolverProtocolError("Douyin URL is invalid") from exc

        try:
            if self._component is None:
                return await _resolve_with_streamget(
                    normalized_url, preferred_quality, self._candidate_probe
                )
            payload = await _invoke(self._component, normalized_url)
            return _map_payload(payload, preferred_quality)
        except ResolverError:
            raise
        except Exception as exc:  # noqa: BLE001 - third-party boundary must contain all errors
            _raise_translated_exception(exc)


def _default_component_factory() -> object:
    """Create StreamGet's supported Douyin client at the adapter boundary."""
    try:
        from streamget import DouyinLiveStream  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ResolverProtocolError("StreamGet Douyin component is unavailable") from exc
    return DouyinLiveStream()


async def _resolve_with_streamget(
    normalized_url: str, preferred_quality: str | None, candidate_probe: object | None
) -> ResolvedStream:
    client = _default_component_factory()
    fetch_data = getattr(client, "fetch_web_stream_data", None)
    fetch_url = getattr(client, "fetch_stream_url", None)
    if not callable(fetch_data) or not callable(fetch_url):
        raise ResolverProtocolError("StreamGet Douyin component has an invalid API")
    web_data = await cast(Callable[[str], Awaitable[object]], fetch_data)(normalized_url)
    status = _streamget_status(web_data)
    if status == 4:
        return _map_streamget_offline(web_data, normalized_url)
    raw_qualities = _streamget_quality_urls(web_data)
    available_qualities = await _probe_quality_candidates(raw_qualities, candidate_probe)
    if raw_qualities and candidate_probe is not None and not available_qualities:
        raise ResolverNetworkError("candidate probe found no playable media candidates")
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
    anchor_name = _nonempty_string(web_data.get("anchor_name"), "StreamGet.anchor_name")
    room_id = _streamget_room_id(web_data, normalized_url)
    flv_urls, hls_urls = candidates
    if not flv_urls and not hls_urls:
        raise ResolverProtocolError("selected StreamGet quality has no media URL")
    acquired_at = datetime.now(UTC)
    return ResolvedStream(
        url=(flv_urls or hls_urls)[0],
        acquired_at=acquired_at,
        expires_at=_expires_at(flv_urls + hls_urls, acquired_at),
        room_id=room_id,
        anchor_name=anchor_name,
        is_live=True,
        selected_quality=selected_quality,
        flv_urls=flv_urls,
        hls_urls=hls_urls,
    )


def _map_streamget_offline(web_data: object, normalized_url: str) -> ResolvedStream:
    if not isinstance(web_data, Mapping):
        raise ResolverProtocolError("StreamGet web data is malformed")
    return ResolvedStream(
        url="",
        acquired_at=datetime.now(UTC),
        expires_at=None,
        room_id=_streamget_room_id(web_data, normalized_url),
        anchor_name=_nonempty_string(web_data.get("anchor_name"), "StreamGet.anchor_name"),
        is_live=False,
    )


def _streamget_status(web_data: object) -> int:
    if not isinstance(web_data, Mapping):
        raise ResolverProtocolError("StreamGet web data is malformed")
    status_code = web_data.get("status_code")
    message = " ".join(
        str(web_data.get(key, "")) for key in ("message", "error", "detail")
    ).casefold()
    if status_code == 429 or any(
        term in message for term in ("rate limit", "rate_limited", "频繁", "风控")
    ):
        retry = web_data.get("retry_after", web_data.get("retry_after_seconds"))
        raise ResolverRateLimited(_retry_after(retry))
    _raise_component_error(cast(ComponentResult, web_data))
    status = web_data.get("status")
    if not isinstance(status, int) or isinstance(status, bool) or status not in (2, 4):
        raise ResolverProtocolError("StreamGet status is malformed")
    return status


async def _probe_quality_candidates(
    qualities: Mapping[str, QualityCandidates], probe: object | None
) -> dict[str, QualityCandidates]:
    if probe is None:
        return dict(qualities)
    candidate = getattr(probe, "is_playable", probe)
    if not callable(candidate):
        raise ResolverProtocolError("candidate probe is not callable")
    result: dict[str, QualityCandidates] = {}
    for quality, (flv_urls, hls_urls) in qualities.items():
        valid: list[list[str]] = [[], []]
        for index, urls in enumerate((flv_urls, hls_urls)):
            for url in urls:
                outcome = candidate(url)
                if inspect.isawaitable(outcome):
                    outcome = await cast(Awaitable[object], outcome)
                if not isinstance(outcome, bool):
                    raise ResolverProtocolError("candidate probe returned a malformed result")
                if outcome:
                    valid[index].append(url)
        if valid[0] or valid[1]:
            result[quality] = (tuple(valid[0]), tuple(valid[1]))
    return result


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
        expires_at=_expires_at(flv_urls + hls_urls, acquired_at),
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
    unknown_count = 0
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
                candidates[candidate_index].append(_validate_media_url(url))
            else:
                unknown_count += 1
    if unknown_count:
        LOGGER.warning("ignored %d unrecognized StreamGet quality entries", unknown_count)
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
    if status not in (2, 4):
        raise ResolverProtocolError("Douyin payload field room.status is unknown")

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
    expires_at = _optional_datetime(
        payload.get("expires_at"), acquired_at, flv_urls + hls_urls
    )

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
    return tuple(_validate_media_url(item) for item in cast(list[str] | tuple[str, ...], values))


def _optional_datetime(
    value: object, acquired_at: datetime, urls: tuple[str, ...] = ()
) -> datetime | None:
    if value is None:
        return _expires_at(urls, acquired_at)
    if not isinstance(value, str):
        raise ResolverProtocolError("Douyin payload field expires_at is malformed")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ResolverProtocolError("Douyin payload field expires_at is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed <= acquired_at:
        raise ResolverProtocolError("Douyin payload field expires_at is malformed")
    return parsed


def _validate_media_url(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ResolverProtocolError("media URL contains control characters")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ResolverProtocolError("media URL is malformed") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ResolverProtocolError("media URL is malformed")
    if parsed.username is not None or parsed.password is not None:
        raise ResolverProtocolError("media URL must not contain userinfo")
    lowered_host = host.casefold().rstrip(".")
    if lowered_host == "localhost" or "_" in lowered_host:
        raise ResolverProtocolError("media URL host is not public")
    try:
        address = ip_address(lowered_host)
    except ValueError:
        labels = lowered_host.split(".")
        label_pattern = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
        if len(labels) < 2 or any(label_pattern.fullmatch(label) is None for label in labels):
            raise ResolverProtocolError("media URL domain is malformed") from None
    else:
        if not address.is_global:
            raise ResolverProtocolError("media URL IP address is not public")
    return value


def _expires_at(urls: tuple[str, ...], acquired_at: datetime) -> datetime:
    expiries: list[datetime] = []
    for url in urls:
        query = parse_qs(urlsplit(url).query)
        for key, values in query.items():
            if key.casefold() not in {"expiry", "expires", "expire", "wstime"}:
                continue
            for value in values:
                try:
                    timestamp = int(value, 16 if key.casefold() == "wstime" else 10)
                    parsed = datetime.fromtimestamp(timestamp, UTC)
                except (ValueError, OSError, OverflowError):
                    continue
                if parsed > acquired_at:
                    expiries.append(parsed)
    return min(expiries) if expiries else acquired_at + timedelta(minutes=5)


def _retry_after(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ResolverProtocolError("retry-after value is malformed")


def _raise_translated_exception(exc: Exception) -> Never:
    if isinstance(exc, ResolverError):
        raise exc
    message = str(exc).casefold()
    status_code = getattr(exc, "status_code", None)
    if status_code == 429 or any(
        term in message for term in ("rate limit", "rate_limited", "频繁", "风控")
    ):
        raise ResolverRateLimited(_retry_after(getattr(exc, "retry_after", None))) from None
    if isinstance(exc, (TimeoutError, ConnectionError)):
        raise ResolverNetworkError("Douyin component network failure") from None
    raise ResolverProtocolError("Douyin component failed") from None
