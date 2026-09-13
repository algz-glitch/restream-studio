from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "write-tree-manifest.py"


def run_manifest(root: Path, output: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(root),
            "--kind",
            "package",
            "--commit",
            "a" * 40,
            "--version",
            "1.2.3",
            "--output",
            str(output),
            *extra,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_tree_manifest_binds_identity_and_complete_file_set(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_bytes(b"a")
    nested = tree / "nested"
    nested.mkdir()
    (nested / "b.bin").write_bytes(b"bb")
    output = tmp_path / "package-manifest.json"

    result = run_manifest(tree, output)
    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert list(payload) == ["schema_version", "kind", "commit", "version", "files"]
    assert payload["commit"] == "a" * 40
    assert payload["version"] == "1.2.3"
    assert [item["path"] for item in payload["files"]] == ["a.txt", "nested/b.bin"]
    assert run_manifest(tree, output, "--verify-existing").returncode == 0

    (tree / "extra.txt").write_text("extra", encoding="utf-8")
    drift = run_manifest(tree, output, "--verify-existing")
    assert drift.returncode != 0
    assert "file set or content" in drift.stderr

    (tree / "extra.txt").unlink()
    (tree / "a.txt").unlink()
    missing = run_manifest(tree, output, "--verify-existing")
    assert missing.returncode != 0
    assert "file set or content" in missing.stderr

    (tree / "a.txt").write_bytes(b"a")
    (nested / "b.bin").write_bytes(b"changed")
    changed = run_manifest(tree, output, "--verify-existing")
    assert changed.returncode != 0
    assert "file set or content" in changed.stderr
