from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


ROOT = Path(SPEC).resolve().parents[1]
STATIC = ROOT / "src/restream_studio/static"
PACKAGE_INPUT = Path(os.environ.get("RESTREAM_STUDIO_PACKAGE_INPUT", ""))

if not (STATIC / "index.html").is_file():
    raise RuntimeError(
        "frontend build output is missing: run npm ci and npm run frontend:build first"
    )
if not PACKAGE_INPUT.is_dir():
    raise RuntimeError("RESTREAM_STUDIO_PACKAGE_INPUT is missing")

ffmpeg = PACKAGE_INPUT / "ffmpeg.exe"
ffprobe = PACKAGE_INPUT / "ffprobe.exe"
standby = PACKAGE_INPUT / "default-standby.mp4"
licenses = PACKAGE_INPUT / "licenses"
for required in (ffmpeg, ffprobe, standby, licenses):
    if not required.exists():
        raise RuntimeError(f"required package input is missing: {required.name}")

metadata_files = ("README.md", "pyproject.toml", "package.json", "package-lock.json")
for filename in metadata_files:
    if not (ROOT / filename).is_file():
        raise RuntimeError(f"required application metadata is missing: {filename}")

datas = [
    (str(STATIC), "restream_studio/static"),
    (str(standby), "defaults"),
    (str(licenses), "licenses"),
    *((str(ROOT / filename), "metadata") for filename in metadata_files),
]
datas += collect_data_files("streamget")
for distribution in (
    "cryptography",
    "fastapi",
    "httpx",
    "pydantic",
    "streamget",
    "uvicorn",
):
    datas += copy_metadata(distribution)

analysis = Analysis(
    [str(ROOT / "src" / "restream_studio" / "main.py")],
    pathex=[str(ROOT / "src")],
    binaries=[(str(ffmpeg), "."), (str(ffprobe), ".")],
    datas=datas,
    hiddenimports=collect_submodules("streamget"),
    excludes=["pytest", "mypy", "ruff", "playwright", "tests"],
    noarchive=False,
)
helper_analysis = Analysis(
    [str(ROOT / "packaging" / "update-helper-entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=["pytest", "mypy", "ruff", "playwright", "tests"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="RestreamStudio",
    console=True,
    disable_windowed_traceback=False,
    contents_directory="_internal",
)
helper_pyz = PYZ(helper_analysis.pure)
helper_exe = EXE(
    helper_pyz,
    helper_analysis.scripts,
    helper_analysis.binaries,
    helper_analysis.datas,
    [],
    name="RestreamStudioUpdateHelper",
    console=False,
    disable_windowed_traceback=False,
    contents_directory="_internal",
)
distribution = COLLECT(
    exe,
    helper_exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="RestreamStudio",
)
