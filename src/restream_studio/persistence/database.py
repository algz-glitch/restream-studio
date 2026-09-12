"""Transactional SQLite persistence with a strict secret boundary."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self, cast
from urllib.parse import urlsplit

from restream_studio.config import AppPaths
from restream_studio.domain import DestinationKind
from restream_studio.orchestration.controller import PersistedControllerState
from restream_studio.security.redaction import redact
from restream_studio.security.secrets import decrypt_secret, encrypt_secret
from restream_studio.source import normalize_douyin_url

_BUSY_TIMEOUT_MS: Final = 5_000
_EVENT_LIMIT: Final = 5_000
_SAFE_EVENT_NAME: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_URL_IN_TEXT: Final = re.compile(r"(?i)\b(?:https?|rtmps?)://[^\s\"'<>]+")
_KIND_TO_STORAGE: Final = {
    DestinationKind.DOUYIN: "douyin",
    DestinationKind.WECHAT: "wechat_channels",
    DestinationKind.LOCAL_TEST: "local_test",
}
_STORAGE_TO_KIND: Final = {value: key for key, value in _KIND_TO_STORAGE.items()}


class DatabaseError(RuntimeError):
    """Base class for persistence failures."""


class DatabaseClosedError(DatabaseError):
    """Raised when an operation is attempted without an open connection."""


class DatabaseCorruptError(DatabaseError):
    """Raised when SQLite reports malformed or inconsistent storage."""


class MigrationError(DatabaseError):
    """Raised after an atomic schema migration is rolled back."""


class DestinationSecretError(DatabaseError, ValueError):
    """Raised when a destination credential is empty or unreadable."""


class PathPolicyError(DatabaseError, ValueError):
    """Raised when standby media is outside trusted local roots."""


@dataclass(frozen=True, slots=True)
class SourceConfig:
    room_identity: str
    preferred_quality: str | None
    desired_running: bool


@dataclass(frozen=True, slots=True)
class DestinationConfig:
    kind: DestinationKind
    base_server: str
    enabled: bool
    configured: bool


@dataclass(frozen=True, slots=True)
class RuntimeDestination:
    kind: DestinationKind
    base_server: str
    stream_key: str = dataclass_field(repr=False)
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class AppSettings:
    standby_file: Path | None = None
    backoff_initial_seconds: float = 2.0
    backoff_max_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: int
    created_at: str
    level: str
    event_type: str
    payload: dict[str, object]


Migration = Callable[[sqlite3.Connection], None]


def _migration_1(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE source_config ("
        "singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1), "
        "room_identity TEXT NOT NULL CHECK("
        "(room_identity LIKE 'https://live.douyin.com/%' OR "
        "room_identity LIKE 'https://v.douyin.com/%') AND "
        "instr(room_identity, '?') = 0 AND instr(room_identity, '#') = 0 AND "
        "lower(room_identity) NOT LIKE '%.flv%' AND lower(room_identity) NOT LIKE '%.m3u8%'), "
        "preferred_quality TEXT, "
        "desired_running INTEGER NOT NULL CHECK(desired_running IN (0, 1)), "
        "enabled_destinations_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE destination_config ("
        "kind TEXT PRIMARY KEY CHECK(kind IN ('douyin','wechat_channels','local_test')), "
        "base_server TEXT NOT NULL, encrypted_stream_key TEXT NOT NULL CHECK(length(encrypted_stream_key) > 0), "
        "enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)), updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE app_settings ("
        "singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1), standby_file TEXT, "
        "backoff_initial_seconds REAL NOT NULL, backoff_max_seconds REAL NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
        "level TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL)"
    )


MIGRATIONS: tuple[Migration, ...] = (_migration_1,)


class Database:
    """One process-local SQLite owner safe for concurrent writer threads."""

    def __init__(
        self,
        path: Path | str,
        *,
        paths: AppPaths,
        selected_standby_roots: Sequence[Path | str] = (),
        encrypt_secret: Callable[[str], str] = encrypt_secret,
        decrypt_secret: Callable[[str], str] = decrypt_secret,
        busy_timeout_ms: int = _BUSY_TIMEOUT_MS,
    ) -> None:
        self._path = Path(path).resolve(strict=False)
        self._paths = paths
        self._trusted_roots = tuple(
            root.resolve(strict=False)
            for root in (paths.standby_dir, *(Path(item) for item in selected_standby_roots))
        )
        self._encrypt_secret = encrypt_secret
        self._decrypt_secret = decrypt_secret
        self._busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def open(self) -> Self:
        with self._lock:
            if self._connection is not None:
                return self
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    self._path,
                    timeout=self._busy_timeout_ms / 1_000,
                    isolation_level=None,
                    check_same_thread=False,
                )
                connection.row_factory = sqlite3.Row
                connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
                integrity = cast(str, connection.execute("PRAGMA quick_check").fetchone()[0])
                if integrity != "ok":
                    raise DatabaseCorruptError("SQLite integrity check failed")
                self._connection = connection
                self._migrate()
            except DatabaseCorruptError:
                if connection is not None:
                    connection.close()
                self._connection = None
                raise
            except sqlite3.DatabaseError as exc:
                if connection is not None:
                    connection.close()
                self._connection = None
                raise DatabaseCorruptError("Unable to open SQLite database") from exc
            except BaseException:
                if connection is not None:
                    connection.close()
                self._connection = None
                raise
            return self

    def close(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                connection.close()

    @property
    def schema_version(self) -> int:
        connection = self._require_connection()
        with self._lock:
            row = connection.execute("SELECT max(version) FROM schema_version").fetchone()
            return int(row[0] or 0)

    def pragma(self, name: str) -> str | int:
        if name not in {"journal_mode", "foreign_keys", "busy_timeout"}:
            raise ValueError("Unsupported pragma")
        with self._lock:
            value = self._require_connection().execute(f"PRAGMA {name}").fetchone()[0]
        return cast(str | int, value)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise DatabaseClosedError("Database connection is closed")
        return self._connection

    def _migrate(self) -> None:
        connection = self._require_connection()
        has_version_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        current = 0
        if has_version_table:
            current = int(
                connection.execute(
                    "SELECT coalesce(max(version), 0) FROM schema_version"
                ).fetchone()[0]
            )
        if current > len(MIGRATIONS):
            raise MigrationError("Database schema is newer than this application")
        for version, migration in enumerate(MIGRATIONS[current:], start=current + 1):
            try:
                connection.execute("BEGIN IMMEDIATE")
                migration(connection)
                connection.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(?, ?)",
                    (version, _now()),
                )
                connection.commit()
            except BaseException as exc:
                connection.rollback()
                raise MigrationError(f"Migration {version} failed") from exc

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def execute_for_test(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._require_connection().execute(sql, parameters)

    def set_source(
        self, room_identity: str, preferred_quality: str | None, desired_running: bool
    ) -> None:
        canonical = normalize_douyin_url(room_identity)
        quality = preferred_quality.strip() if preferred_quality is not None else None
        if quality == "":
            quality = None
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO source_config(singleton_id, room_identity, preferred_quality, "
                "desired_running, enabled_destinations_json, updated_at) VALUES(1,?,?,?,?,?) "
                "ON CONFLICT(singleton_id) DO UPDATE SET room_identity=excluded.room_identity, "
                "preferred_quality=excluded.preferred_quality, desired_running=excluded.desired_running, "
                "updated_at=excluded.updated_at",
                (canonical, quality, int(desired_running), "[]", _now()),
            )

    def get_source(self) -> SourceConfig | None:
        with self._lock:
            row = (
                self._require_connection()
                .execute(
                    "SELECT room_identity, preferred_quality, desired_running FROM source_config WHERE singleton_id=1"
                )
                .fetchone()
            )
        if row is None:
            return None
        return SourceConfig(str(row[0]), cast(str | None, row[1]), bool(row[2]))

    def set_destination(
        self,
        kind: DestinationKind,
        base_server: str,
        stream_key: str,
        *,
        enabled: bool = True,
    ) -> None:
        server = _validate_base_server(base_server)
        secret = _validate_plaintext_secret(stream_key)
        try:
            encrypted = self._encrypt_secret(secret)
        except Exception as exc:
            raise DestinationSecretError("Unable to encrypt destination secret") from exc
        if not encrypted or encrypted == secret:
            raise DestinationSecretError("Secret encryption returned invalid ciphertext")
        storage_kind = _storage_kind(kind)
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO destination_config(kind, base_server, encrypted_stream_key, enabled, updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(kind) DO UPDATE SET base_server=excluded.base_server, "
                "encrypted_stream_key=excluded.encrypted_stream_key, enabled=excluded.enabled, "
                "updated_at=excluded.updated_at",
                (storage_kind, server, encrypted, int(enabled), _now()),
            )

    def update_destination_key(self, kind: DestinationKind, stream_key: str) -> None:
        secret = _validate_plaintext_secret(stream_key)
        try:
            encrypted = self._encrypt_secret(secret)
        except Exception as exc:
            raise DestinationSecretError("Unable to encrypt destination secret") from exc
        if not encrypted or encrypted == secret:
            raise DestinationSecretError("Secret encryption returned invalid ciphertext")
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE destination_config SET encrypted_stream_key=?, updated_at=? WHERE kind=?",
                (encrypted, _now(), _storage_kind(kind)),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Destination {kind.name} is not configured")

    def get_destination(self, kind: DestinationKind) -> DestinationConfig | None:
        row = self._destination_row(kind)
        if row is None:
            return None
        return DestinationConfig(kind, str(row[1]), bool(row[3]), bool(row[2]))

    def get_destination_runtime(self, kind: DestinationKind) -> RuntimeDestination | None:
        row = self._destination_row(kind)
        if row is None:
            return None
        try:
            secret = self._decrypt_secret(str(row[2]))
        except Exception as exc:
            raise DestinationSecretError("Unable to decrypt destination secret") from exc
        if not secret.strip():
            raise DestinationSecretError("Decrypted destination secret is empty")
        return RuntimeDestination(kind, str(row[1]), secret, bool(row[3]))

    def _destination_row(self, kind: DestinationKind) -> sqlite3.Row | None:
        with self._lock:
            row = (
                self._require_connection()
                .execute(
                    "SELECT kind, base_server, encrypted_stream_key, enabled FROM destination_config WHERE kind=?",
                    (_storage_kind(kind),),
                )
                .fetchone()
            )
        return cast(sqlite3.Row | None, row)

    def list_destinations(self) -> list[DestinationConfig]:
        with self._lock:
            rows = (
                self._require_connection()
                .execute(
                    "SELECT kind, base_server, encrypted_stream_key, enabled FROM destination_config "
                    "ORDER BY CASE kind WHEN 'douyin' THEN 1 WHEN 'wechat_channels' THEN 2 ELSE 3 END"
                )
                .fetchall()
            )
        return [
            DestinationConfig(
                _STORAGE_TO_KIND[str(row[0])], str(row[1]), bool(row[3]), bool(row[2])
            )
            for row in rows
        ]

    def delete_destination(self, kind: DestinationKind) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM destination_config WHERE kind=?", (_storage_kind(kind),)
            )
            return cursor.rowcount == 1

    def set_app_settings(self, settings: AppSettings) -> None:
        _validate_backoff(settings)
        standby = self._normalize_standby(settings.standby_file)
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO app_settings(singleton_id, standby_file, backoff_initial_seconds, "
                "backoff_max_seconds, updated_at) VALUES(1,?,?,?,?) "
                "ON CONFLICT(singleton_id) DO UPDATE SET standby_file=excluded.standby_file, "
                "backoff_initial_seconds=excluded.backoff_initial_seconds, "
                "backoff_max_seconds=excluded.backoff_max_seconds, updated_at=excluded.updated_at",
                (
                    str(standby) if standby else None,
                    settings.backoff_initial_seconds,
                    settings.backoff_max_seconds,
                    _now(),
                ),
            )

    def get_app_settings(self) -> AppSettings:
        with self._lock:
            row = (
                self._require_connection()
                .execute(
                    "SELECT standby_file, backoff_initial_seconds, backoff_max_seconds "
                    "FROM app_settings WHERE singleton_id=1"
                )
                .fetchone()
            )
        if row is None:
            return AppSettings()
        return AppSettings(Path(row[0]) if row[0] else None, float(row[1]), float(row[2]))

    def _normalize_standby(self, value: Path | None) -> Path | None:
        if value is None:
            return None
        raw = str(value)
        if any(ord(character) < 32 or ord(character) == 127 for character in raw):
            raise PathPolicyError("Standby path contains control characters")
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw):
            raise PathPolicyError("Standby media must be a local file")
        candidate = Path(value).expanduser().resolve(strict=False)
        if not candidate.is_file():
            raise PathPolicyError("Standby media must be an existing local file")
        if not any(_is_below(candidate, root) for root in self._trusted_roots):
            raise PathPolicyError("Standby media is outside an approved directory")
        return candidate

    def add_event(self, level: str, event_type: str, payload: Mapping[str, object]) -> int:
        normalized_level = level.upper()
        if normalized_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("Unsupported event level")
        if _SAFE_EVENT_NAME.fullmatch(event_type) is None:
            raise ValueError("Event type is malformed")
        safe_payload = _remove_event_urls(redact(dict(payload)))
        try:
            payload_json = json.dumps(
                safe_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Event payload must be JSON serializable") from exc
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO events(created_at, level, event_type, payload_json) VALUES(?,?,?,?)",
                (_now(), normalized_level, event_type, payload_json),
            )
            if cursor.lastrowid is None:
                raise DatabaseError("SQLite did not return an event identifier")
            event_id = cursor.lastrowid
            connection.execute(
                "DELETE FROM events WHERE id <= (SELECT coalesce(max(id), 0) - ? FROM events)",
                (_EVENT_LIMIT,),
            )
        return event_id

    def list_events(self, *, limit: int = 100) -> list[EventRecord]:
        if not 1 <= limit <= 10_000:
            raise ValueError("Event limit is out of bounds")
        with self._lock:
            rows = (
                self._require_connection()
                .execute(
                    "SELECT id, created_at, level, event_type, payload_json FROM events "
                    "ORDER BY id ASC LIMIT ?",
                    (limit,),
                )
                .fetchall()
            )
        return [
            EventRecord(int(row[0]), str(row[1]), str(row[2]), str(row[3]), json.loads(str(row[4])))
            for row in rows
        ]

    def export_config(self) -> dict[str, object]:
        source = self.get_source()
        settings = self.get_app_settings()
        return {
            "source": asdict(source) if source else None,
            "destinations": [
                {
                    "kind": _storage_kind(item.kind),
                    "base_server": item.base_server,
                    "enabled": item.enabled,
                    "configured": item.configured,
                }
                for item in self.list_destinations()
            ],
            "app_settings": {
                "standby_file": str(settings.standby_file) if settings.standby_file else None,
                "backoff_initial_seconds": settings.backoff_initial_seconds,
                "backoff_max_seconds": settings.backoff_max_seconds,
            },
        }

    def import_config(self, value: Mapping[str, object]) -> None:
        source = value.get("source")
        if isinstance(source, Mapping):
            self.set_source(
                str(source["room_identity"]),
                cast(str | None, source.get("preferred_quality")),
                bool(source.get("desired_running", False)),
            )
        destinations = value.get("destinations")
        if isinstance(destinations, list):
            for item in destinations:
                if not isinstance(item, Mapping):
                    continue
                kind = _STORAGE_TO_KIND.get(str(item.get("kind")))
                if kind is None:
                    continue
                existing = self._destination_row(kind)
                if existing is None:
                    continue
                server = _validate_base_server(str(item.get("base_server", existing[1])))
                with self.transaction() as connection:
                    connection.execute(
                        "UPDATE destination_config SET base_server=?, enabled=?, updated_at=? WHERE kind=?",
                        (
                            server,
                            int(bool(item.get("enabled", existing[3]))),
                            _now(),
                            _storage_kind(kind),
                        ),
                    )

    async def save(self, state: PersistedControllerState) -> None:
        canonical = normalize_douyin_url(state.room_identity)
        enabled = json.dumps(list(state.enabled_destinations), separators=(",", ":"))
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO source_config(singleton_id, room_identity, preferred_quality, desired_running, "
                "enabled_destinations_json, updated_at) VALUES(1,?,NULL,?,?,?) "
                "ON CONFLICT(singleton_id) DO UPDATE SET room_identity=excluded.room_identity, "
                "desired_running=excluded.desired_running, enabled_destinations_json=excluded.enabled_destinations_json, "
                "updated_at=excluded.updated_at",
                (canonical, int(state.desired_running), enabled, _now()),
            )

    async def load(self, room_identity: str) -> PersistedControllerState | None:
        canonical = normalize_douyin_url(room_identity)
        with self._lock:
            row = (
                self._require_connection()
                .execute(
                    "SELECT room_identity, desired_running, enabled_destinations_json "
                    "FROM source_config WHERE singleton_id=1 AND room_identity=?",
                    (canonical,),
                )
                .fetchone()
            )
        if row is None:
            return None
        enabled_raw = json.loads(str(row[2]))
        if not isinstance(enabled_raw, list) or not all(
            isinstance(item, str) for item in enabled_raw
        ):
            raise DatabaseCorruptError("Persisted controller state is malformed")
        return PersistedControllerState(str(row[0]), bool(row[1]), tuple(enabled_raw))

    def dump_text(self) -> str:
        """Test/audit representation of stored values; never decrypts credentials."""
        with self._lock:
            connection = self._require_connection()
            tables = (
                "schema_version",
                "source_config",
                "destination_config",
                "app_settings",
                "events",
            )
            values = [
                tuple(row)
                for table in tables
                for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            ]
        return repr(values)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _storage_kind(kind: DestinationKind) -> str:
    try:
        return _KIND_TO_STORAGE[kind]
    except KeyError as exc:
        raise ValueError("Unsupported destination kind") from exc


def _validate_plaintext_secret(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DestinationSecretError("Destination secret must not be empty")
    return value


def _validate_base_server(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Destination base server contains control characters")
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Destination base server is malformed") from exc
    if parsed.scheme.lower() not in {"rtmp", "rtmps"} or not parsed.hostname:
        raise ValueError("Destination base server must use RTMP or RTMPS")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Destination base server must not contain credentials or query data")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) > 1:
        raise ValueError("Destination base server must not contain a stream key")
    authority = parsed.hostname.lower()
    if ":" in authority and not authority.startswith("["):
        authority = f"[{authority}]"
    if port is not None:
        authority += f":{port}"
    path = f"/{segments[0]}" if segments else ""
    return f"{parsed.scheme.lower()}://{authority}{path}"


def _validate_backoff(settings: AppSettings) -> None:
    if not 1.0 <= settings.backoff_initial_seconds <= 60.0:
        raise ValueError("Initial backoff must be between 1 and 60 seconds")
    if not settings.backoff_initial_seconds <= settings.backoff_max_seconds <= 300.0:
        raise ValueError("Maximum backoff must be between initial backoff and 300 seconds")


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _remove_event_urls(value: object) -> object:
    if isinstance(value, str):
        return _URL_IN_TEXT.sub("***", value)
    if isinstance(value, Mapping):
        sanitized: dict[object, object] = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower()) if isinstance(key, str) else ""
            sanitized[key] = "***" if normalized.endswith("url") else _remove_event_urls(item)
        return sanitized
    if isinstance(value, list):
        return [_remove_event_urls(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_remove_event_urls(item) for item in value)
    return value
