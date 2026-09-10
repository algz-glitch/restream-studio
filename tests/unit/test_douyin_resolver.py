import json
import os
from collections.abc import Coroutine, Mapping
from pathlib import Path
from typing import Any

import pytest

from restream_studio.source.base import (
    LiveSourceResolver,
    ResolverProtocolError,
    ResolverRateLimited,
)
from restream_studio.source.douyin import QUALITY_CODE_BY_NAME, DouyinResolver

FIXTURES = Path(__file__).parents[1] / "fixtures"


def fixture(name: str) -> dict[str, Any]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def run_immediate(coroutine: Coroutine[object, object, Any]) -> Any:
    """Drive adapter-only coroutines without opening a Windows network event loop."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("offline component unexpectedly suspended")


def test_live_fixture_maps_metadata_candidates_and_best_quality() -> None:
    calls: list[str] = []

    async def component(url: str) -> Mapping[str, object]:
        calls.append(url)
        return fixture("douyin_live.json")

    resolver: LiveSourceResolver = DouyinResolver(component=component)
    result = run_immediate(resolver.resolve("https://LIVE.DOUYIN.COM/731234?from=copy", None))

    assert calls == ["https://live.douyin.com/731234"]
    assert result.room_id == "7312345678901234567"
    assert result.anchor_name == "fixture-anchor"
    assert result.is_live is True
    assert result.selected_quality == "blue"
    assert result.flv_urls == (
        "https://media.example.test/live/blue.flv?signature=cleaned",
    )
    assert result.hls_urls == (
        "https://media.example.test/live/blue.m3u8?signature=cleaned",
    )
    assert result.url == result.flv_urls[0]
    assert result.acquired_at.tzinfo is not None
    assert result.expires_at is not None
    assert result.expires_at > result.acquired_at


def test_preferred_quality_wins_over_ranked_best() -> None:
    resolver = DouyinResolver(component=lambda _: fixture("douyin_live.json"))
    result = run_immediate(resolver.resolve("https://live.douyin.com/731234", "high"))
    assert result.selected_quality == "high"
    assert "/high.flv" in result.url


def test_quality_falls_back_in_documented_order() -> None:
    payload = fixture("douyin_live.json")
    payload["streams"] = {
        "smooth": {"hls": "https://media.example.test/smooth.m3u8"},
        "ultra": {"hls": "https://media.example.test/ultra.m3u8"},
        "origin": {"flv": "https://media.example.test/origin.flv"},
    }
    resolver = DouyinResolver(component=lambda _: payload)
    result = run_immediate(resolver.resolve("https://live.douyin.com/731234", "missing"))
    assert result.selected_quality == "origin"
    assert result.url.endswith("origin.flv")


def test_offline_fixture_has_no_media_urls() -> None:
    resolver = DouyinResolver(component=lambda _: fixture("douyin_offline.json"))
    result = run_immediate(resolver.resolve("https://live.douyin.com/731234", None))
    assert result.is_live is False
    assert result.url == ""
    assert result.selected_quality is None
    assert result.flv_urls == ()
    assert result.hls_urls == ()
    assert result.expires_at is None


def test_component_is_called_on_every_resolution() -> None:
    calls = 0

    def component(_: str) -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        return fixture("douyin_offline.json")

    resolver = DouyinResolver(component=component)
    run_immediate(resolver.resolve("https://live.douyin.com/731234", None))
    run_immediate(resolver.resolve("https://live.douyin.com/731234", None))
    assert calls == 2


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("room"),
        lambda payload: payload["room"].pop("id"),
        lambda payload: payload["room"].update(status="live"),
        lambda payload: payload.update(streams=[]),
        lambda payload: payload["streams"].update(blue={"flv": 123}),
        lambda payload: payload.update(expires_at="not-a-date"),
    ],
)
def test_missing_or_malformed_live_fields_raise_protocol_error(mutation: Any) -> None:
    payload = fixture("douyin_live.json")
    mutation(payload)
    resolver = DouyinResolver(component=lambda _: payload)
    with pytest.raises(ResolverProtocolError):
        run_immediate(resolver.resolve("https://live.douyin.com/731234", None))


def test_rate_limit_is_mapped_with_retry_after() -> None:
    resolver = DouyinResolver(
        component=lambda _: {"error": {"type": "rate_limited", "retry_after_seconds": 17}}
    )
    with pytest.raises(ResolverRateLimited) as caught:
        run_immediate(resolver.resolve("https://live.douyin.com/731234", None))
    assert caught.value.retry_after_seconds == 17


def test_arbitrary_third_party_exception_never_crosses_boundary() -> None:
    class VendorFailure(Exception):
        pass

    def component(_: str) -> Mapping[str, object]:
        raise VendorFailure("vendor internals")

    resolver = DouyinResolver(component=component)
    with pytest.raises(ResolverProtocolError) as caught:
        run_immediate(resolver.resolve("https://live.douyin.com/731234", None))
    assert "vendor internals" not in str(caught.value)
    assert not isinstance(caught.value, VendorFailure)


def test_default_component_unavailable_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> object:
        raise ResolverProtocolError("StreamGet Douyin component is unavailable")

    monkeypatch.setattr("restream_studio.source.douyin._default_component_factory", unavailable)
    resolver = DouyinResolver()
    with pytest.raises(ResolverProtocolError, match="StreamGet Douyin component is unavailable"):
        run_immediate(resolver.resolve("https://live.douyin.com/731234", None))


def test_client_object_with_sync_resolve_is_supported() -> None:
    class Client:
        def resolve(self, url: str) -> Mapping[str, object]:
            assert url == "https://live.douyin.com/731234"
            return fixture("douyin_offline.json")

    result = run_immediate(DouyinResolver(component=Client()).resolve("https://live.douyin.com/731234", None))
    assert result.is_live is False


def test_streamget_quality_codes_match_the_supported_public_contract() -> None:
    assert QUALITY_CODE_BY_NAME == {
        "origin": "OD",
        "blue": "BD",
        "ultra": "UHD",
        "high": "HD",
        "standard": "SD",
        "smooth": "LD",
    }


def test_default_adapter_uses_streamget_api_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []
    web_data = {
        "status": 2,
        "stream_url": {
            "flv_pull_url": {"ORIGIN": "origin-flv", "UHD": "ultra-flv"},
            "hls_pull_url_map": {"ORIGIN": "origin-hls", "UHD": "ultra-hls"},
        },
    }

    class StreamData:
        room_id = "7312345678901234567"
        anchor_name = "streamget-anchor"
        is_live = True
        quality = "UHD"
        flv_url = "https://media.example.test/streamget.flv"
        m3u8_url = "https://media.example.test/streamget.m3u8"

    class FakeDouyinLiveStream:
        async def fetch_web_stream_data(self, url: str) -> object:
            calls.append(("fetch_web_stream_data", url))
            return web_data

        async def fetch_stream_url(self, data: object, quality: str) -> object:
            calls.append(("fetch_stream_url", data, quality))
            return StreamData()

    monkeypatch.setattr(
        "restream_studio.source.douyin._default_component_factory", FakeDouyinLiveStream
    )
    result = run_immediate(
        DouyinResolver().resolve("https://LIVE.DOUYIN.COM/731234?from=copy", "ultra")
    )

    assert calls == [
        ("fetch_web_stream_data", "https://live.douyin.com/731234"),
        ("fetch_stream_url", web_data, "UHD"),
    ]
    assert result.room_id == "7312345678901234567"
    assert result.anchor_name == "streamget-anchor"
    assert result.selected_quality == "ultra"
    assert result.flv_urls == ("https://media.example.test/streamget.flv",)
    assert result.hls_urls == ("https://media.example.test/streamget.m3u8",)


def test_default_adapter_falls_back_and_reports_actual_returned_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    web_data = {
        "status": 2,
        "stream_url": {
            "flv_pull_url": {
                "ORIGIN": "https://media.example.test/origin.flv",
                "UHD": "https://media.example.test/ultra.flv",
                "HD": "https://media.example.test/high.flv",
            },
            "hls_pull_url_map": {},
        },
    }

    class FakeDouyinLiveStream:
        async def fetch_web_stream_data(self, url: str) -> object:
            return web_data

        async def fetch_stream_url(self, data: object, quality: str) -> object:
            calls.append(quality)
            return {
                "room_id": "731234",
                "anchor_name": "fallback-anchor",
                "is_live": True,
                "quality": "HD",
                "flv_url": "https://media.example.test/high.flv",
                "m3u8_url": None,
            }

    monkeypatch.setattr(
        "restream_studio.source.douyin._default_component_factory", FakeDouyinLiveStream
    )
    result = run_immediate(DouyinResolver().resolve("https://live.douyin.com/731234", "blue"))

    assert calls == ["OD"]
    assert result.selected_quality == "high"


def test_default_adapter_selects_blue_only_when_bd_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    web_data = {
        "status": 2,
        "stream_url": {
            "flv_pull_url": {
                "ORIGIN": "https://media.example.test/origin.flv",
                "BD": "https://media.example.test/blue.flv",
            },
            "hls_pull_url_map": {},
        },
    }

    class FakeDouyinLiveStream:
        async def fetch_web_stream_data(self, url: str) -> object:
            return web_data

        async def fetch_stream_url(self, data: object, quality: str) -> object:
            calls.append(quality)
            return {
                "room_id": "731234",
                "anchor_name": "blue-anchor",
                "is_live": True,
                "quality": "BD",
                "flv_url": "https://media.example.test/blue.flv",
                "m3u8_url": None,
            }

    monkeypatch.setattr(
        "restream_studio.source.douyin._default_component_factory", FakeDouyinLiveStream
    )
    result = run_immediate(DouyinResolver().resolve("https://live.douyin.com/731234", "blue"))

    assert calls == ["BD"]
    assert result.selected_quality == "blue"


def test_default_adapter_maps_streamget_dictionary_result(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDouyinLiveStream:
        async def fetch_web_stream_data(self, url: str) -> object:
            return {"source_url": url}

        async def fetch_stream_url(self, data: object, quality: str) -> object:
            return {
                "anchor_name": "dictionary-anchor",
                "is_live": False,
                "quality": quality,
                "flv_url": None,
                "m3u8_url": None,
                "live_url": "https://live.douyin.com/998877",
            }

    monkeypatch.setattr(
        "restream_studio.source.douyin._default_component_factory", FakeDouyinLiveStream
    )
    result = run_immediate(DouyinResolver().resolve("https://v.douyin.com/AbCd123/", None))
    assert result.room_id == "998877"
    assert result.anchor_name == "dictionary-anchor"
    assert result.is_live is False
    assert result.url == ""


def test_default_streamget_exception_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    class VendorFailure(Exception):
        pass

    class FakeDouyinLiveStream:
        async def fetch_web_stream_data(self, url: str) -> object:
            raise VendorFailure(url)

        async def fetch_stream_url(self, data: object, quality: str) -> object:
            raise AssertionError("must not be called")

    monkeypatch.setattr(
        "restream_studio.source.douyin._default_component_factory", FakeDouyinLiveStream
    )
    with pytest.raises(ResolverProtocolError) as caught:
        run_immediate(DouyinResolver().resolve("https://live.douyin.com/731234", None))
    assert not isinstance(caught.value, VendorFailure)


@pytest.mark.live_network
@pytest.mark.asyncio
async def test_live_network_schema_invariants() -> None:
    room_url = os.getenv("DOUYIN_TEST_ROOM_URL")
    if not room_url:
        pytest.skip("DOUYIN_TEST_ROOM_URL is not set")
    result = await DouyinResolver().resolve(room_url, None)
    assert result.room_id
    assert result.anchor_name
    assert result.acquired_at.tzinfo is not None
    if result.is_live:
        assert result.selected_quality
        assert result.flv_urls or result.hls_urls
        assert result.url
    else:
        assert result.url == ""
        assert not result.flv_urls
        assert not result.hls_urls
