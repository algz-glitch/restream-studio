"""Strict, secret-safe API contracts."""

from __future__ import annotations

import re
from ipaddress import ip_address
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictStr,
    field_validator,
)

from restream_studio.source import DouyinUrlValidationError, normalize_douyin_url

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_QUALITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_IPV4_LIKE = re.compile(r"^[0-9.]+$")
DestinationName = Literal["douyin", "wechat_channels", "local_test"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmptyRequest(StrictModel):
    pass


class StartRequest(StrictModel):
    local_test: StrictBool = False


class SourceUpdate(StrictModel):
    room_url: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    preferred_quality: Annotated[StrictStr, Field(min_length=1, max_length=32)] | None = None

    @field_validator("room_url")
    @classmethod
    def canonical_room_url(cls, value: str) -> str:
        try:
            return normalize_douyin_url(value)
        except DouyinUrlValidationError as exc:
            raise ValueError("unsupported Douyin room URL") from exc

    @field_validator("preferred_quality")
    @classmethod
    def safe_quality(cls, value: str | None) -> str | None:
        if value is not None and _QUALITY.fullmatch(value) is None:
            raise ValueError("quality contains unsupported characters")
        return value


class DestinationUpdate(StrictModel):
    base_server: Annotated[StrictStr, Field(min_length=1, max_length=256)] | None = None
    stream_key: Annotated[SecretStr, Field(min_length=8, max_length=512)] | None = None
    enabled: StrictBool = True

    @field_validator("base_server")
    @classmethod
    def safe_server(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or _CONTROL.search(value) or any(char.isspace() for char in value):
            raise ValueError("server contains unsupported characters")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("server is malformed") from exc
        hostname = parsed.hostname
        if (
            parsed.scheme.lower() not in {"rtmp", "rtmps"}
            or hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port not in {None, 1935, 443}
            or len([segment for segment in parsed.path.split("/") if segment]) > 1
            or ".." in parsed.path.split("/")
        ):
            raise ValueError("server is not an approved RTMP endpoint")
        try:
            address = ip_address(hostname)
        except ValueError:
            if (
                len(hostname) > 253
                or not hostname.isascii()
                or _IPV4_LIKE.fullmatch(hostname) is not None
                or any(_HOST_LABEL.fullmatch(label) is None for label in hostname.split("."))
            ):
                raise ValueError("server hostname is invalid") from None
        else:
            if not address.is_global:
                raise ValueError("server address is not globally routable")
        return value

    @field_validator("stream_key")
    @classmethod
    def safe_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            secret = value.get_secret_value()
            if secret != secret.strip() or _CONTROL.search(secret):
                raise ValueError("stream key contains unsupported characters")
        return value


class SourceResponse(StrictModel):
    configured: bool
    room_identity: str | None
    preferred_quality: str | None


class DestinationResponse(StrictModel):
    kind: DestinationName
    configured: bool
    masked_stream_key: Literal["********"] | None
    enabled: bool
    status: str


class ControlResponse(StrictModel):
    status: Literal["started", "stopped"]


class ReconnectResponse(StrictModel):
    kind: DestinationName
    status: Literal["reconnect_requested"]


class TestResponse(StrictModel):
    kind: DestinationName
    ok: bool
    diagnostic: Literal["connection_valid", "connection_failed"]


class EventResponse(StrictModel):
    id: int
    created_at: str
    level: str
    event_type: str
    payload: dict[str, object]


class EventsResponse(StrictModel):
    items: list[EventResponse]
    next_cursor: int | None


class StatusOutput(StrictModel):
    kind: str
    enabled: bool
    status: str
    input: str
    last_error: str | None


class StatusResponse(StrictModel):
    desired_running: bool
    source_state: str
    source_failure: str | None
    outputs: list[StatusOutput]
