from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from contextlib import closing, suppress
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .contracts import (
    MAX_INSTALLER_BYTES,
    DownloadResult,
    UpdateCheckResult,
    UpdateErrorCode,
    UpdateManifest,
    Version,
)

DEFAULT_MANIFEST_URL = (
    "https://github.com/algz-glitch/restream-studio/releases/latest/download/latest.json"
)
MAX_MANIFEST_BYTES = 1024 * 1024
DISK_SPACE_RESERVE_BYTES = 64 * 1024 * 1024
DEFAULT_TIMEOUT = 10.0
DEFAULT_CHUNK_SIZE = 64 * 1024
_GITHUB_HOST = "github.com"
_ASSET_HOSTS = frozenset(
    {"objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
_MANIFEST_PATH_RE = re.compile(
    r"/algz-glitch/restream-studio/releases/"
    r"(?:latest/download|download/[^/]+)/latest\.json\Z"
)


class ReadableResponse(Protocol):
    status: int
    headers: Mapping[str, str]

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...

    def geturl(self) -> str: ...


class Opener(Protocol):
    def open(self, request: Request, timeout: float) -> ReadableResponse: ...


Transport = Callable[[Request, float], ReadableResponse]


class UpdateClientError(RuntimeError):
    def __init__(self, code: UpdateErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class _DuplicateKeyError(ValueError):
    pass


class _TrustedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        try:
            _validate_redirect_url(newurl)
        except ValueError as error:
            raise URLError("untrusted update redirect target") from error
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHubUpdateClient:
    def __init__(
        self,
        manifest_url: str = DEFAULT_MANIFEST_URL,
        *,
        opener: Opener | None = None,
        transport: Transport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        if opener is not None and transport is not None:
            raise ValueError("provide opener or transport, not both")
        _validate_manifest_url(manifest_url)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.manifest_url = manifest_url
        self._opener = opener or cast(Opener, build_opener(_TrustedRedirectHandler()))
        self._transport = transport
        self.timeout = timeout
        self.chunk_size = chunk_size

    def check_for_update(self, current_version: str | Version) -> UpdateCheckResult:
        try:
            current = (
                current_version
                if isinstance(current_version, Version)
                else Version.parse(current_version)
            )
        except (TypeError, ValueError) as error:
            return UpdateCheckResult.failed(UpdateErrorCode.INVALID_CURRENT_VERSION, str(error))
        try:
            manifest = self.fetch_manifest()
        except UpdateClientError as error:
            return UpdateCheckResult.failed(error.code, str(error))
        return (
            UpdateCheckResult.available(manifest)
            if manifest.version > current
            else UpdateCheckResult.current()
        )

    def fetch_manifest(self) -> UpdateManifest:
        response = self._open_response(
            _request(self.manifest_url, "application/json"), response_kind="manifest"
        )
        try:
            body = bytearray()
            with closing(response):
                while len(body) <= MAX_MANIFEST_BYTES:
                    chunk = response.read(MAX_MANIFEST_BYTES + 1 - len(body))
                    if not chunk:
                        break
                    body.extend(chunk)
        except TimeoutError as error:
            raise UpdateClientError(UpdateErrorCode.TIMEOUT, "manifest request timed out") from error
        except URLError as error:
            raise _url_error(error, "manifest request") from error
        except OSError as error:
            raise UpdateClientError(UpdateErrorCode.NETWORK, "manifest request failed") from error
        if len(body) > MAX_MANIFEST_BYTES:
            raise UpdateClientError(
                UpdateErrorCode.MANIFEST_TOO_LARGE, "manifest exceeds the 1 MiB limit"
            )
        try:
            payload = json.loads(
                body.decode("utf-8", errors="strict"),
                object_pairs_hook=_strict_object,
                parse_constant=_reject_json_constant,
            )
            return UpdateManifest.from_dict(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise UpdateClientError(UpdateErrorCode.INVALID_MANIFEST, "manifest is invalid") from error

    def download_installer(self, manifest: UpdateManifest, destination: Path) -> Path:
        destination = Path(destination)
        parent = destination.parent
        try:
            free = shutil.disk_usage(parent).free
        except OSError as error:
            raise UpdateClientError(UpdateErrorCode.FILESYSTEM, "disk space could not be checked") from error
        required = manifest.size + DISK_SPACE_RESERVE_BYTES
        if free < required:
            raise UpdateClientError(
                UpdateErrorCode.INSUFFICIENT_SPACE,
                f"installer requires {required} bytes of available disk space",
            )

        response = self._open_response(
            _request(manifest.installer_url, "application/octet-stream"),
            response_kind="installer",
        )
        temporary: Path | None = None
        descriptor = -1
        try:
            _validate_content_length(response.headers, manifest.size)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".partial", dir=str(parent)
            )
            temporary = Path(temporary_name)
            digest = hashlib.sha256()
            received = 0
            with closing(response), os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                while True:
                    chunk = response.read(self.chunk_size)
                    if not chunk:
                        break
                    if received + len(chunk) > manifest.size:
                        raise UpdateClientError(
                            UpdateErrorCode.SIZE_MISMATCH,
                            f"installer exceeds declared size {manifest.size}",
                        )
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            if received != manifest.size:
                raise UpdateClientError(
                    UpdateErrorCode.SIZE_MISMATCH,
                    f"installer size mismatch: expected {manifest.size}, received {received}",
                )
            if digest.hexdigest() != manifest.sha256:
                raise UpdateClientError(UpdateErrorCode.SHA256_MISMATCH, "installer SHA-256 mismatch")
            os.replace(temporary, destination)
            temporary = None
            return destination
        except UpdateClientError:
            raise
        except TimeoutError as error:
            raise UpdateClientError(UpdateErrorCode.TIMEOUT, "installer download timed out") from error
        except URLError as error:
            raise _url_error(error, "installer download") from error
        except OSError as error:
            raise UpdateClientError(UpdateErrorCode.FILESYSTEM, "installer could not be saved") from error
        finally:
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
            with suppress(OSError):
                response.close()

    def download_installer_result(
        self, manifest: UpdateManifest, destination: Path
    ) -> DownloadResult:
        try:
            path = self.download_installer(manifest, destination)
        except UpdateClientError as error:
            return DownloadResult.failed(manifest, error.code, str(error))
        return DownloadResult.succeeded(manifest, path)

    def _open_response(self, request: Request, *, response_kind: str) -> ReadableResponse:
        try:
            response = (
                self._transport(request, self.timeout)
                if self._transport is not None
                else self._opener.open(request, timeout=self.timeout)
            )
        except HTTPError as error:
            raise _http_error(error.code, response_kind) from error
        except TimeoutError as error:
            raise UpdateClientError(UpdateErrorCode.TIMEOUT, f"{response_kind} request timed out") from error
        except URLError as error:
            raise _url_error(error, f"{response_kind} request") from error
        except OSError as error:
            raise UpdateClientError(UpdateErrorCode.NETWORK, f"{response_kind} request failed") from error
        try:
            _validate_response(response, response_kind)
        except (UpdateClientError, ValueError) as error:
            with suppress(OSError):
                response.close()
            if isinstance(error, UpdateClientError):
                raise
            raise UpdateClientError(UpdateErrorCode.NETWORK, "untrusted final response URL") from error
        return response


def _request(url: str, accept: str) -> Request:
    return Request(
        url,
        headers={"Accept": accept, "User-Agent": "RestreamStudio-Updater/1"},
        method="GET",
    )


def _validate_manifest_url(url: str) -> None:
    try:
        parsed = _parse_trusted_url(url, {_GITHUB_HOST}, allow_query=False)
    except ValueError as error:
        raise ValueError("manifest_url must use trusted repository HTTPS") from error
    if _MANIFEST_PATH_RE.fullmatch(parsed.path) is None:
        raise ValueError("manifest_url must identify latest.json in the configured repository")


def _validate_redirect_url(url: str) -> None:
    parsed = _parse_trusted_url(url, {_GITHUB_HOST, *_ASSET_HOSTS}, allow_query=True)
    if parsed.hostname == _GITHUB_HOST and (
        parsed.query
        or not parsed.path.startswith("/algz-glitch/restream-studio/releases/")
    ):
        raise ValueError("GitHub redirects must remain in the configured repository")


def _validate_response(response: ReadableResponse, response_kind: str) -> None:
    if response.status != 200:
        raise _http_error(response.status, response_kind)
    final_url = response.geturl()
    if response_kind == "manifest":
        _validate_manifest_url(final_url)
    else:
        _validate_installer_response_url(final_url)


def _validate_installer_response_url(url: str) -> None:
    parsed = _parse_trusted_url(url, {_GITHUB_HOST, *_ASSET_HOSTS}, allow_query=True)
    if parsed.hostname == _GITHUB_HOST and (
        parsed.query
        or re.fullmatch(
            r"/algz-glitch/restream-studio/releases/download/[^/]+/"
            r"RestreamStudio-Setup-[^/]+\.exe",
            parsed.path,
        )
        is None
    ):
        raise ValueError("GitHub installer response must remain in the configured repository")


def _parse_trusted_url(url: str, hosts: set[str], *, allow_query: bool) -> SplitResult:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.netloc != parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        raise ValueError("untrusted update URL")
    return parsed


def _validate_content_length(headers: Mapping[str, str], expected: int) -> None:
    value = headers.get("Content-Length")
    if value is None:
        return
    if not value.isascii() or not value.isdecimal():
        raise UpdateClientError(UpdateErrorCode.SIZE_MISMATCH, "invalid installer Content-Length")
    declared = int(value)
    if declared > MAX_INSTALLER_BYTES or declared != expected:
        raise UpdateClientError(UpdateErrorCode.SIZE_MISMATCH, "installer Content-Length mismatch")


def _http_error(status: int, operation: str) -> UpdateClientError:
    code = UpdateErrorCode.RATE_LIMITED if status == 429 else UpdateErrorCode.HTTP_STATUS
    return UpdateClientError(code, f"{operation} returned HTTP {status}")


def _url_error(error: URLError, operation: str) -> UpdateClientError:
    if isinstance(error.reason, TimeoutError):
        return UpdateClientError(UpdateErrorCode.TIMEOUT, f"{operation} timed out")
    return UpdateClientError(UpdateErrorCode.NETWORK, f"{operation} failed")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")
