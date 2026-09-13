from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
MANIFEST_FIELDS = {"schema_version", "kind", "commit", "version", "files"}
FILE_FIELDS = {"path", "size", "sha256"}


@dataclass(frozen=True, slots=True)
class Arguments:
    root: Path
    kind: Literal["frontend", "package"]
    commit: str
    version: str
    output: Path
    verify_existing: bool


def parse_arguments() -> Arguments:
    parser = argparse.ArgumentParser(description="Write or verify a complete tree manifest")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--kind", choices=("frontend", "package"), required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-existing", action="store_true")
    namespace = parser.parse_args()
    kind = str(namespace.kind)
    if kind not in ("frontend", "package"):
        parser.error("kind must be frontend or package")
    return Arguments(
        root=Path(namespace.root),
        kind=cast(Literal["frontend", "package"], kind),
        commit=str(namespace.commit),
        version=str(namespace.version),
        output=Path(namespace.output),
        verify_existing=bool(namespace.verify_existing),
    )


def is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def inspect_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"manifest entry is not a regular file: {path}")
        read = 0
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            read += len(block)
            digest.update(block)
        after = os.fstat(stream.fileno())
        if (
            read != before.st_size
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise ValueError(f"manifest entry changed while hashing: {path}")
    return before.st_size, digest.hexdigest().upper()


def build_manifest(arguments: Arguments) -> dict[str, object]:
    if COMMIT.fullmatch(arguments.commit) is None:
        raise ValueError("commit must be exactly 40 lowercase hexadecimal characters")
    if SEMVER.fullmatch(arguments.version) is None:
        raise ValueError("version must be canonical SemVer major.minor.patch")
    if is_link(arguments.root):
        raise ValueError("root must be a real directory")
    root = arguments.root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("root must be a real directory")
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if is_link(path):
            raise ValueError(f"tree must not contain links or junctions: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            raise ValueError("manifest contains an unsafe relative path")
        size, digest = inspect_file(path)
        files.append({"path": relative, "size": size, "sha256": digest})
    if not files:
        raise ValueError("manifest tree must contain files")
    return {
        "schema_version": 1,
        "kind": arguments.kind,
        "commit": arguments.commit,
        "version": arguments.version,
        "files": files,
    }


def validate_schema(payload: object) -> None:
    if not isinstance(payload, dict) or set(payload) != MANIFEST_FIELDS:
        raise ValueError("tree manifest has unexpected fields")
    if payload["schema_version"] != 1 or type(payload["schema_version"]) is not int:
        raise ValueError("tree manifest schema_version must be 1")
    if payload["kind"] not in ("frontend", "package"):
        raise ValueError("tree manifest kind is invalid")
    if not isinstance(payload["commit"], str) or COMMIT.fullmatch(payload["commit"]) is None:
        raise ValueError("tree manifest commit is invalid")
    if not isinstance(payload["version"], str) or SEMVER.fullmatch(payload["version"]) is None:
        raise ValueError("tree manifest version is invalid")
    files = payload["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("tree manifest files must be a non-empty list")
    paths: list[str] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != FILE_FIELDS:
            raise ValueError("tree manifest file entry has unexpected fields")
        path = item["path"]
        if (
            not isinstance(path, str)
            or not path
            or "\\" in path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            raise ValueError("tree manifest file path is unsafe")
        if type(item["size"]) is not int or item["size"] < 0:
            raise ValueError("tree manifest file size is invalid")
        if (
            not isinstance(item["sha256"], str)
            or re.fullmatch(r"[0-9A-F]{64}", item["sha256"]) is None
        ):
            raise ValueError("tree manifest file hash is invalid")
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("tree manifest paths must be unique and sorted")


def execute(arguments: Arguments) -> Path:
    expected = build_manifest(arguments)
    validate_schema(expected)
    output_parent = arguments.output.parent.resolve(strict=True)
    output = output_parent / arguments.output.name
    if output.is_symlink() or output.name not in (
        "frontend-build-manifest.json",
        "package-manifest.json",
    ):
        raise ValueError("output must be a canonical tree manifest filename")
    if arguments.verify_existing:
        try:
            existing = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("existing tree manifest is invalid UTF-8 JSON") from error
        validate_schema(existing)
        if existing != expected:
            raise ValueError("tree manifest file set or content does not match")
        return output
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=output_parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = stream.name
            json.dump(expected, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return output


def main() -> int:
    try:
        output = execute(parse_arguments())
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"TREE_MANIFEST_PATH={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
