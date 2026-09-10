"""Non-mutating recursive redaction for streaming logs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

_MASK: Final = "***"
_SENSITIVE_KEYS: Final = {"stream_key", "cookie", "authorization"}
_SENSITIVE_QUERY_KEYS: Final = {
    "access_token",
    "auth",
    "authorization",
    "cookie",
    "key",
    "password",
    "secret",
    "signature",
    "stream_key",
    "token",
}
_SENSITIVE_FLAGS: Final = {
    "--authorization",
    "--cookie",
    "--stream-key",
    "-authorization",
    "-cookie",
    "-stream_key",
}
_AUTH_HEADER = re.compile(r"(?i)(\bauthorization\s*:\s*)(?:bearer|basic)\s+\S+")
_COOKIE_HEADER = re.compile(r"(?i)(\bcookie\s*:\s*)\S.*")
_AUTH_VALUE = re.compile(r"(?i)^\s*(?:bearer|basic)\s+\S+\s*$")
_INLINE_FLAG = re.compile(r"(?i)^(--stream-key|-stream_key|--cookie|--authorization)=(.*)$")
_QUERY_VALUE = re.compile(r"([?&])([^=&]+)=([^&#]*)")
_RTMP_URL = re.compile(r"(?i)\b(rtmps?://[^/?#]+)(/[^?#]*)?(\?[^#]*)?")


def _redact_query(query: str) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(2)
        if key.lower() in _SENSITIVE_QUERY_KEYS:
            return f"{match.group(1)}{key}={_MASK}"
        return match.group(0)

    return _QUERY_VALUE.sub(replace, query)


def _redact_rtmp(match: re.Match[str]) -> str:
    authority = match.group(1)
    path = match.group(2) or ""
    query = match.group(3) or ""
    segments = path.split("/")
    if len(segments) > 2:
        segments = segments[:2] + [_MASK]
    return authority + "/".join(segments) + _redact_query(query)


def _redact_string(value: str) -> str:
    if _AUTH_VALUE.fullmatch(value):
        return _MASK
    inline_flag = _INLINE_FLAG.fullmatch(value)
    if inline_flag:
        return inline_flag.group(1) + "=" + _MASK
    result = _AUTH_HEADER.sub(lambda match: match.group(1) + _MASK, value)
    result = _COOKIE_HEADER.sub(lambda match: match.group(1) + _MASK, result)
    result = _RTMP_URL.sub(_redact_rtmp, result)
    return _redact_query(result)


def _redact_sequence(value: list[object] | tuple[object, ...]) -> list[object] | tuple[object, ...]:
    redacted: list[object] = []
    mask_next = False
    for item in value:
        if mask_next:
            redacted.append(_MASK)
            mask_next = False
            continue
        redacted_item = redact(item)
        redacted.append(redacted_item)
        if isinstance(item, str) and item.lower() in _SENSITIVE_FLAGS:
            mask_next = True
    return tuple(redacted) if isinstance(value, tuple) else redacted


def redact(value: object) -> object:
    """Return a recursively redacted copy suitable for diagnostic logging."""
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Mapping):
        return {
            key: _MASK if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return _redact_sequence(value)
    return value
