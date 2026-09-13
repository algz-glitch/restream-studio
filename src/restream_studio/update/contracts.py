from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self
from urllib.parse import urlsplit

_VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "version",
        "installer_url",
        "sha256",
        "size",
        "published_at",
        "release_url",
    }
)


@dataclass(frozen=True, order=True, slots=True)
class Version:
    major: int
    minor: int
    patch: int

    def __post_init__(self) -> None:
        for part in (self.major, self.minor, self.patch):
            if type(part) is not int or part < 0:
                raise ValueError("version components must be non-negative integers")

    @classmethod
    def parse(cls, value: str) -> Self:
        if not isinstance(value, str):
            raise TypeError("version must be a string")
        match = _VERSION_RE.fullmatch(value)
        if match is None:
            raise ValueError("version must be canonical SemVer major.minor.patch")
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


class UpdateErrorCode(StrEnum):
    NETWORK = "network"
    TIMEOUT = "timeout"
    MANIFEST_TOO_LARGE = "manifest_too_large"
    INVALID_MANIFEST = "invalid_manifest"
    INVALID_CURRENT_VERSION = "invalid_current_version"
    DOWNLOAD_FAILED = "download_failed"
    SIZE_MISMATCH = "size_mismatch"
    SHA256_MISMATCH = "sha256_mismatch"
    FILESYSTEM = "filesystem"


@dataclass(frozen=True, slots=True)
class UpdateManifest:
    schema_version: int
    version: Version
    installer_url: str
    sha256: str
    size: int
    published_at: datetime
    release_url: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        if not isinstance(self.version, Version):
            raise TypeError("version must be a Version")
        if not isinstance(self.installer_url, str):
            raise TypeError("installer_url must be a string")
        if not isinstance(self.release_url, str):
            raise TypeError("release_url must be a string")
        if not isinstance(self.sha256, str) or _SHA256_RE.fullmatch(self.sha256) is None:
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        if type(self.size) is not int or self.size <= 0:
            raise ValueError("size must be a positive integer")
        if not isinstance(self.published_at, datetime):
            raise TypeError("published_at must be a datetime")
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("published_at must include a timezone")
        _validate_release_urls(self.installer_url, self.release_url, self.version)
        object.__setattr__(self, "sha256", self.sha256.lower())

    @classmethod
    def from_dict(cls, payload: object) -> Self:
        if not isinstance(payload, dict) or set(payload) != _MANIFEST_FIELDS:
            raise ValueError("manifest fields must exactly match schema version 1")

        schema_version = payload["schema_version"]
        if type(schema_version) is not int or schema_version != 1:
            raise ValueError("schema_version must be 1")
        version = Version.parse(_require_string(payload, "version"))
        sha256 = _require_string(payload, "sha256")
        if _SHA256_RE.fullmatch(sha256) is None:
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        size = payload["size"]
        if type(size) is not int or size <= 0:
            raise ValueError("size must be a positive integer")
        published_at = _parse_published_at(_require_string(payload, "published_at"))
        installer_url = _require_string(payload, "installer_url")
        release_url = _require_string(payload, "release_url")
        _validate_release_urls(installer_url, release_url, version)
        return cls(
            schema_version=schema_version,
            version=version,
            installer_url=installer_url,
            sha256=sha256.lower(),
            size=size,
            published_at=published_at,
            release_url=release_url,
        )


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    update_available: bool
    manifest: UpdateManifest | None = None
    error_code: UpdateErrorCode | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.update_available != (self.manifest is not None):
            raise ValueError("available results must contain exactly one manifest")
        if (self.error_code is None) != (self.error_message is None):
            raise ValueError("error code and message must be provided together")
        if self.update_available and self.error_code is not None:
            raise ValueError("available results cannot contain errors")

    @classmethod
    def available(cls, manifest: UpdateManifest) -> Self:
        return cls(update_available=True, manifest=manifest)

    @classmethod
    def current(cls) -> Self:
        return cls(update_available=False)

    @classmethod
    def failed(cls, code: UpdateErrorCode, message: str) -> Self:
        if not message:
            raise ValueError("error message must not be empty")
        return cls(update_available=False, error_code=code, error_message=message)


def _require_string(payload: dict[object, object], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _parse_published_at(value: str) -> datetime:
    if "T" not in value:
        raise ValueError("published_at must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("published_at must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("published_at must include a timezone")
    return parsed


def _validate_release_urls(installer_url: str, release_url: str, version: Version) -> None:
    installer = urlsplit(installer_url)
    release = urlsplit(release_url)
    for field, value in (("installer_url", installer), ("release_url", release)):
        if (
            value.scheme != "https"
            or value.netloc != "github.com"
            or value.username is not None
            or value.password is not None
            or value.port is not None
            or value.query
            or value.fragment
        ):
            raise ValueError(f"{field} must be a trusted GitHub HTTPS URL")

    version_text = str(version)
    installer_match = re.fullmatch(
        rf"/algz-glitch/restream-studio/releases/download/([^/]+)/"
        rf"RestreamStudio-Setup-{re.escape(version_text)}\.exe",
        installer.path,
    )
    if installer_match is None:
        raise ValueError("installer_url does not match the release installer contract")
    tag = installer_match.group(1)
    if release.path != f"/algz-glitch/restream-studio/releases/tag/{tag}":
        raise ValueError("release_url must identify the corresponding repository tag")
