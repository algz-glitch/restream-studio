import json
import tomllib
from pathlib import Path

import pytest


def test_package_exposes_version() -> None:
    import restream_studio

    root = Path(__file__).parents[2]
    with (root / "pyproject.toml").open("rb") as pyproject_file:
        pyproject_version = tomllib.load(pyproject_file)["project"]["version"]
    with (root / "package.json").open(encoding="utf-8") as package_file:
        package_version = json.load(package_file)["version"]

    assert restream_studio.__version__ == pyproject_version == package_version == "0.1.0"


def test_run_passes_application_object_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from restream_studio import main

    captured: dict[str, object] = {}

    def fake_run(application: object, **configuration: object) -> None:
        captured["application"] = application
        captured.update(configuration)

    monkeypatch.setattr(main.uvicorn, "run", fake_run)
    monkeypatch.setenv("RESTREAM_STUDIO_PORT", "49152")

    main.run()

    assert captured == {
        "application": main.app,
        "host": "127.0.0.1",
        "port": 49152,
        "proxy_headers": False,
    }
