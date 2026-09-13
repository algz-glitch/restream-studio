"""Dependency-injected local control routes."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from fastapi import APIRouter, Header, Query, Request, Response

from restream_studio import __version__
from restream_studio.domain import DestinationKind
from restream_studio.orchestration.controller import ControllerSnapshot
from restream_studio.persistence.database import (
    DestinationConfig,
    EventRecord,
    RevisionConflictError,
    RuntimeDestination,
    SourceConfig,
)
from restream_studio.security.redaction import redact
from restream_studio.update.service import UpdateOperationError, UpdateSnapshot

from .schemas import (
    ControlResponse,
    DestinationName,
    DestinationResponse,
    DestinationUpdate,
    EmptyRequest,
    EventResponse,
    EventsResponse,
    InstallResponse,
    ReconnectResponse,
    SessionResponse,
    SourceResponse,
    SourceUpdate,
    StartRequest,
    StatusOutput,
    StatusResponse,
    TestResponse,
    UpdateResponse,
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
    def get_source_with_revision(self) -> tuple[SourceConfig | None, int]: ...
    def set_source(self, room_identity: str, preferred_quality: str | None, desired_running: bool) -> None: ...
    def delete_source(self) -> bool: ...
    def get_destination(self, kind: DestinationKind) -> DestinationConfig | None: ...
    def get_destination_runtime(self, kind: DestinationKind) -> RuntimeDestination | None: ...
    def get_destination_with_revision(
        self, kind: DestinationKind
    ) -> tuple[DestinationConfig | None, RuntimeDestination | None, int]: ...
    def configuration_snapshot(self) -> tuple[SourceConfig | None, list[DestinationConfig]]: ...
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
    def delete_destination(self, kind: DestinationKind) -> bool: ...
    def get_api_revision(self, resource: str) -> int: ...
    def set_api_revision(self, resource: str, revision: int) -> None: ...
    def set_source_revisioned(
        self,
        room_identity: str,
        preferred_quality: str | None,
        desired_running: bool,
        *,
        expected_revision: int,
    ) -> int: ...
    def set_destination_revisioned(
        self,
        kind: DestinationKind,
        base_server: str,
        stream_key: str,
        *,
        enabled: bool,
        expected_revision: int,
    ) -> int: ...


class ControllerPort(Protocol):
    async def initialize(self) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def shutdown(self) -> None: ...
    async def snapshot(self) -> ControllerSnapshot: ...
    async def set_destination_enabled(self, identity: str, enabled: bool) -> None: ...
    async def apply_configuration(self) -> None: ...
    def clear(self) -> None: ...


class UpdateServicePort(Protocol):
    def snapshot(self) -> UpdateSnapshot: ...
    async def check(self) -> UpdateSnapshot: ...
    async def download(self) -> UpdateSnapshot: ...
    async def install(self, *, current_pid: int) -> None: ...
    def start_background(self) -> None: ...
    async def shutdown(self) -> None: ...


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
    update_service: UpdateServicePort | None = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


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


def _required_revision(if_match: str | None) -> int:
    revision = _revision(if_match)
    if revision is None:
        raise ApiError(428, "precondition_required", "If-Match revision is required")
    return revision


def start_precondition_error(
    *, source_configured: bool, real_ready: bool, local_ready: bool
) -> tuple[str, str] | None:
    """Return the stable first unmet start prerequisite, in API contract order."""
    if not real_ready and not local_ready:
        return "destination_required", "An enabled configured destination is required"
    if not source_configured:
        return "source_required", "A source configuration is required"
    return None


async def _destination_response(
    deps: ApiDependencies,
    kind: DestinationKind,
    config: DestinationConfig | None,
) -> DestinationResponse:
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
        return {
            "status": "ok",
            "app": "restream-studio",
            "version": __version__,
        }

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

    @router.get("/api/session", response_model=SessionResponse)
    async def session(request: Request) -> SessionResponse:
        token = str(getattr(request.app.state, "session_token", ""))
        if not token:
            raise ApiError(503, "session_unavailable", "Session is not initialized")
        return SessionResponse(session_token=token)

    def update_service() -> UpdateServicePort:
        if deps.update_service is None:
            raise ApiError(503, "update_unavailable", "Update service is unavailable")
        return deps.update_service

    def update_response(value: UpdateSnapshot) -> UpdateResponse:
        return UpdateResponse(
            status=value.status.value,
            current_version=value.current_version,
            available_version=value.available_version,
            release_url=value.release_url,
            last_checked_at=value.last_checked_at,
            error_code=value.error_code,
            error_message=value.error_message,
        )

    @router.get("/api/update", response_model=UpdateResponse)
    async def get_update() -> UpdateResponse:
        return update_response(update_service().snapshot())

    @router.post("/api/update/check", response_model=UpdateResponse)
    async def check_update(value: EmptyRequest) -> UpdateResponse:
        del value
        try:
            state = await update_service().check()
        except UpdateOperationError as error:
            raise ApiError(409, error.code, error.message) from error
        return update_response(state)

    @router.post("/api/update/download", response_model=UpdateResponse)
    async def download_update(value: EmptyRequest) -> UpdateResponse:
        del value
        try:
            state = await update_service().download()
        except UpdateOperationError as error:
            raise ApiError(409, error.code, error.message) from error
        return update_response(state)

    @router.post("/api/update/install", response_model=InstallResponse)
    async def install_update(value: EmptyRequest) -> InstallResponse:
        del value
        snapshot = await deps.controller.snapshot()
        if snapshot.desired_running:
            raise ApiError(
                409,
                "relay_must_be_stopped",
                "Stop all outputs before installing",
            )
        try:
            await update_service().install(current_pid=os.getpid())
        except UpdateOperationError as error:
            raise ApiError(409, error.code, error.message) from error
        return InstallResponse(status="restart_scheduled")

    @router.get("/api/source", response_model=SourceResponse)
    async def get_source(response: Response) -> SourceResponse:
        source, revision = deps.database.get_source_with_revision()
        response.headers["ETag"] = f'"{revision}"'
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
        resource = "source"
        async with deps.write_lock:
            expected = _required_revision(if_match)
            existing, revision = deps.database.get_source_with_revision()
            previous_revision = revision
            unchanged = existing is not None and (
                existing.room_identity,
                existing.preferred_quality,
            ) == (value.room_url, value.preferred_quality)
            if expected != revision and not unchanged:
                raise ApiError(409, "write_conflict", "Configuration changed; reload and retry")
            if not unchanged:
                try:
                    revision = deps.database.set_source_revisioned(
                        value.room_url,
                        value.preferred_quality,
                        existing.desired_running if existing else False,
                        expected_revision=revision,
                    )
                except RevisionConflictError as exc:
                    raise ApiError(409, "write_conflict", "Configuration changed; reload and retry") from exc
                try:
                    await deps.controller.apply_configuration()
                except Exception as exc:
                    if existing is None:
                        deps.database.delete_source()
                    else:
                        deps.database.set_source(
                            existing.room_identity,
                            existing.preferred_quality,
                            existing.desired_running,
                        )
                    deps.database.set_api_revision(resource, previous_revision)
                    await deps.controller.apply_configuration()
                    raise ApiError(500, "configuration_apply_failed", "Configuration was not applied") from exc
            response.headers["ETag"] = f'"{revision}"'
            source, stored_revision = deps.database.get_source_with_revision()
            if stored_revision != revision:
                raise ApiError(500, "configuration_apply_failed", "Configuration was not applied")
        if source is None:
            raise ApiError(500, "internal_error", "Request could not be completed")
        return SourceResponse(configured=True, room_identity=source.room_identity, preferred_quality=source.preferred_quality)

    @router.get("/api/destinations/{kind}", response_model=DestinationResponse)
    async def get_destination(kind: str, response: Response) -> DestinationResponse:
        selected = _kind(kind)
        config, _runtime, revision = deps.database.get_destination_with_revision(selected)
        response.headers["ETag"] = f'"{revision}"'
        return await _destination_response(deps, selected, config)

    @router.put("/api/destinations/{kind}", response_model=DestinationResponse)
    async def put_destination(
        kind: str,
        value: DestinationUpdate,
        response: Response,
        if_match: str | None = Header(default=None, max_length=24),
    ) -> DestinationResponse:
        selected = _kind(kind)
        resource = f"destination:{kind}"
        async with deps.write_lock:
            current, runtime, revision = deps.database.get_destination_with_revision(selected)
            server = value.base_server or (runtime.base_server if runtime else None)
            key = value.stream_key.get_secret_value() if value.stream_key else (runtime.stream_key if runtime else None)
            enabled = value.enabled if value.enabled is not None else (current.enabled if current else True)
            if server is None or key is None:
                raise ApiError(
                    422,
                    "validation_error",
                    "Destination configuration is incomplete",
                    {"body": "base_server and stream_key are required"},
                )
            previous_revision = revision
            unchanged = current is not None and runtime is not None and (
                runtime.base_server,
                runtime.stream_key,
                current.enabled,
            ) == (server, key, enabled)
            expected = _required_revision(if_match)
            if expected != revision and not unchanged:
                raise ApiError(409, "write_conflict", "Configuration changed; reload and retry")
            if not unchanged:
                try:
                    revision = deps.database.set_destination_revisioned(
                        selected,
                        server,
                        key,
                        enabled=enabled,
                        expected_revision=revision,
                    )
                except RevisionConflictError as exc:
                    raise ApiError(409, "write_conflict", "Configuration changed; reload and retry") from exc
                try:
                    await deps.controller.apply_configuration()
                except Exception as exc:
                    if current is None or runtime is None:
                        deps.database.delete_destination(selected)
                    else:
                        deps.database.set_destination(
                            selected,
                            runtime.base_server,
                            runtime.stream_key,
                            enabled=current.enabled,
                            controller_identity=current.controller_identity,
                        )
                    deps.database.set_api_revision(resource, previous_revision)
                    await deps.controller.apply_configuration()
                    raise ApiError(500, "configuration_apply_failed", "Configuration was not applied") from exc
            response.headers["ETag"] = f'"{revision}"'
            stored, _stored_runtime, stored_revision = deps.database.get_destination_with_revision(
                selected
            )
            if stored_revision != revision:
                raise ApiError(500, "configuration_apply_failed", "Configuration was not applied")
        return await _destination_response(deps, selected, stored)

    @router.post("/api/control/start", response_model=ControlResponse)
    async def start(value: StartRequest) -> ControlResponse:
        async with deps.write_lock:
            source, destinations = deps.database.configuration_snapshot()
            configured = {
                item.kind: item for item in destinations if item.configured and item.enabled
            }
            real_ready = bool(
                {DestinationKind.DOUYIN, DestinationKind.WECHAT} & configured.keys()
            )
            local_ready = (
                DestinationKind.LOCAL_TEST in configured
                and deps.local_test_mode
                and value.local_test
            )
            if value.local_test and not deps.local_test_mode:
                raise ApiError(409, "local_test_disabled", "Local test mode is not enabled")
            precondition = start_precondition_error(
                source_configured=source is not None,
                real_ready=real_ready,
                local_ready=local_ready,
            )
            if precondition is not None:
                raise ApiError(409, precondition[0], precondition[1])
            await deps.controller.start()
        return ControlResponse(status="started")

    @router.post("/api/control/stop", response_model=ControlResponse)
    async def stop(value: EmptyRequest) -> ControlResponse:
        del value
        async with deps.write_lock:
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

    @router.api_route(
        "/api/{unmatched:path}",
        methods=[
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "HEAD",
            "OPTIONS",
            "TRACE",
            "CONNECT",
        ],
        include_in_schema=False,
    )
    async def api_not_found(unmatched: str, request: Request) -> None:
        del unmatched, request
        raise ApiError(404, "not_found", "Resource not found")

    return router
