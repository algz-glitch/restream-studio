from restream_studio.security.redaction import redact


def test_redact_recursively_masks_credentials_without_mutating_input() -> None:
    original = {
        "Stream_Key": "alpha-key",
        "COOKIE": "session=value",
        "Authorization": "Bearer opaque-token",
        "nested": [
            "rtmp://media.example/live/private-stream-key",
            {"authorization": "Basic encoded-credentials", "safe": 42},
        ],
        "tuple": (True, None, 7),
    }

    result = redact(original)

    assert result == {
        "Stream_Key": "***",
        "COOKIE": "***",
        "Authorization": "***",
        "nested": [
            "rtmp://media.example/live/***",
            {"authorization": "***", "safe": 42},
        ],
        "tuple": (True, None, 7),
    }
    assert original["Stream_Key"] == "alpha-key"
    assert original["nested"][0] == "rtmp://media.example/live/private-stream-key"
    assert isinstance(result["tuple"], tuple)


def test_redact_normalizes_common_sensitive_mapping_keys_recursively() -> None:
    nested = [{"stream-key": "value-8", "diagnosticCode": "ok"}]
    original: dict[str, object] = {
        "TOKEN": "value-1",
        "accessToken": "value-2",
        "refresh-token": "value-3",
        "Password": "value-4",
        "client_secret": "value-5",
        "apiKey": "value-6",
        "SIGNATURE": "value-7",
        "nested": nested,
    }

    assert redact(original) == {
        "TOKEN": "***",
        "accessToken": "***",
        "refresh-token": "***",
        "Password": "***",
        "client_secret": "***",
        "apiKey": "***",
        "SIGNATURE": "***",
        "nested": [{"stream-key": "***", "diagnosticCode": "ok"}],
    }
    assert nested[0]["stream-key"] == "value-8"


def test_redact_masks_sensitive_url_queries_including_encoded_values() -> None:
    value = (
        "https://example.test/path?stream_key=key%2Fpart&cookie=session%3Dvalue"
        "&authorization=Bearer%20opaque-token&quality=high"
    )

    result = redact(value)

    assert result == (
        "https://example.test/path?stream_key=***&cookie=***"
        "&authorization=***&quality=high"
    )


def test_redact_masks_rtmp_path_and_query_but_preserves_diagnostics() -> None:
    value = "rtmps://media.example/app/path-key?token=query-secret&latency=low"

    result = redact(value)

    assert result == "rtmps://media.example/app/***?token=***&latency=low"


def test_redact_masks_entire_multi_segment_rtmp_stream_key() -> None:
    value = "rtmp://media.example/app/key/part/segment?latency=low"

    assert redact(value) == "rtmp://media.example/app/***?latency=low"


def test_redact_masks_command_argument_credentials() -> None:
    command = [
        "ffmpeg",
        "-i",
        "input.mp4",
        "-headers",
        "Authorization: Bearer command-token",
        "-f",
        "flv",
        "rtmp://media.example/live/command-stream-key",
    ]

    result = redact(command)

    assert result == [
        "ffmpeg",
        "-i",
        "input.mp4",
        "-headers",
        "Authorization: ***",
        "-f",
        "flv",
        "rtmp://media.example/live/***",
    ]


def test_redact_masks_command_stream_key_flags() -> None:
    command = ["publisher", "--stream-key=inline-key", "-stream_key", "separate-key"]

    assert redact(command) == ["publisher", "--stream-key=***", "-stream_key", "***"]


def test_redact_masks_command_cookie_header() -> None:
    command = ["ffmpeg", "-headers", "Cookie: session=credential", "-i", "input.mp4"]

    assert redact(command) == ["ffmpeg", "-headers", "Cookie: ***", "-i", "input.mp4"]


def test_redact_masks_standalone_authorization_values() -> None:
    assert redact("Bearer standalone-token") == "***"
    assert redact("Basic standalone-credentials") == "***"


def test_redact_leaves_scalar_diagnostics_unchanged() -> None:
    assert redact(123) == 123
    assert redact(False) is False
    assert redact(None) is None
    assert redact("ordinary diagnostic") == "ordinary diagnostic"
