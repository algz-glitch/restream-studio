from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr

from restream_studio.api.routes import ApiDependencies, start_precondition_error
from restream_studio.domain import DestinationKind, OutputState, SourceState
from restream_studio.main import create_app, mutation_is_authorized
from restream_studio.orchestration.controller import (
    ControllerOutputSnapshot,
    ControllerSnapshot,
    OutputInput,
)
from restream_studio.persistence.database import DestinationConfig, EventRecord, SourceConfig

CANONICAL = "https://live.douyin.com/123456"
SECRET = "never-print-this-stream-key"
MUTATING = {"host": "localhost", "origin": "http://localhost"}


class FakeDatabase:
    def __init__(self) -> None:
        self.source: SourceConfig | None = None
        self.destinations: dict[DestinationKind, dict[str, object]] = {}
        self.events: list[EventRecord] = []
        self.opened = False
        self.closed = False
        self.revisions: dict[str, int] = {}

    def open(self) -> FakeDatabase:
        self.opened = True
        return self

    def close(self) -> None:
        self.closed = True

    def get_source(self) -> SourceConfig | None:
        return self.source

    def get_source_with_revision(self) -> tuple[SourceConfig | None, int]:
        return self.source, self.get_api_revision("source")

    def set_source(self, room_identity: str, preferred_quality: str | None, desired_running: bool) -> None:
        from restream_studio.source import normalize_douyin_url

        self.source = SourceConfig(normalize_douyin_url(room_identity), preferred_quality, desired_running)

    def delete_source(self) -> bool:
        existed = self.source is not None
        self.source = None
        return existed

    def get_destination(self, kind: DestinationKind) -> DestinationConfig | None:
        item = self.destinations.get(kind)
        if item is None:
            return None
        return DestinationConfig(kind, kind.name.lower(), "rtmps://publish.invalid/live", bool(item["enabled"]), True)

    def get_destination_runtime(self, kind: DestinationKind) -> Any:
        item = self.destinations[kind]
        return type("Runtime", (), {"base_server": item["base_server"], "stream_key": item["stream_key"]})()

    def get_destination_with_revision(
        self, kind: DestinationKind
    ) -> tuple[DestinationConfig | None, Any, int]:
        resource = f"destination:{kind.name.lower() if kind is not DestinationKind.WECHAT else 'wechat_channels'}"
        public = self.get_destination(kind)
        runtime = self.get_destination_runtime(kind) if public is not None else None
        return public, runtime, self.get_api_revision(resource)

    def configuration_snapshot(self) -> tuple[SourceConfig | None, list[DestinationConfig]]:
        return self.source, self.list_destinations()

    def set_destination(
        self,
        kind: DestinationKind,
        base_server: str,
        stream_key: str,
        *,
        enabled: bool = True,
        controller_identity: str | None = None,
    ) -> None:
        self.destinations[kind] = {
            "base_server": base_server,
            "stream_key": stream_key,
            "enabled": enabled,
        }

    def list_destinations(self) -> list[DestinationConfig]:
        return [item for kind in self.destinations if (item := self.get_destination(kind))]

    def list_events(self, *, limit: int = 100) -> list[EventRecord]:
        return self.events[:limit]

    def delete_destination(self, kind: DestinationKind) -> bool:
        return self.destinations.pop(kind, None) is not None

    def get_api_revision(self, resource: str) -> int:
        return self.revisions.get(resource, 0)

    def set_api_revision(self, resource: str, revision: int) -> None:
        self.revisions[resource] = revision

    def set_source_revisioned(
        self,
        room_identity: str,
        preferred_quality: str | None,
        desired_running: bool,
        *,
        expected_revision: int,
    ) -> int:
        assert self.get_api_revision("source") == expected_revision
        self.set_source(room_identity, preferred_quality, desired_running)
        revision = expected_revision + 1
        self.set_api_revision("source", revision)
        return revision

    def set_destination_revisioned(
        self,
        kind: DestinationKind,
        base_server: str,
        stream_key: str,
        *,
        enabled: bool,
        expected_revision: int,
    ) -> int:
        resource = f"destination:{kind.name.lower() if kind is not DestinationKind.WECHAT else 'wechat_channels'}"
        assert self.get_api_revision(resource) == expected_revision
        self.set_destination(kind, base_server, stream_key, enabled=enabled)
        revision = expected_revision + 1
        self.set_api_revision(resource, revision)
        return revision


class FakeController:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.initialized = 0
        self.enabled_calls: list[tuple[str, bool]] = []
        self.applied = 0

    async def initialize(self) -> None:
        self.initialized += 1

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def shutdown(self) -> None:
        self.stopped += 1

    async def snapshot(self) -> ControllerSnapshot:
        return ControllerSnapshot(
            CANONICAL,
            self.started > self.stopped,
            SourceState.MONITORING if self.started > self.stopped else SourceState.STOPPED,
            None,
            None,
            0,
            (
                ControllerOutputSnapshot(
                    "douyin", DestinationKind.DOUYIN, True, OutputState.STOPPED, OutputInput.NONE, None
                ),
                ControllerOutputSnapshot(
                    "wechat_channels",
                    DestinationKind.WECHAT,
                    True,
                    OutputState.STOPPED,
                    OutputInput.NONE,
                    None,
                ),
                ControllerOutputSnapshot(
                    "local_test",
                    DestinationKind.LOCAL_TEST,
                    True,
                    OutputState.STOPPED,
                    OutputInput.NONE,
                    None,
                ),
            ),
        )

    async def set_destination_enabled(self, identity: str, enabled: bool) -> None:
        self.enabled_calls.append((identity, enabled))

    async def apply_configuration(self) -> None:
        self.applied += 1

    def clear(self) -> None:
        return None


