"""Dependency-injected local control routes."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from fastapi import APIRouter, Header, Query, Request, Response

from restream_studio.domain import DestinationKind
from restream_studio.orchestration.controller import ControllerSnapshot
from restream_studio.persistence.database import (
    DestinationConfig,
    EventRecord,
    RuntimeDestination,
    SourceConfig,
)
from restream_studio.security.redaction import redact

from .schemas import (
    ControlResponse,
    DestinationName,
    DestinationResponse,
    DestinationUpdate,
    EmptyRequest,
    EventResponse,
    EventsResponse,
    ReconnectResponse,
    SourceResponse,
    SourceUpdate,
    StartRequest,
    StatusOutput,
    StatusResponse,
    TestResponse,
)

_KINDS = {
    "douyin": DestinationKind.DOUYIN,
    "wechat_channels": DestinationKind.WECHAT,
    "local_test": DestinationKind.LOCAL_TEST,
}
_NAMES: dict[DestinationKind, DestinationName] = {
    DestinationKind.DOUYIN: "douyin",
    DestinationKind.WECHAT: "wechat_channels",
    DestinationKind.LOCAL_TEST: "local_test",
}
_MASK: Final = "********"


class DatabasePort(Protocol):
    def open(self) -> object: ...
    def close(self) -> None: ...
    def get_source(self) -> SourceConfig | None: ...
    def set_source(self, room_identity: str, preferred_quality: str | None, desired_running: bool) -> None: ...
    def get_destination(self, kind: DestinationKind) -> DestinationConfig | None: ...
    def get_destination_runtime(self, kind: DestinationKind) -> RuntimeDestination | None: ...
    def set_destination(
        self,
        kind: DestinationKind,
        base_server: str,
        stream_key: str,
        *,
        enabled: bool = True,
        controller_identity: str | None = None,
    ) -> None: ...
    def list_destinations(self) -> list[DestinationConfig]: ...
    def list_events(self, *, limit: int = 100) -> list[EventRecord]: ...


class ControllerPort(Protocol):
    async def initialize(self) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def snapshot(self) -> ControllerSnapshot: ...
    async def set_destination_enabled(self, identity: str, enabled: bool) -> None: ...


DestinationAction = Callable[[DestinationKind], Awaitable[None]]
DestinationTest = Callable[[DestinationKind, str, str], Awaitable[bool]]


async def _unavailable_action(kind: DestinationKind) -> None:
    del kind
    raise RuntimeError("adapter unavailable")


async def _unavailable_test(kind: DestinationKind, server: str, key: str) -> bool:
    del kind, server, key
    return False


@dataclass(slots=True)
class ApiDependencies:
    database: DatabasePort
    controller: ControllerPort
    reconnect_destination: DestinationAction = _unavailable_action
    test_destination: DestinationTest = _unavailable_test
    local_test_mode: bool = False
    assets_dir: Path | None = None
    write_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    revisions: dict[str, int] = field(default_factory=dict, repr=False)
    fingerprints: dict[str, str] = field(default_factory=dict, repr=False)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, fields: dict[str, str] | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields or {}


def _kind(raw: str) -> DestinationKind:
    try:
        return _KINDS[raw]
    except KeyError as exc:
        raise ApiError(404, "not_found", "Resource not found") from exc


def _revision(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    if len(if_match) > 22 or not (if_match.startswith('"') and if_match.endswith('"')):
        raise ApiError(400, "invalid_precondition", "If-Match must be a quoted revision")
    raw = if_match[1:-1]
    if not raw.isascii() or not raw.isdecimal():
        raise ApiError(400, "invalid_precondition", "If-Match must be a quoted revision")
    return int(raw)


def _fingerprint(*values: object) -> str:
    encoded = "\x1f".join(str(value) for value in values).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _check_write(deps: ApiDependencies, resource: str, expected: int | None, fingerprint: str) -> bool:
    current = deps.revisions.get(resource, 0)
    if expected is not None and expected != current:
        if deps.fingerprints.get(resource) == fingerprint:
            return False
        raise ApiError(409, "write_conflict", "Configuration changed; reload and retry")
    return deps.fingerprints.get(resource) != fingerprint


async def _destination_response(
    deps: ApiDependencies, kind: DestinationKind
) -> DestinationResponse:
    config = deps.database.get_destination(kind)
    statuses = {item.destination: item.state.value for item in (await deps.controller.snapshot()).outputs}
    return DestinationResponse(
        kind=_NAMES[kind],
        configured=config is not None and config.configured,
        masked_stream_key=_MASK if config is not None and config.configured else None,
        enabled=config.enabled if config is not None else False,
        status=statuses.get(kind, "STOPPED"),
    )


def install_routes(deps: ApiDependencies) -> APIRouter:
    router = APIRouter()

    @router.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/api/status", response_model=StatusResponse)
    async def status() -> StatusResponse:
        snapshot = await deps.controller.snapshot()
        return StatusResponse(
            desired_running=snapshot.desired_running,
            source_state=snapshot.source_state.value,
            source_failure=snapshot.source_failure.value if snapshot.source_failure else None,
            outputs=[
                StatusOutput(
                    kind=_NAMES[item.destination],
                    enabled=item.enabled,
                    status=item.state.value,
                    input=item.input.value,
                    last_error=item.last_error.value if item.last_error else None,
                )
                for item in snapshot.outputs
            ],
        )

    @router.get("/api/source", response_model=SourceResponse)
    async def get_source() -> SourceResponse:
        source = deps.database.get_source()
        return SourceResponse(
            configured=source is not None,
            room_identity=source.room_identity if source else None,
            preferred_quality=source.preferred_quality if source else None,
        )

    @router.put("/api/source", response_model=SourceResponse)
    async def put_source(
        value: SourceUpdate,
        response: Response,
        if_match: str | None = Header(default=None, max_length=24),
    ) -> SourceResponse:
        fingerprint = _fingerprint(value.room_url, value.preferred_quality)
        resource = "source"
        with deps.write_lock:
            expected = _revision(if_match)
            if _check_write(deps, resource, expected, fingerprint):
                existing = deps.database.get_source()
                deps.database.set_source(
                    value.room_url,
                    value.preferred_quality,
                    existing.desired_running if existing else False,
                )
                deps.revisions[resource] = deps.revisions.get(resource, 0) + 1
                deps.fingerprints[resource] = fingerprint
            response.headers["ETag"] = f'"{deps.revisions.get(resource, 0)}"'
            source = deps.database.get_source()
        if source is None:
            raise ApiError(500, "internal_error", "Request could not be completed")
        return SourceResponse(configured=True, room_identity=source.room_identity, preferred_quality=source.preferred_quality)

    @router.get("/api/destinations/{kind}", response_model=DestinationResponse)
    async def get_destination(kind: str) -> DestinationResponse:
        return await _destination_response(deps, _kind(kind))

    @router.put("/api/destinations/{kind}", response_model=DestinationResponse)
    async def put_destination(
        kind: str,
        value: DestinationUpdate,
        response: Response,
        if_match: str | None = Header(default=None, max_length=24),
    ) -> DestinationResponse:
        selected = _kind(kind)
        resource = f"destination:{kind}"
        with deps.write_lock:
            current = deps.database.get_destination(selected)
            runtime = deps.database.get_destination_runtime(selected) if current else None
            server = value.base_server or (runtime.base_server if runtime else None)
            key = value.stream_key.get_secret_value() if value.stream_key else (runtime.stream_key if runtime else None)
            if server is None or key is None:
                raise ApiError(
                    422,
                    "validation_error",
                    "Destination configuration is incomplete",
                    {"body": "base_server and stream_key are required"},
                )
            fingerprint = _fingerprint(server, key, value.enabled)
            expected = _revision(if_match)
            if _check_write(deps, resource, expected, fingerprint):
                deps.database.set_destination(selected, server, key, enabled=value.enabled)
                deps.revisions[resource] = deps.revisions.get(resource, 0) + 1
                deps.fingerprints[resource] = fingerprint
            response.headers["ETag"] = f'"{deps.revisions.get(resource, 0)}"'
        snapshot = await deps.controller.snapshot()
        identity = next((item.identity for item in snapshot.outputs if item.destination is selected), None)
        if identity is not None:
            await deps.controller.set_destination_enabled(identity, value.enabled)
        return await _destination_response(deps, selected)

    @router.post("/api/control/start", response_model=ControlResponse)
    async def start(value: StartRequest) -> ControlResponse:
        if deps.database.get_source() is None:
            raise ApiError(409, "source_required", "A source configuration is required")
        configured = {item.kind: item for item in deps.database.list_destinations() if item.configured and item.enabled}
        real_ready = bool({DestinationKind.DOUYIN, DestinationKind.WECHAT} & configured.keys())
        local_ready = DestinationKind.LOCAL_TEST in configured and deps.local_test_mode and value.local_test
        if value.local_test and not deps.local_test_mode:
            raise ApiError(409, "local_test_disabled", "Local test mode is not enabled")
        if not real_ready and not local_ready:
            raise ApiError(409, "destination_required", "An enabled configured destination is required")
        await deps.controller.start()
        return ControlResponse(status="started")

    @router.post("/api/control/stop", response_model=ControlResponse)
    async def stop(value: EmptyRequest) -> ControlResponse:
        del value
        await deps.controller.stop()
        return ControlResponse(status="stopped")

    @router.post("/api/destinations/{kind}/reconnect", response_model=ReconnectResponse)
    async def reconnect(kind: str, value: EmptyRequest) -> ReconnectResponse:
        del value
        selected = _kind(kind)
        if deps.database.get_destination(selected) is None:
            raise ApiError(409, "destination_unconfigured", "Destination is not configured")
        try:
            await deps.reconnect_destination(selected)
        except Exception as exc:
            raise ApiError(503, "reconnect_failed", "Reconnect request failed") from exc
        return ReconnectResponse(kind=_NAMES[selected], status="reconnect_requested")

    @router.post("/api/destinations/{kind}/test", response_model=TestResponse)
    async def test_destination(kind: str, value: EmptyRequest) -> TestResponse:
        del value
        selected = _kind(kind)
        if deps.database.get_destination(selected) is None:
            raise ApiError(409, "destination_unconfigured", "Destination is not configured")
        runtime = deps.database.get_destination_runtime(selected)
        if runtime is None:
            raise ApiError(409, "destination_unconfigured", "Destination is not configured")
        try:
            ok = await asyncio.wait_for(
                deps.test_destination(selected, runtime.base_server, runtime.stream_key), timeout=5.0
            )
        except Exception:  # noqa: BLE001 - adapter details never cross the API boundary
            ok = False
        return TestResponse(
            kind=_NAMES[selected],
            ok=ok,
            diagnostic="connection_valid" if ok else "connection_failed",
        )

    @router.get("/api/events", response_model=EventsResponse)
    async def events(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: int = Query(default=0, ge=0, le=2_147_483_647),
    ) -> EventsResponse:
        if set(request.query_params) - {"limit", "cursor"}:
            raise ApiError(400, "invalid_query", "Unsupported query parameter")
        records = [item for item in deps.database.list_events(limit=10_000) if item.id > cursor][:limit]
        items: list[EventResponse] = []
        for item in records:
            safe_payload = redact(item.payload)
            items.append(
                EventResponse(
                    id=item.id,
                    created_at=item.created_at,
                    level=item.level,
                    event_type=item.event_type,
                    payload=safe_payload if isinstance(safe_payload, dict) else {},
                )
            )
        return EventsResponse(items=items, next_cursor=items[-1].id if items else None)

    @router.api_route("/api/{unmatched:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def api_not_found(unmatched: str, request: Request) -> None:
        del unmatched, request
        raise ApiError(404, "not_found", "Resource not found")

    return router
