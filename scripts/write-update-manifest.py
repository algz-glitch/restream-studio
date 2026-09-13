from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from restream_studio.update.contracts import MAX_INSTALLER_BYTES, UpdateManifest

REPOSITORY = "algz-glitch/restream-studio"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a verified update manifest")
    parser.add_argument("--version", required=True)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--verify-existing",
        action="store_true",
        help="verify output against the installer instead of rewriting it",
    )
    return parser.parse_args()


def validate_inputs(arguments: argparse.Namespace) -> tuple[Path, Path]:
    if SEMVER.fullmatch(arguments.version) is None:
        raise ValueError("version must be canonical SemVer major.minor.patch")
    if arguments.repository != REPOSITORY:
        raise ValueError(f"repository must be exactly {REPOSITORY}")
    expected_tag = f"v{arguments.version}"
    if arguments.tag != expected_tag:
        raise ValueError(f"tag must be exactly {expected_tag}")

    installer = arguments.installer.expanduser()
    if installer.is_symlink():
        raise ValueError("installer must not be a symbolic link")
    try:
        installer = installer.resolve(strict=True)
    except OSError as error:
        raise ValueError("installer must be an existing regular file") from error
    if not installer.is_file():
        raise ValueError("installer must be an existing regular file")
    expected_name = f"RestreamStudio-Setup-{arguments.version}.exe"
    if installer.name != expected_name:
        raise ValueError(f"installer filename must be exactly {expected_name}")

    output_argument = arguments.output.expanduser()
    if output_argument.name != "latest.json":
        raise ValueError("output filename must be exactly latest.json")
    if output_argument.is_symlink():
        raise ValueError("output must not be a symbolic link")
    try:
        output_parent = output_argument.parent.resolve(strict=True)
    except OSError as error:
        raise ValueError("output directory must already exist") from error
    if not output_parent.is_dir():
        raise ValueError("output directory must already exist")
    output = output_parent / "latest.json"
    if output.exists() and not output.is_file():
        raise ValueError("output must be a regular file")
    if output == installer:
        raise ValueError("output must not overwrite the installer")
    return installer, output


def inspect_installer(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("installer must be an existing regular file")
        if metadata.st_size <= 0:
            raise ValueError("installer must not be empty")
        if metadata.st_size > MAX_INSTALLER_BYTES:
            raise ValueError("installer exceeds the maximum installer size")
        bytes_read = 0
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            bytes_read += len(block)
            digest.update(block)
        final_metadata = os.fstat(stream.fileno())
        if (
            bytes_read != metadata.st_size
            or final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise ValueError("installer changed while it was being inspected")
    return metadata.st_size, digest.hexdigest().upper()


def expected_manifest(arguments: argparse.Namespace, installer: Path) -> dict[str, object]:
    size, digest = inspect_installer(installer)
    repository = arguments.repository
    tag = arguments.tag
    return {
        "schema_version": 1,
        "version": arguments.version,
        "installer_url": (
            f"https://github.com/{repository}/releases/download/{tag}/{installer.name}"
        ),
        "sha256": digest,
        "size": size,
        "published_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "release_url": f"https://github.com/{repository}/releases/tag/{tag}",
    }


def write_manifest(arguments: argparse.Namespace) -> Path:
    installer, output = validate_inputs(arguments)
    manifest = expected_manifest(arguments, installer)
    UpdateManifest.from_dict(manifest)

    if arguments.verify_existing:
        try:
            raw = output.read_text(encoding="utf-8")
            existing = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("existing manifest is not valid UTF-8 JSON") from error
        parsed = UpdateManifest.from_dict(existing)
        published_at = existing.get("published_at")
        if not isinstance(published_at, str) or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", published_at
        ) is None:
            raise ValueError("existing manifest published_at must be exact UTC seconds")
        for key in ("schema_version", "version", "installer_url", "sha256", "size", "release_url"):
            if existing.get(key) != manifest[key]:
                raise ValueError(f"existing manifest {key} does not match installer identity")
        # Parsing enforces the exact schema and canonical UTC timestamp contract.
        del parsed
        return output

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".latest.json.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(manifest, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, output)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return output


def main() -> int:
    try:
        output = write_manifest(parse_arguments())
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"MANIFEST_PATH={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
