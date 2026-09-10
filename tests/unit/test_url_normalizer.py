import pytest

from restream_studio.source import DouyinUrlValidationError, normalize_douyin_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://live.douyin.com/123456789", "https://live.douyin.com/123456789"),
        ("https://live.douyin.com/yall1102", "https://live.douyin.com/yall1102"),
        ("https://v.douyin.com/AbCd123/", "https://v.douyin.com/AbCd123/"),
        (
            "  https://LIVE.DOUYIN.COM/room_name-1?from=copy  ",
            "https://live.douyin.com/room_name-1",
        ),
        ("https://V.DOUYIN.COM/AbCd123?from=copy", "https://v.douyin.com/AbCd123/"),
    ],
)
def test_normalizes_supported_douyin_urls(raw: str, expected: str) -> None:
    assert normalize_douyin_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("http://live.douyin.com/123", "HTTPS"),
        ("ftp://live.douyin.com/123", "HTTPS"),
        ("https://user@live.douyin.com/123", "userinfo"),
        ("https://user:pass@live.douyin.com/123", "password"),
        ("https://live.douyin.com:444/123", "port"),
        ("https://douyin.com/123", "host"),
        ("https://evil-live.douyin.com/123", "host"),
        ("https://x.live.douyin.com/123", "host"),
        ("https://live.douyin.com", "room"),
        ("https://v.douyin.com/", "code"),
        ("https://live.douyin.com/a/b", "segment"),
        ("https://v.douyin.com/a/b", "segment"),
        ("https://live.douyin.com/.", "dot"),
        ("https://live.douyin.com/%2e%2e", "dot"),
        ("https://live.douyin.com/a%2Fb", "slash"),
        ("https://live.douyin.com/a%5Cb", "backslash"),
        ("https://live.douyin.com/room#section", "fragment"),
        ("https://live.douyin.com/room\n", "control"),
        ("https://live.douyin.com/room name", "characters"),
        ("https://v.douyin.com/abc_def", "characters"),
    ],
)
def test_rejects_invalid_url_shapes(raw: str, message: str) -> None:
    with pytest.raises(DouyinUrlValidationError, match=message):
        normalize_douyin_url(raw)


@pytest.mark.parametrize(
    "query",
    [
        "redirect=https://evil.example",
        "redirect_uri=%2F%2Fevil.example",
        "target=evil.example",
        "url=https%3A%2F%2Fevil.example%2Fpwn",
        "next=https://evil.example",
        "redirectUrl=https://evil.example",
        "target-url=evil.example",
        "from=https://evil.example",
        "from=%2F%2Fevil.example",
        "from=evil.example",
        "from=127.0.0.1",
    ],
)
def test_rejects_destination_queries(query: str) -> None:
    with pytest.raises(DouyinUrlValidationError, match="query"):
        normalize_douyin_url(f"https://live.douyin.com/123?{query}")


def test_validation_error_is_a_value_error_with_actionable_text() -> None:
    with pytest.raises(ValueError, match="host") as captured:
        normalize_douyin_url("https://example.com/123")

    assert isinstance(captured.value, DouyinUrlValidationError)