@dataclass
class Harness:
    db: FakeDatabase
    controller: FakeController
    reconnects: list[DestinationKind]
    tests: list[tuple[DestinationKind, str, str]]


@pytest.fixture
def harness() -> Harness:
    return Harness(FakeDatabase(), FakeController(), [], [])


@pytest.fixture
def app(harness: Harness) -> FastAPI:
    async def reconnect(kind: DestinationKind) -> None:
        harness.reconnects.append(kind)

    async def test_destination(kind: DestinationKind, server: str, key: str) -> bool:
        harness.tests.append((kind, server, key))
        return True

    def factory() -> ApiDependencies:
        return ApiDependencies(
            database=harness.db,
            controller=harness.controller,
            reconnect_destination=reconnect,
            test_destination=test_destination,
            local_test_mode=False,
        )

    return create_app(factory)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, headers={"host": "localhost"}) as test_client:
        test_client.headers.update(
            {
                "origin": "http://localhost",
                "x-restream-session": app.state.session_token,
                "if-match": '"0"',
            }
        )
        yield test_client


def test_health_is_minimal_and_status_is_safe(client: TestClient) -> None:
    assert client.get("/health").json() == {
        "status": "ok",
        "app": "restream-studio",
        "version": "0.1.2",
    }
    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["source_state"] == "STOPPED"
    assert SECRET not in response.text


def test_source_round_trip_normalizes_identity_and_forbids_extra(client: TestClient, harness: Harness) -> None:
    response = client.put(
        "/api/source",
        json={"room_url": f" {CANONICAL}?utm_source=x ", "preferred_quality": "origin"},
    )
    assert response.status_code == 200
    assert response.json() == {"configured": True, "room_identity": CANONICAL, "preferred_quality": "origin"}
    assert harness.db.source is not None and "?" not in harness.db.source.room_identity
    invalid = client.put("/api/source", json={"room_url": CANONICAL, "unexpected": True})
    assert invalid.status_code == 422
    assert set(invalid.json()["error"]) == {"code", "message", "fields", "request_id"}


def test_get_source_etag_comes_from_atomic_database_snapshot(
    client: TestClient, harness: Harness
) -> None:
    harness.db.source = SourceConfig(CANONICAL, "origin", False)
    harness.db.revisions["source"] = 7
    harness.db.get_source = lambda: (_ for _ in ()).throw(AssertionError("split read"))  # type: ignore[method-assign]

    response = client.get("/api/source")

    assert response.status_code == 200
    assert response.headers["etag"] == '"7"'
    assert response.json()["room_identity"] == CANONICAL


