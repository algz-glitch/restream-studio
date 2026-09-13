from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Mapping
from email.message import Message
from pathlib import Path
from typing import Any, Self
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from restream_studio.update.client import (
    DEFAULT_MANIFEST_URL,
    GitHubUpdateClient,
    _TrustedRedirectHandler,
)
from restream_studio.update.contracts import (
    DownloadStatus,
    UpdateErrorCode,
    UpdateManifest,
)

INSTALLER_URL = (
    "https://github.com/algz-glitch/restream-studio/releases/download/"
    "v1.2.3/RestreamStudio-Setup-1.2.3.exe"
)


def payload(content: bytes = b"installer") -> dict[str, object]:
    return {
        "schema_version": 1,
        "version": "1.2.3",
        "installer_url": INSTALLER_URL,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "published_at": "2026-09-13T12:30:00Z",
        "release_url": "https://github.com/algz-glitch/restream-studio/releases/tag/v1.2.3",
    }


class Response(io.BytesIO):
    def __init__(
        self,
        body: bytes,
        *,
        url: str = DEFAULT_MANIFEST_URL,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(body)
        self._url = url
        self.status = status
        self.headers = headers or {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def geturl(self) -> str:
        return self._url


class FakeOpener:
    def __init__(self, responses: list[bytes | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, float]] = []

    def open(self, request: Request, timeout: float) -> Response:
        self.calls.append((request.full_url, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return Response(response, url=request.full_url)


class ChunkedResponse:
    def __init__(self, chunks: list[bytes], url: str = DEFAULT_MANIFEST_URL) -> None:
        self.chunks = chunks
        self.read_count = 0
        self.url = url
        self.status = 200
        self.headers: Mapping[str, str] = {}

    def read(self, size: int = -1) -> bytes:
        del size
        self.read_count += 1
        return self.chunks.pop(0) if self.chunks else b""

    def close(self) -> None:
        return None

    def geturl(self) -> str:
        return self.url


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


def test_manifest_url_cannot_leave_configured_repository() -> None:
    for url in (
        "http://github.com/algz-glitch/restream-studio/releases/latest/download/latest.json",
        "https://evil.test/algz-glitch/restream-studio/releases/latest/download/latest.json",
        "https://github.com/other/restream-studio/releases/latest/download/latest.json",
    ):
        with pytest.raises(ValueError, match="manifest_url"):
            GitHubUpdateClient(manifest_url=url, opener=FakeOpener([]))


def test_redirect_handler_rejects_downgrade_evil_host_and_checks_every_hop() -> None:
    handler = _TrustedRedirectHandler()
    request = Request(DEFAULT_MANIFEST_URL)
    with pytest.raises(URLError, match="redirect"):
        handler.redirect_request(request, None, 302, "Found", {}, "http://github.com/file")
    with pytest.raises(URLError, match="redirect"):
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.test/file")
    with pytest.raises(URLError, match="redirect"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://github.com/other/repo/releases/download/v1/file.exe",
        )

    first = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://github.com/algz-glitch/restream-studio/releases/download/v1/latest.json",
    )
    assert first is not None
    second = handler.redirect_request(
        first,
        None,
        302,
        "Found",
        {},
        "https://release-assets.githubusercontent.com/github-production-release-asset/file?sig=x",
    )
    assert second is not None
    with pytest.raises(URLError, match="redirect"):
        handler.redirect_request(
            second,
            None,
            302,
            "Found",
            {},
            "https://attacker.githubusercontent.com/file",
        )


def test_final_response_url_is_validated_for_manifest_and_installer(tmp_path: Path) -> None:
    class FinalUrlOpener:
        def __init__(self, final_url: str, body: bytes) -> None:
            self.final_url, self.body = final_url, body

        def open(self, request: Request, timeout: float) -> Response:
            del request, timeout
            return Response(self.body, url=self.final_url)

    check = GitHubUpdateClient(
        opener=FinalUrlOpener("https://evil.test/latest.json", encoded(payload()))
    ).check_for_update("1.0.0")
    assert check.error_code is UpdateErrorCode.NETWORK

    manifest = UpdateManifest.from_dict(payload())
    download = GitHubUpdateClient(
        opener=FinalUrlOpener("http://release-assets.githubusercontent.com/file", b"installer")
    ).download_installer_result(manifest, tmp_path / "installer.exe")
    assert download.status is DownloadStatus.FAILED
    assert download.error_code is UpdateErrorCode.NETWORK

    wrong_repository = GitHubUpdateClient(
        opener=FinalUrlOpener("https://github.com/other/repo/releases/download/v1/file.exe", b"installer")
    ).download_installer_result(manifest, tmp_path / "installer.exe")
    assert wrong_repository.error_code is UpdateErrorCode.NETWORK


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


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, UpdateErrorCode.RATE_LIMITED), (404, UpdateErrorCode.HTTP_STATUS)],
)
def test_http_statuses_are_mapped_explicitly(status: int, code: UpdateErrorCode) -> None:
    error = HTTPError(DEFAULT_MANIFEST_URL, status, "status", Message(), None)
    result = GitHubUpdateClient(opener=FakeOpener([error])).check_for_update("1.0.0")
    assert result.error_code is code


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (URLError(TimeoutError()), UpdateErrorCode.TIMEOUT),
        (
            HTTPError(DEFAULT_MANIFEST_URL, 429, "rate", Message(), None),
            UpdateErrorCode.RATE_LIMITED,
        ),
        (
            HTTPError(DEFAULT_MANIFEST_URL, 503, "status", Message(), None),
            UpdateErrorCode.HTTP_STATUS,
        ),
    ],
)
def test_download_uses_same_network_error_mapping(
    failure: Exception, code: UpdateErrorCode, tmp_path: Path
) -> None:
    result = GitHubUpdateClient(opener=FakeOpener([failure])).download_installer_result(
        UpdateManifest.from_dict(payload()), tmp_path / "installer.exe"
    )
    assert result.error_code is code


