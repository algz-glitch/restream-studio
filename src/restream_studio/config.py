"""Portable local filesystem configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigPathError(RuntimeError):
    """Raised when application storage paths cannot be resolved or created."""


@dataclass(frozen=True, slots=True)
class AppPaths:
    base_dir: Path
    data_dir: Path
    log_dir: Path
    standby_dir: Path
    database_file: Path

    @classmethod
    def create(cls, base_dir: Path | str | None = None) -> AppPaths:
        if base_dir is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                raise ConfigPathError("LOCALAPPDATA is not configured")
            base = Path(local_app_data) / "RestreamStudio"
        else:
            base = Path(base_dir)
        try:
            base = base.expanduser().resolve(strict=False)
            data = (base / "data").resolve(strict=False)
            logs = (base / "logs").resolve(strict=False)
            standby = (data / "standby").resolve(strict=False)
            for directory in (data, logs, standby):
                directory.mkdir(parents=True, exist_ok=True)
            if not all(directory.is_dir() for directory in (data, logs, standby)):
                raise OSError("configured application path is not a directory")
        except (OSError, RuntimeError) as exc:
            raise ConfigPathError(f"Unable to initialize application paths: {exc}") from exc
        return cls(
            base_dir=base,
            data_dir=data,
            log_dir=logs,
            standby_dir=standby,
            database_file=(data / "restream-studio.sqlite3").resolve(strict=False),
        )
