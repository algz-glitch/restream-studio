from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr

from restream_studio.api.routes import ApiDependencies
from restream_studio.domain import DestinationKind, OutputState, SourceState
from restream_studio.main import create_app
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

    def open(self) -> FakeDatabase:
        self.opened = True
        return self

    def close(self) -> None:
        self.closed = True

    def get_source(self) -> SourceConfig | None:
        return self.source

    def set_source(self, room_identity: str, preferred_quality: str | None, desired_running: bool) -> None:
        from restream_studio.source import normalize_douyin_url

        self.source = SourceConfig(normalize_douyin_url(room_identity), preferred_quality, desired_running)

    def get_destination(self, kind: DestinationKind) -> DestinationConfig | None:
        item = self.destinations.get(kind)
        if item is None:
            return None
        return DestinationConfig(kind, kind.name.lower(), "rtmps://publish.invalid/live", bool(item["enabled"]), True)

    def get_destination_runtime(self, kind: DestinationKind) -> Any:
        item = self.destinations[kind]
        return type("Runtime", (), {"base_server": item["base_server"], "stream_key": item["stream_key"]})()

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


class FakeController:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.initialized = 0
        self.enabled_calls: list[tuple[str, bool]] = []

    async def initialize(self) -> None:
        self.initialized += 1

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
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
            {"origin": "http://localhost", "x-restream-session": app.state.session_token}
        )
        yield test_client


def test_health_is_minimal_and_status_is_safe(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
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
        json={"base_server": "rtmp://127.0.0.1/live", "stream_key": SECRET, "enabled": True},
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
    harness.db.set_destination(DestinationKind.LOCAL_TEST, "rtmp://127.0.0.1/live", SECRET)
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
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/api/missing").status_code == 404
        assert "error" in client.get("/api/missing").json()


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