def test_wrapped_timeout_is_classified_as_timeout() -> None:
    result = GitHubUpdateClient(opener=FakeOpener([URLError(TimeoutError())])).check_for_update(
        "1.0.0"
    )
    assert result.error_code is UpdateErrorCode.TIMEOUT


def test_transport_adapter_failure_is_a_typed_network_result() -> None:
    def transport(request: Request, timeout: float) -> Response:
        del request, timeout
        raise OSError("adapter failed")

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


def test_download_requires_space_and_matching_content_length(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = UpdateManifest.from_dict(payload())
    monkeypatch.setattr("shutil.disk_usage", lambda _: type("Usage", (), {"free": 0})())
    no_space = GitHubUpdateClient(opener=FakeOpener([])).download_installer_result(
        manifest, tmp_path / "installer.exe"
    )
    assert no_space.error_code is UpdateErrorCode.INSUFFICIENT_SPACE

    class LengthOpener:
        def open(self, request: Request, timeout: float) -> Response:
            del timeout
            return Response(
                b"installer",
                url=request.full_url,
                headers={"Content-Length": "99"},
            )

    monkeypatch.setattr("shutil.disk_usage", lambda _: type("Usage", (), {"free": 10**9})())
    wrong_length = GitHubUpdateClient(opener=LengthOpener()).download_installer_result(
        manifest, tmp_path / "installer.exe"
    )
    assert wrong_length.error_code is UpdateErrorCode.SIZE_MISMATCH


def test_tempfile_is_unique_exclusive_and_created_in_destination_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import tempfile

    real_mkstemp = tempfile.mkstemp
    calls: list[tuple[str | None, str | None, str | None]] = []

    def tracked_mkstemp(
        suffix: str | None = None, prefix: str | None = None, dir: str | None = None
    ) -> tuple[int, str]:
        calls.append((suffix, prefix, dir))
        return real_mkstemp(suffix=suffix, prefix=prefix, dir=dir)

    monkeypatch.setattr(tempfile, "mkstemp", tracked_mkstemp)
    destination = tmp_path / "installer.exe"
    result = GitHubUpdateClient(opener=FakeOpener([b"installer"])).download_installer_result(
        UpdateManifest.from_dict(payload()), destination
    )
    assert result.status is DownloadStatus.SUCCESS
    assert calls[0][0] == ".partial"
    assert Path(calls[0][2] or "") == tmp_path


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
    response = ChunkedResponse([b"12345", b"must-not-be-read"], INSTALLER_URL)

    def transport(request: Request, timeout: float) -> ChunkedResponse:
        del request, timeout
        return response

    manifest = UpdateManifest.from_dict(payload(b"1234"))
    client = GitHubUpdateClient(transport=transport)
    result = client.download_installer_result(manifest, tmp_path / "installer.exe")
    assert result.error_code is UpdateErrorCode.SIZE_MISMATCH
    assert response.read_count == 1


def test_download_timeout_cleans_unique_partial(tmp_path: Path) -> None:
    class BrokenResponse(ChunkedResponse):
        def read(self, size: int = -1) -> bytes:
            del size
            raise TimeoutError("broken stream")

    destination = tmp_path / "installer.exe"

    def transport(request: Request, timeout: float) -> BrokenResponse:
        del request, timeout
        return BrokenResponse([], INSTALLER_URL)

    result = GitHubUpdateClient(transport=transport).download_installer_result(
        UpdateManifest.from_dict(payload()), destination
    )
    assert result.error_code is UpdateErrorCode.TIMEOUT
    assert list(tmp_path.glob("*.partial")) == []


def test_cleanup_failure_does_not_replace_primary_download_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class BrokenResponse(ChunkedResponse):
        def read(self, size: int = -1) -> bytes:
            del size
            raise TimeoutError("primary timeout")

    real_unlink = Path.unlink

    def failed_cleanup(path: Path, missing_ok: bool = False) -> None:
        if path.suffix == ".partial":
            raise OSError("cleanup failed")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failed_cleanup)

    def transport(request: Request, timeout: float) -> BrokenResponse:
        del request, timeout
        return BrokenResponse([], INSTALLER_URL)

    result = GitHubUpdateClient(transport=transport).download_installer_result(
        UpdateManifest.from_dict(payload()), tmp_path / "installer.exe"
    )
    assert result.error_code is UpdateErrorCode.TIMEOUT


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
