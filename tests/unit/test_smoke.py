import json
import tomllib
from pathlib import Path

import pytest
import uvicorn


def test_package_exposes_version() -> None:
    import restream_studio

    root = Path(__file__).parents[2]
    with (root / "pyproject.toml").open("rb") as pyproject_file:
        pyproject_version = tomllib.load(pyproject_file)["project"]["version"]
    with (root / "package.json").open(encoding="utf-8") as package_file:
        package_version = json.load(package_file)["version"]

    assert restream_studio.__version__ == pyproject_version == package_version == "0.1.4"


def test_runtime_version_uses_valid_bundled_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import restream_studio

    metadata = tmp_path / "version.txt"
    metadata.write_text("0.1.4", encoding="ascii")
    monkeypatch.setattr(restream_studio, "_BUNDLED_VERSION", metadata)

    assert restream_studio._runtime_version() == "0.1.4"


def test_runtime_version_rejects_invalid_bundled_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import restream_studio

    metadata = tmp_path / "version.txt"
    metadata.write_text("0.1.4-dev", encoding="ascii")
    monkeypatch.setattr(restream_studio, "_BUNDLED_VERSION", metadata)

    with pytest.raises(RuntimeError, match="bundled version metadata is invalid"):
        restream_studio._runtime_version()


def test_run_passes_application_object_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from restream_studio import main

    captured: dict[str, object] = {}

    def fake_run(application: object, **configuration: object) -> None:
        captured["application"] = application
        captured.update(configuration)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setenv("RESTREAM_STUDIO_PORT", "49152")

    main.run()

    assert captured == {
        "application": main.app,
        "host": "127.0.0.1",
        "port": 49152,
        "proxy_headers": False,
    }
