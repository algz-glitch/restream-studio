import json
import tomllib
from pathlib import Path


def test_package_exposes_version() -> None:
    import restream_studio

    root = Path(__file__).parents[2]
    with (root / "pyproject.toml").open("rb") as pyproject_file:
        pyproject_version = tomllib.load(pyproject_file)["project"]["version"]
    with (root / "package.json").open(encoding="utf-8") as package_file:
        package_version = json.load(package_file)["version"]

    assert restream_studio.__version__ == pyproject_version == package_version == "0.1.0"
