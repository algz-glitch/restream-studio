from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any, Self
from urllib.error import URLError
from urllib.request import Request

from restream_studio.update.client import DEFAULT_MANIFEST_URL, GitHubUpdateClient
from restream_studio.update.contracts import UpdateErrorCode, UpdateManifest


def payload(content: bytes = b"installer") -> dict[str, object]:
    return {
        "schema_version": 1,
        "version": "1.2.3",
        "installer_url": (
            "https://github.com/algz-glitch/restream-studio/releases/download/"
            "v1.2.3/RestreamStudio-Setup-1.2.3.exe"
        ),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "published_at": "2026-09-13T12:30:00Z",
        "release_url": "https://github.com/algz-glitch/restream-studio/releases/tag/v1.2.3",
    }


class Response(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class FakeOpener:
    def __init__(self, responses: list[bytes | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, float]] = []

    def open(self, request: Request, timeout: float) -> Response:
        self.calls.append((request.full_url, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return Response(response)


class ChunkedResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read_count = 0

    def read(self, size: int = -1) -> bytes:
        del size
        self.read_count += 1
        return self.chunks.pop(0) if self.chunks else b""

    def close(self) -> None:
        return None


def encoded(value: dict[str, object]) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def test_default_url_and_newer_version_check_are_offline_and_bounded() -> None:
    opener = FakeOpener([encoded(payload())])
    client = GitHubUpdateClient(opener=opener, timeout=3.5)
    result = client.check_for_update("1.2.2")
    assert DEFAULT_MANIFEST_URL == (
        "https://github.com/algz-glitch/restream-studio/releases/latest/download/latest.json"
    )
    assert result.update_available
    assert opener.calls == [(DEFAULT_MANIFEST_URL, 3.5)]


def test_same_or_older_release_is_not_an_available_update() -> None:
    for current_version in ("1.2.3", "2.0.0"):
        result = GitHubUpdateClient(opener=FakeOpener([encoded(payload())])).check_for_update(
            current_version
        )
        assert not result.update_available
        assert result.manifest is None
        assert result.error_code is None


def test_manifest_is_strict_json_and_limited_to_one_mibibyte() -> None:
    duplicate = encoded(payload())[:-1] + b',"size":9}'
    malformed = b'{"schema_version":1} trailing'
    oversized = b" " * (1024 * 1024 + 1)
    for body, code in (
        (duplicate, UpdateErrorCode.INVALID_MANIFEST),
        (malformed, UpdateErrorCode.INVALID_MANIFEST),
        (oversized, UpdateErrorCode.MANIFEST_TOO_LARGE),
    ):
        result = GitHubUpdateClient(opener=FakeOpener([body])).check_for_update("1.0.0")
        assert result.error_code is code


def test_manifest_limit_applies_across_fragmented_reads() -> None:
    response = ChunkedResponse([b" " * 700_000, b" " * 400_000])

    def transport(request: Request, timeout: float) -> ChunkedResponse:
        assert request.full_url == DEFAULT_MANIFEST_URL
        assert timeout == 10.0
        return response

    result = GitHubUpdateClient(transport=transport).check_for_update("1.0.0")
    assert result.error_code is UpdateErrorCode.MANIFEST_TOO_LARGE


def test_network_failure_is_a_typed_result() -> None:
    result = GitHubUpdateClient(opener=FakeOpener([URLError("offline")])).check_for_update("1.0.0")
    assert result.error_code is UpdateErrorCode.NETWORK
    assert not result.update_available


def test_wrapped_timeout_is_classified_as_timeout() -> None:
    result = GitHubUpdateClient(opener=FakeOpener([URLError(TimeoutError())])).check_for_update(
        "1.0.0"
    )
    assert result.error_code is UpdateErrorCode.TIMEOUT


def test_transport_adapter_failure_is_a_typed_network_result() -> None:
    def transport(request: Request, timeout: float) -> Response:
        del request, timeout
        raise RuntimeError("adapter failed")

    result = GitHubUpdateClient(transport=transport).check_for_update("1.0.0")
    assert result.error_code is UpdateErrorCode.NETWORK


def test_download_streams_to_partial_then_atomically_replaces(tmp_path: Path) -> None:
    content = b"verified-installer-content"
    opener = FakeOpener([content])
    client = GitHubUpdateClient(opener=opener, chunk_size=4)
    destination = tmp_path / "RestreamStudio-Setup-1.2.3.exe"
    destination.write_bytes(b"old")
    manifest = UpdateManifest.from_dict(payload(content))
    result = client.download_installer(manifest, destination)
    assert result == destination
    assert destination.read_bytes() == content
    assert not destination.with_name(destination.name + ".partial").exists()
    assert opener.calls[0][0] == manifest.installer_url


def test_download_rejects_size_or_hash_and_cleans_partial(tmp_path: Path) -> None:
    destination = tmp_path / "installer.exe"
    destination.write_bytes(b"existing")
    for body, changes, code in (
        (b"short", {"size": 99}, UpdateErrorCode.SIZE_MISMATCH),
        (b"wrong-hash", {"size": 10, "sha256": "0" * 64}, UpdateErrorCode.SHA256_MISMATCH),
    ):
        manifest = UpdateManifest.from_dict(payload() | changes)
        client = GitHubUpdateClient(opener=FakeOpener([body]))
        result = client.download_installer_result(manifest, destination)
        assert result.error_code is code
        assert destination.read_bytes() == b"existing"
        assert not destination.with_name(destination.name + ".partial").exists()


def test_download_stops_as_soon_as_declared_size_is_exceeded(tmp_path: Path) -> None:
    response = ChunkedResponse([b"12345", b"must-not-be-read"])

    def transport(request: Request, timeout: float) -> ChunkedResponse:
        del request, timeout
        return response

    manifest = UpdateManifest.from_dict(payload(b"1234"))
    client = GitHubUpdateClient(transport=transport)
    result = client.download_installer_result(manifest, tmp_path / "installer.exe")
    assert result.error_code is UpdateErrorCode.SIZE_MISMATCH
    assert response.read_count == 1


def test_unexpected_download_failure_cleans_partial(tmp_path: Path) -> None:
    class BrokenResponse(ChunkedResponse):
        def read(self, size: int = -1) -> bytes:
            del size
            raise RuntimeError("broken stream")

    destination = tmp_path / "installer.exe"
    partial = destination.with_name(destination.name + ".partial")
    partial.write_bytes(b"stale")

    def transport(request: Request, timeout: float) -> BrokenResponse:
        del request, timeout
        return BrokenResponse([])

    result = GitHubUpdateClient(transport=transport).download_installer_result(
        UpdateManifest.from_dict(payload()), destination
    )
    assert result.error_code is UpdateErrorCode.DOWNLOAD_FAILED
    assert not partial.exists()


def test_client_never_executes_downloaded_file(monkeypatch: Any, tmp_path: Path) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("download client must not execute files")

    monkeypatch.setattr("os.startfile", forbidden)
    content = b"installer"
    destination = tmp_path / "installer.exe"
    GitHubUpdateClient(opener=FakeOpener([content])).download_installer(
        UpdateManifest.from_dict(payload(content)), destination
    )
    assert destination.read_bytes() == content
