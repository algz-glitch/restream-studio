from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from restream_studio.update.contracts import (
    MAX_INSTALLER_BYTES,
    DownloadResult,
    DownloadStatus,
    UpdateCheckResult,
    UpdateErrorCode,
    UpdateManifest,
    Version,
)


def manifest_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "version": "1.2.3",
        "installer_url": (
            "https://github.com/algz-glitch/restream-studio/releases/download/"
            "v1.2.3/RestreamStudio-Setup-1.2.3.exe"
        ),
        "sha256": "a" * 64,
        "size": 42,
        "published_at": "2026-09-13T12:30:00Z",
        "release_url": (
            "https://github.com/algz-glitch/restream-studio/releases/tag/v1.2.3"
        ),
    }
    payload.update(changes)
    return payload


def test_version_is_strict_immutable_and_sortable() -> None:
    versions = [Version.parse("2.0.0"), Version.parse("1.10.0"), Version.parse("1.2.9")]
    assert [str(item) for item in sorted(versions)] == ["1.2.9", "1.10.0", "2.0.0"]
    with pytest.raises(FrozenInstanceError):
        versions[0].major = 9  # type: ignore[misc]


@pytest.mark.parametrize(
    "value",
    ["01.2.3", "1.02.3", "1.2.03", "1.2", "v1.2.3", "1.2.3-rc.1", "1.2.3+build"],
)
def test_version_rejects_noncanonical_semver(value: str) -> None:
    with pytest.raises(ValueError, match="version"):
        Version.parse(value)


def test_manifest_accepts_exact_schema_and_normalizes_contract_types() -> None:
    manifest = UpdateManifest.from_dict(manifest_payload())
    assert manifest.version == Version(1, 2, 3)
    assert manifest.published_at == datetime(2026, 9, 13, 12, 30, tzinfo=UTC)
    assert manifest.sha256 == "a" * 64


def test_manifest_constructor_enforces_invariants() -> None:
    valid = UpdateManifest.from_dict(manifest_payload())
    with pytest.raises(ValueError, match="schema_version"):
        UpdateManifest(
            schema_version=2,
            version=valid.version,
            installer_url=valid.installer_url,
            sha256=valid.sha256,
            size=valid.size,
            published_at=valid.published_at,
            release_url=valid.release_url,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("sha256", "a" * 63),
        ("sha256", "g" * 64),
        ("size", 0),
        ("size", -1),
        ("published_at", "2026-09-13"),
    ],
)
def test_manifest_rejects_invalid_scalar_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        UpdateManifest.from_dict(manifest_payload(**{field: value}))


def test_manifest_rejects_installer_larger_than_hard_limit() -> None:
    assert MAX_INSTALLER_BYTES == 512 * 1024 * 1024
    with pytest.raises(ValueError, match="maximum"):
        UpdateManifest.from_dict(manifest_payload(size=MAX_INSTALLER_BYTES + 1))


def test_manifest_rejects_unknown_or_missing_fields() -> None:
    with pytest.raises(ValueError, match="fields"):
        UpdateManifest.from_dict(manifest_payload(extra=True))
    payload = manifest_payload()
    del payload["size"]
    with pytest.raises(ValueError, match="fields"):
        UpdateManifest.from_dict(payload)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/algz-glitch/restream-studio/releases/download/v1.2.3/RestreamStudio-Setup-1.2.3.exe",
        "https://evil.test/algz-glitch/restream-studio/releases/download/v1.2.3/RestreamStudio-Setup-1.2.3.exe",
        "https://user@github.com/algz-glitch/restream-studio/releases/download/v1.2.3/RestreamStudio-Setup-1.2.3.exe",
        "https://github.com/algz-glitch/restream-studio/releases/download/v1.2.3/RestreamStudio-Setup-1.2.3.exe?q=1",
        "https://github.com/algz-glitch/restream-studio/releases/download/v1.2.3/RestreamStudio-Setup-1.2.4.exe",
    ],
)
def test_manifest_rejects_untrusted_installer_urls(url: str) -> None:
    with pytest.raises(ValueError, match="installer_url"):
        UpdateManifest.from_dict(manifest_payload(installer_url=url))


def test_manifest_allows_release_tag_names_when_both_urls_identify_the_same_tag() -> None:
    manifest = UpdateManifest.from_dict(
        manifest_payload(
            installer_url=(
                "https://github.com/algz-glitch/restream-studio/releases/download/"
                "stable-1/RestreamStudio-Setup-1.2.3.exe"
            ),
            release_url=(
                "https://github.com/algz-glitch/restream-studio/releases/tag/stable-1"
            ),
        )
    )
    assert manifest.version == Version(1, 2, 3)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/algz-glitch/restream-studio/releases/tag/v1.2.3",
        "https://github.com/other/restream-studio/releases/tag/v1.2.3",
        "https://github.com/algz-glitch/restream-studio/releases/tag/v1.2.3#notes",
        "https://github.com/algz-glitch/restream-studio/releases/tag/v1.2.4",
    ],
)
def test_manifest_rejects_untrusted_release_urls(url: str) -> None:
    with pytest.raises(ValueError, match="release_url"):
        UpdateManifest.from_dict(manifest_payload(release_url=url))


def test_update_result_has_typed_success_and_failure_states() -> None:
    manifest = UpdateManifest.from_dict(manifest_payload())
    available = UpdateCheckResult.available(manifest)
    current = UpdateCheckResult.current()
    failed = UpdateCheckResult.failed(UpdateErrorCode.NETWORK, "offline")
    assert available.update_available and available.manifest is manifest
    assert not current.update_available and current.error_code is None
    assert failed.error_code is UpdateErrorCode.NETWORK and failed.error_message == "offline"


def test_download_result_has_independent_success_and_failure_states() -> None:
    manifest = UpdateManifest.from_dict(manifest_payload())
    path = Path("installer.exe")
    success = DownloadResult.succeeded(manifest, path)
    failed = DownloadResult.failed(manifest, UpdateErrorCode.TIMEOUT, "timed out")
    assert success.status is DownloadStatus.SUCCESS and success.path == path
    assert failed.status is DownloadStatus.FAILED and failed.path is None
    assert failed.error_code is UpdateErrorCode.TIMEOUT
