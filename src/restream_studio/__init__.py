"""Restream Studio package."""

import re
from pathlib import Path

_SOURCE_VERSION = "0.1.5"
_BUNDLED_VERSION = Path(__file__).with_name("version.txt")


def _runtime_version() -> str:
    if not _BUNDLED_VERSION.is_file():
        return _SOURCE_VERSION
    value = _BUNDLED_VERSION.read_text(encoding="ascii").strip()
    if re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value) is None:
        raise RuntimeError("bundled version metadata is invalid")
    return value


__version__ = _runtime_version()
