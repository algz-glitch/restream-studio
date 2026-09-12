"""FastAPI application factory and localhost-only process entry point."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from restream_studio.api.routes import ApiDependencies, ApiError, install_routes
from restream_studio.config import AppPaths
from restream_studio.persistence.database import Database
from restream_studio.runtime import RuntimeManager


def _default_dependencies() -> ApiDependencies:
    paths = AppPaths.create(Path.cwd() / "runtime")
    database = Database(paths.database_file, paths=paths)
    runtime = RuntimeManager(database)
    return ApiDependencies(
        database,
        runtime,
        reconnect_destination=runtime.reconnect_destination,
        test_destination=runtime.test_destination,
    )


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", uuid4().hex))


def _error(request: Request, status: int, code: str, message: str, fields: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "fields": fields or {},
                "request_id": _request_id(request),
            }
        },
    )


def _safe_fields(error: RequestValidationError) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"] if part not in {"body", "query", "path", "header"}) or "request"
        fields[location] = str(item["type"])
    return fields


def _host_is_local(value: str) -> bool:
    if not value or any(character in value for character in "\r\n/\\@"):
        return False
    candidate = value
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return False
        candidate = value[1:end]
        suffix = value[end + 1 :]
        if suffix and (not suffix.startswith(":") or not suffix[1:].isdigit()):
            return False
    elif value.count(":") == 1:
        candidate, port = value.rsplit(":", 1)
        if not port.isdigit():
            return False
    return candidate.casefold() in {"localhost", "127.0.0.1", "::1"}


def _origin_matches_host(origin: str, host: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and parsed.netloc.casefold() == host.casefold()
        and _host_is_local(parsed.netloc)
    )


def mutation_is_authorized(
    origin: str, host: str, provided_token: str, session_token: str
) -> bool:
    """Fail closed before lifespan while comparing token values in constant time."""
    tokens_match = secrets.compare_digest(provided_token, session_token)
    return (
        bool(provided_token)
        and bool(session_token)
        and tokens_match
        and _origin_matches_host(origin, host)
    )


def create_app(factory: Callable[[], ApiDependencies] = _default_dependencies) -> FastAPI:
    deps = factory()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.session_token = secrets.token_urlsafe(32)
        application.state.dependencies = deps
        deps.database.open()
        try:
            await deps.controller.initialize()
            yield
        finally:
            try:
                await deps.controller.stop()
            finally:
                deps.controller.clear()
                application.state.session_token = ""
                application.state.dependencies = None
                deps.database.close()

    application = FastAPI(
        title="Restream Studio Local API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    application.state.session_token = ""
    application.state.dependencies = deps

    @application.middleware("http")
    async def local_security(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = uuid4().hex
        host = request.headers.get("host", "")
        proxy_headers = {"forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"}
        if not _host_is_local(host) or proxy_headers.intersection(request.headers):
            return _error(request, 421, "local_host_required", "Only direct localhost requests are accepted")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin", "")
            token = request.headers.get("x-restream-session", "")
            if not mutation_is_authorized(origin, host, token, application.state.session_token):
                return _error(request, 403, "request_forbidden", "Origin or session token is invalid")
        if (
            request.method in {"GET", "HEAD"}
            and request.url.path == "/api/session"
            and not _origin_matches_host(request.headers.get("origin", ""), host)
        ):
            return _error(request, 403, "request_forbidden", "Origin is invalid")
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return _error(request, error.status, error.code, error.message, error.fields)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return _error(request, 422, "validation_error", "Request validation failed", _safe_fields(error))

    @application.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if error.status_code == 404 else "http_error"
        message = "Resource not found" if error.status_code == 404 else "Request was rejected"
        return _error(request, error.status_code, code, message)

    @application.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        del error
        return _error(request, 500, "internal_error", "Request could not be completed")

    application.include_router(install_routes(deps))
    assets = deps.assets_dir
    if assets is not None and Path(assets).is_dir():
        application.mount("/", StaticFiles(directory=assets, html=True), name="frontend")
    return application


app = create_app()


def run() -> None:
    uvicorn.run("restream_studio.main:app", host="127.0.0.1", port=8000, proxy_headers=False)


if __name__ == "__main__":
    run()
