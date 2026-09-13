from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from .contracts import UpdateCheckResult, UpdateErrorCode, UpdateManifest, Version

DEFAULT_MANIFEST_URL = (
    "https://github.com/algz-glitch/restream-studio/releases/latest/download/latest.json"
)
MAX_MANIFEST_BYTES = 1024 * 1024
DEFAULT_TIMEOUT = 10.0
DEFAULT_CHUNK_SIZE = 64 * 1024


class ReadableResponse(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class Opener(Protocol):
    def open(self, request: Request, timeout: float) -> ReadableResponse: ...


Transport = Callable[[Request, float], ReadableResponse]


class UpdateClientError(RuntimeError):
    def __init__(self, code: UpdateErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class _DuplicateKeyError(ValueError):
    pass


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
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.manifest_url = manifest_url
        self._opener = opener or cast(Opener, build_opener())
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
        if manifest.version <= current:
            return UpdateCheckResult.current()
        return UpdateCheckResult.available(manifest)

    def fetch_manifest(self) -> UpdateManifest:
        request = Request(
            self.manifest_url,
            headers={"Accept": "application/json", "User-Agent": "RestreamStudio-Updater/1"},
            method="GET",
        )
        try:
            body = bytearray()
            with closing(self._open(request)) as response:
                while len(body) <= MAX_MANIFEST_BYTES:
                    chunk = response.read(MAX_MANIFEST_BYTES + 1 - len(body))
                    if not chunk:
                        break
                    body.extend(chunk)
        except TimeoutError as error:
            raise UpdateClientError(UpdateErrorCode.TIMEOUT, "manifest request timed out") from error
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise UpdateClientError(
                    UpdateErrorCode.TIMEOUT, "manifest request timed out"
                ) from error
            raise UpdateClientError(UpdateErrorCode.NETWORK, "manifest request failed") from error
        except (HTTPError, OSError) as error:
            raise UpdateClientError(UpdateErrorCode.NETWORK, "manifest request failed") from error
        except Exception as error:
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
        partial = destination.with_name(destination.name + ".partial")
        request = Request(
            manifest.installer_url,
            headers={"Accept": "application/octet-stream", "User-Agent": "RestreamStudio-Updater/1"},
            method="GET",
        )
        digest = hashlib.sha256()
        received = 0
        try:
            partial.unlink(missing_ok=True)
            with (
                closing(self._open(request)) as response,
                partial.open("wb") as output,
            ):
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
                raise UpdateClientError(
                    UpdateErrorCode.SHA256_MISMATCH, "installer SHA-256 mismatch"
                )
            os.replace(partial, destination)
            return destination
        except UpdateClientError:
            partial.unlink(missing_ok=True)
            raise
        except TimeoutError as error:
            partial.unlink(missing_ok=True)
            raise UpdateClientError(UpdateErrorCode.TIMEOUT, "installer download timed out") from error
        except (HTTPError, URLError) as error:
            partial.unlink(missing_ok=True)
            raise UpdateClientError(UpdateErrorCode.DOWNLOAD_FAILED, "installer download failed") from error
        except OSError as error:
            partial.unlink(missing_ok=True)
            raise UpdateClientError(UpdateErrorCode.FILESYSTEM, "installer could not be saved") from error
        except Exception as error:
            partial.unlink(missing_ok=True)
            raise UpdateClientError(UpdateErrorCode.DOWNLOAD_FAILED, "installer download failed") from error

    def download_installer_result(
        self, manifest: UpdateManifest, destination: Path
    ) -> UpdateCheckResult:
        try:
            self.download_installer(manifest, destination)
        except UpdateClientError as error:
            return UpdateCheckResult.failed(error.code, str(error))
        return UpdateCheckResult.current()

    def _open(self, request: Request) -> ReadableResponse:
        if self._transport is not None:
            return self._transport(request, self.timeout)
        return self._opener.open(request, timeout=self.timeout)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")