@pytest.mark.parametrize(
    "payload",
    [
        {"room_url": "http://live.douyin.com/1"},
        {"room_url": "https://evil.invalid/1"},
        {"room_url": "https://live.douyin.com/1\u0000"},
        {"room_url": "https://live.douyin.com/" + "a" * 300},
        {"room_url": CANONICAL, "preferred_quality": "bad\nvalue"},
    ],
)
def test_source_strict_validation_has_structured_safe_errors(client: TestClient, payload: dict[str, object]) -> None:
    response = client.put("/api/source", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert "traceback" not in response.text.lower()
    assert "C:\\" not in response.text


def test_destination_round_trip_masks_secret_with_fixed_value(client: TestClient) -> None:
    response = client.put(
        "/api/destinations/douyin",
        json={"base_server": "rtmps://publish.invalid/live", "stream_key": SECRET, "enabled": True},
    )
    assert response.status_code == 200
    assert response.json() == {
        "kind": "douyin",
        "configured": True,
        "masked_stream_key": "********",
        "enabled": True,
        "status": "STOPPED",
    }
    assert SECRET not in response.text
    assert client.get("/api/destinations/douyin").json() == response.json()


@pytest.mark.parametrize("kind", ["other", "DOUYIN", "douyin%2Fextra", "1 OR 1=1"])
def test_destination_kind_is_closed_enum(client: TestClient, kind: str) -> None:
    response = client.get(f"/api/destinations/{kind}")
    assert response.status_code in {404, 422}
    assert "error" in response.json()


@pytest.mark.parametrize(
    "payload",
    [
        {"base_server": "https://publish.invalid/live", "stream_key": SECRET},
        {"base_server": "rtmp://user@publish.invalid/live", "stream_key": SECRET},
        {"base_server": "rtmp://publish.invalid/live?key=x", "stream_key": SECRET},
        {"base_server": "rtmp://publish.invalid/live", "stream_key": "x\nsecret"},
        {"base_server": "rtmp://publish.invalid/live", "stream_key": "x", "extra": 1},
    ],
)
def test_destination_strict_validation(client: TestClient, payload: dict[str, object]) -> None:
    assert client.put("/api/destinations/douyin", json=payload).status_code == 422


def test_start_requires_enabled_configured_real_destination(client: TestClient, harness: Harness) -> None:
    missing = client.post("/api/control/start", json={})
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "destination_required"
    client.put(
        "/api/destinations/local_test",
        json={"base_server": "rtmp://localhost/live", "stream_key": SECRET, "enabled": True},
    )
    assert client.post("/api/control/start", json={}).status_code == 409
    client.put(
        "/api/destinations/wechat_channels",
        json={"base_server": "rtmps://publish.invalid/live", "stream_key": SECRET, "enabled": True},
    )
    no_source = client.post("/api/control/start", json={})
    assert no_source.status_code == 409
    assert no_source.json()["error"]["code"] == "source_required"
    client.put("/api/source", json={"room_url": CANONICAL})
    assert client.post("/api/control/start", json={}).status_code == 200
    assert harness.controller.started == 1


def test_local_test_start_needs_explicit_mode(harness: Harness) -> None:
    harness.db.set_source(CANONICAL, None, False)
    harness.db.set_destination(DestinationKind.LOCAL_TEST, "rtmp://localhost/live", SECRET)
    deps = ApiDependencies(harness.db, harness.controller, local_test_mode=True)
    app = create_app(lambda: deps)
    with TestClient(app, headers=MUTATING) as client:
        client.headers["x-restream-session"] = app.state.session_token
        assert client.post("/api/control/start", json={"local_test": True}).status_code == 200


def test_stop_is_idempotent(client: TestClient, harness: Harness) -> None:
    assert client.post("/api/control/stop", json={}).status_code == 200
    assert client.post("/api/control/stop", json={}).status_code == 200
    assert harness.controller.stopped == 2


def test_reconnect_is_single_target_and_test_is_short_injected(client: TestClient, harness: Harness) -> None:
    client.put(
        "/api/destinations/douyin",
        json={"base_server": "rtmps://publish.invalid/live", "stream_key": SECRET, "enabled": True},
    )
    response = client.post("/api/destinations/douyin/reconnect", json={})
    assert response.json() == {"kind": "douyin", "status": "reconnect_requested"}
    assert harness.reconnects == [DestinationKind.DOUYIN]
    diagnostic = client.post("/api/destinations/douyin/test", json={})
    assert diagnostic.json() == {"kind": "douyin", "ok": True, "diagnostic": "connection_valid"}
    assert harness.tests == [(DestinationKind.DOUYIN, "rtmps://publish.invalid/live", SECRET)]
    assert SECRET not in diagnostic.text


def test_events_are_bounded_cursor_paginated_and_already_redacted(client: TestClient, harness: Harness) -> None:
    harness.db.events = [
        EventRecord(index, "2026-01-01T00:00:00+00:00", "INFO", "state", {"token": "***", "n": index})
        for index in range(1, 6)
    ]
    response = client.get("/api/events?limit=2&cursor=2")
    assert response.json()["next_cursor"] == 4
    assert [item["id"] for item in response.json()["items"]] == [3, 4]
    assert client.get("/api/events?limit=0").status_code == 422
    assert client.get("/api/events?limit=101").status_code == 422
    assert client.get("/api/events?cursor=1%20OR%201=1").status_code == 422
    assert client.get("/api/events?filter=payload%20LIKE%20%27%25%27").status_code == 400


def test_host_forwarded_origin_and_session_guards(app: FastAPI) -> None:
    with TestClient(app) as client:
        token = app.state.session_token
        assert client.get("/health", headers={"host": "evil.example"}).status_code == 421
        assert client.get("/health", headers={"host": "localhost", "forwarded": "host=evil"}).status_code == 421
        assert client.put("/api/source", headers={"host": "localhost"}, json={"room_url": CANONICAL}).status_code == 403
        assert client.put(
            "/api/source",
            headers={"host": "localhost", "origin": "https://evil.example", "x-restream-session": token},
            json={"room_url": CANONICAL},
        ).status_code == 403
        assert client.put(
            "/api/source",
            headers={"host": "localhost", "origin": "http://localhost", "x-restream-session": "wrong"},
            json={"room_url": CANONICAL},
        ).status_code == 403
        assert client.get("/api/session", headers={"host": "localhost"}).status_code == 403
        assert client.get(
            "/api/session", headers={"host": "localhost", "sec-fetch-site": "cross-site"}
        ).status_code == 403
        browser_bootstrap = client.get(
            "/api/session", headers={"host": "localhost", "sec-fetch-site": "same-origin"}
        )
        assert browser_bootstrap.status_code == 200
        assert browser_bootstrap.json()["session_token"] == token


def test_startup_shutdown_close_dependencies(app: FastAPI, harness: Harness) -> None:
    assert not harness.db.opened
    with TestClient(app, headers={"host": "localhost"}):
        assert harness.db.opened
        assert harness.controller.initialized == 1
    assert harness.db.closed
    assert harness.controller.stopped == 1


def test_secret_absent_from_response_repr_logs_and_openapi(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client.put(
        "/api/destinations/douyin",
        json={"base_server": "rtmps://publish.invalid/live", "stream_key": SECRET, "enabled": True},
    )
    rendered = json.dumps(client.get("/openapi.json").json()) + caplog.text
    assert SECRET not in rendered
    from restream_studio.api.schemas import DestinationUpdate

    assert SECRET not in repr(
        DestinationUpdate(
            base_server="rtmps://publish.invalid/live", stream_key=SecretStr(SECRET)
        )
    )


def test_frontend_mount_is_optional_and_does_not_shadow_api(tmp_path: Path, harness: Harness) -> None:
    (tmp_path / "index.html").write_text("SPA", encoding="utf-8")
    app = create_app(lambda: ApiDependencies(harness.db, harness.controller, assets_dir=tmp_path))
    with TestClient(app, headers={"host": "localhost"}) as client:
        assert client.get("/").text == "SPA"
        assert client.get("/health").json() == {
            "status": "ok",
            "app": "restream-studio",
            "version": "0.1.2",
        }
        assert client.get("/api/missing").status_code == 404
        assert "error" in client.get("/api/missing").json()


def test_frontend_build_is_served_with_referenced_assets(harness: Harness) -> None:
    root = Path(__file__).resolve().parents[2]
    assets_dir = root / "src" / "restream_studio" / "static"
    npm = shutil.which("npm")
    assert npm is not None
    if os.environ.get("RESTREAM_STUDIO_FRONTEND_ALREADY_BUILT") != "1":
        subprocess.run(
            [
                npm,
                "run",
                "frontend:build",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    application = create_app(
        lambda: ApiDependencies(harness.db, harness.controller, assets_dir=assets_dir)
    )
    with TestClient(application, headers={"host": "localhost"}) as client:
        index = client.get("/")
        assert index.status_code == 200
        referenced_assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', index.text)
        assert referenced_assets
        for asset in referenced_assets:
            response = client.get(asset)
            assert response.status_code == 200
            assert response.content


def test_root_build_pipeline_builds_frontend_then_backend_wheel() -> None:
    root = Path(__file__).resolve().parents[2]
    scripts = json.loads((root / "package.json").read_text(encoding="utf-8"))["scripts"]
    assert scripts["build"] == "npm run frontend:build && npm run backend:wheel"
    assert scripts["frontend:build"] == "npm --prefix frontend run build"
    assert "pip wheel" in scripts["backend:wheel"]


def test_concurrent_put_same_body_is_idempotent_and_stale_different_body_conflicts(
    app: FastAPI,
) -> None:
    with TestClient(app, headers={"host": "localhost"}) as client:
        headers = {
            "host": "localhost",
            "origin": "http://localhost",
            "x-restream-session": app.state.session_token,
            "if-match": '"0"',
        }

        def update(quality: str) -> Response:
            return cast(
                Response,
                client.put(
                    "/api/source",
                    headers=headers,
                    json={"room_url": CANONICAL, "preferred_quality": quality},
                ),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = tuple(pool.map(update, ("origin", "origin")))
        assert first.status_code == second.status_code == 200
        stale = client.put(
            "/api/source",
            headers=headers,
            json={"room_url": CANONICAL, "preferred_quality": "uhd"},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "write_conflict"


def test_pure_session_guard_fails_closed_before_lifespan(app: FastAPI) -> None:
    assert app.state.session_token == ""
    assert not mutation_is_authorized("http://localhost", "localhost", "", "")
    assert not mutation_is_authorized("http://localhost", "localhost", "token", "")


@pytest.mark.asyncio
async def test_direct_asgi_mutation_without_lifespan_fails_closed(app: FastAPI) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.put(
            "/api/source",
            headers={"host": "localhost", "origin": "http://localhost"},
            json={"room_url": CANONICAL},
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "request_forbidden"


@pytest.mark.parametrize(
    "server",
    [
        "rtmp://bad host/live",
        "rtmp://-bad.example/live",
        "rtmp://bad-.example/live",
        "rtmp://bad..example/live",
        "rtmp://127.0.0.1/live",
        "rtmp://10.0.0.1/live",
        "rtmp://169.254.1.1/live",
        "rtmp://[::1]/live",
        "rtmp://publish.invalid:99999/live",
    ],
)
def test_pure_destination_hostname_validator_rejects_unsafe_hosts(server: str) -> None:
    from pydantic import ValidationError

    from restream_studio.api.schemas import DestinationUpdate

    with pytest.raises(ValidationError):
        DestinationUpdate(base_server=server, stream_key=SecretStr(SECRET))


def test_pure_start_precondition_orders_destination_before_source() -> None:
    assert start_precondition_error(source_configured=False, real_ready=False, local_ready=False) == (
        "destination_required",
        "An enabled configured destination is required",
    )
    assert start_precondition_error(source_configured=False, real_ready=True, local_ready=False) == (
        "source_required",
        "A source configuration is required",
    )


def test_destination_put_merges_omitted_enabled_and_secret(client: TestClient, harness: Harness) -> None:
    first = client.put(
        "/api/destinations/douyin",
        json={"base_server": "rtmps://publish.invalid/live", "stream_key": SECRET, "enabled": True},
    )
    assert first.headers["etag"] == '"1"'
    second = client.put(
        "/api/destinations/douyin",
        headers={"if-match": '"1"'},
        json={"base_server": "rtmps://new-publish.invalid/live"},
    )
    assert second.status_code == 200
    assert second.json()["enabled"] is True
    assert harness.db.destinations[DestinationKind.DOUYIN]["stream_key"] == SECRET


def test_put_requires_persisted_if_match(client: TestClient) -> None:
    del client.headers["if-match"]
    response = client.put("/api/source", json={"room_url": CANONICAL})
    assert response.status_code == 428
    assert response.json()["error"]["code"] == "precondition_required"


def test_runtime_apply_failure_rolls_back_database_and_revision(
    client: TestClient, harness: Harness
) -> None:
    client.put("/api/source", json={"room_url": CANONICAL})
    old = harness.db.source
    calls = 0

    async def fail_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("internal path and secret must not escape")

    harness.controller.apply_configuration = fail_once  # type: ignore[method-assign]
    response = client.put(
        "/api/source",
        headers={"if-match": '"1"'},
        json={"room_url": "https://live.douyin.com/999"},
    )
    assert response.status_code == 500
    assert harness.db.source == old
    assert harness.db.get_api_revision("source") == 1
    assert "internal path" not in response.text


def test_session_bootstrap_lifespan_reuse_and_api_catchall(app: FastAPI) -> None:
    with TestClient(app, headers={"host": "localhost", "origin": "http://localhost"}) as first:
        old_token = first.get("/api/session").json()["session_token"]
        assert old_token
        assert first.options(
            "/api/missing",
            headers={"x-restream-session": old_token},
        ).status_code == 404
        head = first.head("/api/missing")
        assert head.status_code == 404
        assert head.headers["content-type"].startswith("application/json")
    assert app.state.session_token == ""
    assert app.state.dependencies is None
    with TestClient(app, headers={"host": "localhost", "origin": "http://localhost"}) as second:
        new_token = second.get("/api/session").json()["session_token"]
        assert new_token != old_token
        rejected = second.put(
            "/api/source",
            headers={"x-restream-session": old_token, "if-match": '"0"'},
            json={"room_url": CANONICAL},
        )
        assert rejected.status_code == 403


def test_pure_default_factory_uses_runtime_manager() -> None:
    from restream_studio.main import _default_dependencies
    from restream_studio.runtime import RuntimeManager

    dependencies = _default_dependencies()
    assert isinstance(dependencies.controller, RuntimeManager)
    assert dependencies.reconnect_destination == dependencies.controller.reconnect_destination
    assert dependencies.test_destination == dependencies.controller.test_destination


def test_pure_default_factory_points_to_packaged_frontend_assets() -> None:
    import restream_studio.main as main_module

    dependencies = main_module._default_dependencies()
    assert dependencies.assets_dir == Path(main_module.__file__).resolve().parent / "static"


def test_pure_frontend_assets_resolve_from_frozen_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import restream_studio.main as main_module

    expected = tmp_path / "restream_studio" / "static"
    expected.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert main_module._frontend_assets_dir() == expected


def test_pure_default_factory_wires_runtime_start_reconnect_and_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import restream_studio.main as main_module
    from restream_studio.config import AppPaths

    calls: list[str] = []
    database = object()
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    monkeypatch.delenv("FFPROBE_PATH", raising=False)

    class RuntimeSpy(FakeController):
        def __init__(
            self,
            configured_database: object,
            *,
            ffmpeg_executable: str,
            ffprobe_executable: str,
        ) -> None:
            super().__init__()
            assert configured_database is database
            assert ffmpeg_executable == "ffmpeg"
            assert ffprobe_executable == "ffprobe"

        async def start(self) -> None:
            calls.append("start")

        async def reconnect_destination(self, kind: DestinationKind) -> None:
            calls.append(f"reconnect:{kind.value}")

        async def test_destination(self, kind: DestinationKind, server: str, key: str) -> bool:
            del server, key
            calls.append(f"test:{kind.value}")
            return True

    paths = AppPaths(Path("."), Path("."), Path("."), Path("."), Path("runtime.db"))
    monkeypatch.setattr(AppPaths, "create", lambda value: paths)
    monkeypatch.setattr(main_module, "Database", lambda *args, **kwargs: database)
    monkeypatch.setattr(main_module, "RuntimeManager", RuntimeSpy)
    dependencies = main_module._default_dependencies()

    def finish(coroutine: Any) -> Any:
        iterator = coroutine.__await__()
        try:
            iterator.send(None)
        except StopIteration as stopped:
            return stopped.value
        raise AssertionError("spy coroutine unexpectedly suspended")

    finish(dependencies.controller.start())
    finish(dependencies.reconnect_destination(DestinationKind.DOUYIN))
    assert finish(
        dependencies.test_destination(
            DestinationKind.DOUYIN, "rtmps://publish.invalid/live", SECRET
        )
    )
    assert calls == ["start", "reconnect:DOUYIN", "test:DOUYIN"]


def test_pure_dns_policy_checks_every_answer_and_allows_explicit_localhost() -> None:
    from collections.abc import Coroutine

    from restream_studio.runtime import RuntimeBuildError, validate_destination_dns

    def run_immediate(value: Coroutine[object, object, tuple[str, ...]]) -> tuple[str, ...]:
        iterator = value.__await__()
        try:
            iterator.send(None)
        except StopIteration as stopped:
            return cast(tuple[str, ...], stopped.value)
        raise AssertionError("DNS policy unexpectedly suspended")

    async def mixed_answers(host: str, port: int) -> tuple[str, ...]:
        del host, port
        return ("8.8.8.8", "127.0.0.1")

    async def unsorted_public_answers(host: str, port: int) -> tuple[str, ...]:
        del host, port
        return ("8.8.8.8", "1.1.1.1")

    with pytest.raises(RuntimeBuildError):
        run_immediate(
            validate_destination_dns(
                DestinationKind.DOUYIN,
                "rtmps://publish.invalid/live",
                resolver=mixed_answers,
            )
        )
    assert run_immediate(
        validate_destination_dns(DestinationKind.LOCAL_TEST, "rtmp://localhost/live")
    ) == ("127.0.0.1", "::1")
    assert run_immediate(
        validate_destination_dns(
            DestinationKind.DOUYIN,
            "rtmp://publish.invalid/live",
            resolver=unsorted_public_answers,
        )
    ) == ("1.1.1.1", "8.8.8.8")


def test_runtime_initialize_resumes_and_rebuild_shutdown_does_not_rewrite_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import restream_studio.runtime as runtime_module
    from restream_studio.persistence.database import RuntimeDestination

    database = FakeDatabase()
    database.source = SourceConfig(CANONICAL, "origin", True)
    database.revisions["source"] = 9
    database.set_destination(
        DestinationKind.DOUYIN,
        "rtmps://publish.invalid/live",
        SECRET,
        enabled=True,
    )
    controllers: list[Any] = []

    def runtime_destination(kind: DestinationKind) -> RuntimeDestination:
        item = database.destinations[kind]
        return RuntimeDestination(
            kind,
            "douyin",
            cast(str, item["base_server"]),
            cast(str, item["stream_key"]),
            cast(bool, item["enabled"]),
        )

    database.get_destination_runtime = runtime_destination  # type: ignore[method-assign]

    class ControllerSpy(FakeController):
        def __init__(self, **kwargs: object) -> None:
            super().__init__()
            del kwargs
            self.desired = True
            self.shutdowns = 0
            controllers.append(self)

        async def snapshot(self) -> ControllerSnapshot:
            snapshot = await super().snapshot()
            return ControllerSnapshot(
                snapshot.room_identity,
                self.desired,
                snapshot.source_state,
                snapshot.source_failure,
                snapshot.error_detail,
                snapshot.recovery_successes,
                snapshot.outputs,
            )

        async def shutdown(self) -> None:
            self.shutdowns += 1

    monkeypatch.setattr(runtime_module, "Controller", ControllerSpy)
    manager = runtime_module.RuntimeManager(cast(Any, database))

    def finish(coroutine: Any) -> Any:
        iterator = coroutine.__await__()
        try:
            iterator.send(None)
        except StopIteration as stopped:
            return stopped.value
        raise AssertionError("runtime spy coroutine unexpectedly suspended")

    finish(manager.initialize())
    assert controllers[0].started == 1
    finish(manager.apply_configuration())
    assert controllers[0].shutdowns == 1
    assert controllers[1].started == 1
    assert database.source == SourceConfig(CANONICAL, "origin", True)
    assert database.revisions["source"] == 9


def test_lifespan_cleanup_clears_token_and_closes_database_when_runtime_cleanup_fails() -> None:
    database = FakeDatabase()
    controller = FakeController()
    cleared = False

    async def broken_shutdown() -> None:
        raise RuntimeError("shutdown detail")

    def broken_clear() -> None:
        nonlocal cleared
        cleared = True
        raise RuntimeError("clear detail")

    controller.shutdown = broken_shutdown  # type: ignore[method-assign]
    controller.clear = broken_clear  # type: ignore[method-assign]
    application = create_app(lambda: ApiDependencies(database, controller))
    lifespan = application.router.lifespan_context(application)

    def finish(coroutine: Any) -> Any:
        iterator = coroutine.__await__()
        try:
            iterator.send(None)
        except StopIteration as stopped:
            return stopped.value
        raise AssertionError("lifespan coroutine unexpectedly suspended")

    finish(lifespan.__aenter__())
    assert application.state.session_token
    with pytest.raises(RuntimeError, match="did not complete cleanly"):
        finish(lifespan.__aexit__(None, None, None))
    assert application.state.session_token == ""
    assert application.state.dependencies is None
    assert cleared
    assert database.closed


def test_runtime_restart_failure_reports_error_instead_of_false_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import restream_studio.runtime as runtime_module
    from restream_studio.persistence.database import RuntimeDestination

    database = FakeDatabase()
    database.source = SourceConfig(CANONICAL, None, True)
    database.set_destination(
        DestinationKind.DOUYIN, "rtmps://publish.invalid/live", SECRET, enabled=True
    )
    database.get_destination_runtime = lambda kind: RuntimeDestination(  # type: ignore[method-assign]
        kind, "douyin", "rtmps://publish.invalid/live", SECRET, True
    )

    class FailingController(FakeController):
        async def snapshot(self) -> ControllerSnapshot:
            snapshot = await super().snapshot()
            return ControllerSnapshot(
                snapshot.room_identity,
                True,
                snapshot.source_state,
                None,
                None,
                0,
                snapshot.outputs,
            )

        async def start(self) -> None:
            raise RuntimeError("private startup failure")

    monkeypatch.setattr(runtime_module, "Controller", lambda **kwargs: FailingController())
    manager = runtime_module.RuntimeManager(cast(Any, database))

    def finish(coroutine: Any) -> Any:
        iterator = coroutine.__await__()
        try:
            iterator.send(None)
        except StopIteration as stopped:
            return stopped.value
        raise AssertionError("runtime failure coroutine unexpectedly suspended")

    finish(manager.initialize())
    snapshot = finish(manager.snapshot())
    assert snapshot.desired_running is False
    assert snapshot.source_state is SourceState.ERROR
    assert snapshot.error_detail is None


def test_runtime_persists_stopped_error_when_desired_but_no_destination_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import restream_studio.runtime as runtime_module
    from restream_studio.persistence.database import RuntimeDestination

    database = FakeDatabase()
    database.source = SourceConfig(CANONICAL, "origin", True)
    database.set_destination(
        DestinationKind.DOUYIN, "rtmps://publish.invalid/live", SECRET, enabled=False
    )
    database.get_destination_runtime = lambda kind: RuntimeDestination(  # type: ignore[method-assign]
        kind, "douyin", "rtmps://publish.invalid/live", SECRET, False
    )

    class RestoredController(FakeController):
        async def snapshot(self) -> ControllerSnapshot:
            snapshot = await super().snapshot()
            return ControllerSnapshot(
                snapshot.room_identity,
                True,
                snapshot.source_state,
                None,
                None,
                0,
                snapshot.outputs,
            )

    monkeypatch.setattr(runtime_module, "Controller", lambda **kwargs: RestoredController())
    manager = runtime_module.RuntimeManager(cast(Any, database))

    def finish(coroutine: Any) -> Any:
        iterator = coroutine.__await__()
        try:
            iterator.send(None)
        except StopIteration as stopped:
            return stopped.value
        raise AssertionError("blocked runtime coroutine unexpectedly suspended")

    finish(manager.initialize())
    snapshot = finish(manager.snapshot())
    assert database.source is not None and database.source.desired_running is False
    assert snapshot.desired_running is False
    assert snapshot.source_state is SourceState.ERROR
    with pytest.raises(runtime_module.RuntimeBuildError):
        finish(manager.start())


def test_update_api_is_strict_protected_and_secret_safe(harness: Harness) -> None:
    from restream_studio.update.service import UpdateOperationError, UpdateSnapshot, UpdateStatus

    class FakeUpdateService:
        def __init__(self) -> None:
            self.value = UpdateSnapshot(UpdateStatus.IDLE, "0.1.2")
            self.calls: list[str] = []

        def snapshot(self) -> UpdateSnapshot:
            return self.value

        async def check(self) -> UpdateSnapshot:
            self.calls.append("check")
            self.value = UpdateSnapshot(UpdateStatus.CURRENT, "0.1.2")
            return self.value

        async def download(self) -> UpdateSnapshot:
            self.calls.append("download")
            raise UpdateOperationError("download_in_progress", "Download already in progress")

        async def install(self, *, current_pid: int) -> None:
            assert current_pid > 0
            self.calls.append("install")

        def start_background(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

    updates = FakeUpdateService()
    application = create_app(
        lambda: ApiDependencies(harness.db, harness.controller, update_service=updates)
    )
    with TestClient(application, headers={"host": "localhost"}) as update_client:
        token = application.state.session_token
        authorized = {
            "origin": "http://localhost",
            "x-restream-session": token,
        }
        assert update_client.get("/api/update").json() == {
            "status": "idle",
            "current_version": "0.1.2",
            "available_version": None,
            "release_url": None,
            "last_checked_at": None,
            "error_code": None,
            "error_message": None,
        }
        assert update_client.post("/api/update/check", json={}).status_code == 403
        checked = update_client.post("/api/update/check", json={}, headers=authorized)
        assert checked.status_code == 200 and checked.json()["status"] == "current"
        extra = update_client.post(
            "/api/update/check", json={"unexpected": True}, headers=authorized
        )
        assert extra.status_code == 422
        duplicate = update_client.post("/api/update/download", json={}, headers=authorized)
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "download_in_progress"
        assert "path" not in duplicate.text.casefold()


def test_update_install_requires_stopped_relay_before_service_call(harness: Harness) -> None:
    from restream_studio.update.service import UpdateSnapshot, UpdateStatus

    class FakeUpdateService:
        def __init__(self) -> None:
            self.installs = 0

        def snapshot(self) -> UpdateSnapshot:
            return UpdateSnapshot(UpdateStatus.READY, "0.1.2", available_version="0.2.0")

        async def check(self) -> UpdateSnapshot:
            return self.snapshot()

        async def download(self) -> UpdateSnapshot:
            return self.snapshot()

        async def install(self, *, current_pid: int) -> None:
            del current_pid
            self.installs += 1

        def start_background(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

    harness.controller.started = 1
    updates = FakeUpdateService()
    application = create_app(
        lambda: ApiDependencies(harness.db, harness.controller, update_service=updates)
    )
    with TestClient(application, headers={"host": "localhost"}) as update_client:
        response = update_client.post(
            "/api/update/install",
            json={},
            headers={
                "origin": "http://localhost",
                "x-restream-session": application.state.session_token,
            },
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "relay_must_be_stopped"
    assert updates.installs == 0


@pytest.mark.asyncio
async def test_update_install_and_start_share_atomic_write_boundary(harness: Harness) -> None:
    from restream_studio.update.service import UpdateSnapshot, UpdateStatus

    entered = asyncio.Event()
    release = asyncio.Event()

    class BarrierUpdateService:
        installation_pending = False

        def snapshot(self) -> UpdateSnapshot:
            return UpdateSnapshot(UpdateStatus.READY, "0.1.2", available_version="0.2.0")

        async def check(self) -> UpdateSnapshot:
            return self.snapshot()

        async def download(self) -> UpdateSnapshot:
            return self.snapshot()

        async def install(self, *, current_pid: int) -> None:
            assert current_pid > 0
            self.installation_pending = True
            entered.set()
            await release.wait()

        def start_background(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

    harness.db.source = SourceConfig(CANONICAL, "origin", False)
    harness.db.set_destination(
        DestinationKind.DOUYIN,
        "rtmps://publish.invalid/live",
        SECRET,
        enabled=True,
    )
    updates = BarrierUpdateService()
    application = create_app(
        lambda: ApiDependencies(harness.db, harness.controller, update_service=updates)
    )
    async with application.router.lifespan_context(application):
        headers = {
            "host": "localhost",
            "origin": "http://localhost",
            "x-restream-session": application.state.session_token,
        }
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://localhost"
        ) as concurrent_client:
            install_task = asyncio.create_task(
                concurrent_client.post("/api/update/install", json={}, headers=headers)
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            start_task = asyncio.create_task(
                concurrent_client.post("/api/control/start", json={}, headers=headers)
            )
            await asyncio.sleep(0)
            assert not start_task.done()
            release.set()
            install_response, start_response = await asyncio.gather(
                install_task, start_task
            )
    assert install_response.status_code == 200
    assert start_response.status_code == 409
    assert start_response.json()["error"]["code"] == "update_installing"
    assert harness.controller.started == 0


def test_installed_update_gate_blocks_check_download_and_start(harness: Harness) -> None:
    from restream_studio.update.service import UpdateOperationError, UpdateSnapshot, UpdateStatus

    class InstalledUpdateService:
        installation_pending = True

        def snapshot(self) -> UpdateSnapshot:
            return UpdateSnapshot(UpdateStatus.READY, "0.1.2", available_version="0.2.0")

        async def check(self) -> UpdateSnapshot:
            raise UpdateOperationError("update_installing", "Update installation is pending")

        async def download(self) -> UpdateSnapshot:
            raise UpdateOperationError("update_installing", "Update installation is pending")

        async def install(self, *, current_pid: int) -> None:
            del current_pid
            raise UpdateOperationError("update_installing", "Update installation is pending")

        def start_background(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

    harness.db.source = SourceConfig(CANONICAL, "origin", False)
    harness.db.set_destination(
        DestinationKind.DOUYIN,
        "rtmps://publish.invalid/live",
        SECRET,
        enabled=True,
    )
    application = create_app(
        lambda: ApiDependencies(
            harness.db, harness.controller, update_service=InstalledUpdateService()
        )
    )
    with TestClient(application, headers={"host": "localhost"}) as update_client:
        headers = {
            "origin": "http://localhost",
            "x-restream-session": application.state.session_token,
        }
        for path in (
            "/api/update/check",
            "/api/update/download",
            "/api/update/install",
            "/api/control/start",
        ):
            response = update_client.post(path, json={}, headers=headers)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "update_installing"
            assert "path" not in response.text.casefold()


def test_application_shutdown_callback_only_requests_uvicorn_exit(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Awaitable, Callable

    from restream_studio.update.service import UpdateSnapshot, UpdateStatus

    class ShutdownAwareService:
        callback: Callable[[], Awaitable[None] | None] | None = None

        def set_shutdown_callback(
            self, callback: Callable[[], Awaitable[None] | None]
        ) -> None:
            self.callback = callback

        def snapshot(self) -> UpdateSnapshot:
            return UpdateSnapshot(UpdateStatus.IDLE, "0.1.2")

        async def check(self) -> UpdateSnapshot:
            return self.snapshot()

        async def download(self) -> UpdateSnapshot:
            return self.snapshot()

        async def install(self, *, current_pid: int) -> None:
            del current_pid

        def start_background(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

    updates = ShutdownAwareService()
    application = create_app(
        lambda: ApiDependencies(harness.db, harness.controller, update_service=updates)
    )

    class Server:
        should_exit = False

    server = Server()
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("os.kill must not be used"))
    assert updates.callback is not None
    with TestClient(application, headers={"host": "localhost"}):
        application.state.uvicorn_server = server
        result = updates.callback()
        assert result is None
        assert application.state.shutdown_requested is True
        assert server.should_exit is True
        assert harness.db.opened is True and harness.db.closed is False
    assert harness.db.closed is True
    assert harness.controller.stopped == 1
