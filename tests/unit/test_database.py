from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest

from restream_studio.config import AppPaths, ConfigPathError
from restream_studio.domain import DestinationKind
from restream_studio.orchestration.controller import PersistedControllerState
from restream_studio.persistence import database as database_module
from restream_studio.persistence.database import (
    AppSettings,
    Database,
    DatabaseBusyError,
    DatabaseClosedError,
    DatabaseCorruptError,
    DestinationSecretError,
    MigrationError,
    PathPolicyError,
)


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    iterator = coroutine.__await__()
    try:
        iterator.send(None)
    except StopIteration as stopped:
        return cast(T, stopped.value)
    raise AssertionError("database store coroutine unexpectedly suspended")


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    return AppPaths.create(tmp_path / "RestreamStudio")


@pytest.fixture
def crypto() -> tuple[Callable[[str], str], Callable[[str], str]]:
    def encrypt(value: str) -> str:
        return "cipher:" + value[::-1]

    def decrypt(value: str) -> str:
        if not value.startswith("cipher:"):
            raise ValueError("bad ciphertext")
        return value.removeprefix("cipher:")[::-1]

    return encrypt, decrypt


@pytest.fixture
def db(
    paths: AppPaths, crypto: tuple[Callable[[str], str], Callable[[str], str]]
) -> Iterator[Database]:
    database = Database(
        paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
    )
    database.open()
    try:
        yield database
    finally:
        database.close()


def test_app_paths_create_only_portable_local_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "local" / "RestreamStudio"
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    paths = AppPaths.create()

    assert paths.base_dir == root.resolve()
    assert paths.data_dir == (root / "data").resolve()
    assert paths.log_dir == (root / "logs").resolve()
    assert paths.standby_dir == (root / "data" / "standby").resolve()
    assert paths.database_file == (root / "data" / "restream-studio.sqlite3").resolve()
    assert all(path.is_dir() for path in (paths.data_dir, paths.log_dir, paths.standby_dir))


def test_app_paths_raise_typed_error_when_localappdata_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(ConfigPathError, match="LOCALAPPDATA"):
        AppPaths.create()


def test_first_open_creates_exact_schema_and_pragmas_and_reopen_is_idempotent(
    paths: AppPaths, crypto: tuple[Callable[[str], str], Callable[[str], str]]
) -> None:
    with Database(
        paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
    ) as database:
        assert database.schema_version == 2
        assert cast(str, database.pragma("journal_mode")).lower() == "wal"
        assert database.pragma("foreign_keys") == 1
        assert database.pragma("busy_timeout") == 5_000

    with sqlite3.connect(paths.database_file) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not row[0].startswith("sqlite_")
        }
        assert tables == {
            "schema_version",
            "source_config",
            "destination_config",
            "app_settings",
            "events",
        }

    with Database(
        paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
    ) as reopened:
        assert reopened.schema_version == 2


def test_close_is_idempotent_and_operations_after_close_are_explicit(
    paths: AppPaths, crypto: tuple[Callable[[str], str], Callable[[str], str]]
) -> None:
    database = Database(
        paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
    )
    database.open()
    database.close()
    database.close()
    with pytest.raises(DatabaseClosedError):
        database.list_destinations()


def test_corrupt_database_is_reported_with_typed_error(
    paths: AppPaths, crypto: tuple[Callable[[str], str], Callable[[str], str]]
) -> None:
    paths.database_file.write_bytes(b"not sqlite")
    database = Database(
        paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
    )
    with pytest.raises(DatabaseCorruptError):
        database.open()
    database.close()


def test_failed_migration_rolls_back_schema_and_version(
    paths: AppPaths,
    crypto: tuple[Callable[[str], str], Callable[[str], str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with Database(
        paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
    ):
        pass

    def broken(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE must_rollback(value TEXT)")
        raise RuntimeError("migration failed")

    monkeypatch.setattr(database_module, "MIGRATIONS", (*database_module.MIGRATIONS, broken))
    database = Database(
        paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
    )
    with pytest.raises(MigrationError):
        database.open()
    database.close()

    with sqlite3.connect(paths.database_file) as connection:
        assert connection.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='must_rollback'"
            ).fetchone()[0]
            == 0
        )


def test_source_is_singleton_canonical_and_never_persists_resolved_media_url(db: Database) -> None:
    db.set_source("https://live.douyin.com/room-7?token=QUERY-TOKEN", "origin", True)
    db.set_source("https://live.douyin.com/room-8", None, False)

    source = db.get_source()
    assert source is not None
    assert source.room_identity == "https://live.douyin.com/room-8"
    assert source.preferred_quality is None
    assert source.desired_running is False
    assert "QUERY-TOKEN" not in db.dump_text()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute_for_test(
            "INSERT INTO source_config(singleton_id, room_identity, preferred_quality, desired_running, updated_at) "
            "VALUES(2, 'https://media.example/live.flv', NULL, 1, 'now')"
        )
    columns = {row[1] for row in db.execute_for_test("PRAGMA table_info(source_config)").fetchall()}
    assert not columns & {"url", "source_url", "resolved_url", "flv_url", "hls_url", "query_token"}
    assert "enabled_destinations_json" not in columns


def test_set_source_preserves_initial_destination_enabled_state(db: Database) -> None:
    db.set_destination(DestinationKind.DOUYIN, "rtmp://one.test/app", "one", enabled=True)
    db.set_destination(DestinationKind.WECHAT, "rtmp://two.test/app", "two", enabled=False)

    db.set_source("https://live.douyin.com/room-one", "origin", True)

    assert db.get_destination(DestinationKind.DOUYIN).enabled is True  # type: ignore[union-attr]
    assert db.get_destination(DestinationKind.WECHAT).enabled is False  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "identity",
    [
        "https://media.example/live.flv",
        "https://live.douyin.com/room.m3u8",
        "https://live.douyin.com/room?token=raw",
    ],
)
def test_source_table_constraint_rejects_media_and_query_identities(
    db: Database, identity: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute_for_test(
            "INSERT INTO source_config(singleton_id, room_identity, preferred_quality, "
            "desired_running, updated_at) VALUES(1, ?, NULL, 1, 'now')",
            (identity,),
        )


def test_destination_kind_is_reused_mapped_and_unique_per_supported_kind(db: Database) -> None:
    for kind in DestinationKind:
        db.set_destination(kind, "rtmps://push.example.test/live", "stream-key", enabled=True)

    assert [item.kind for item in db.list_destinations()] == list(DestinationKind)
    stored = {
        row[0]
        for row in db.execute_for_test(
            "SELECT kind FROM destination_config ORDER BY kind"
        ).fetchall()
    }
    assert stored == {"douyin", "wechat_channels", "local_test"}
    with pytest.raises(sqlite3.IntegrityError):
        db.execute_for_test(
            "INSERT INTO destination_config(kind, base_server, encrypted_stream_key, enabled, updated_at) "
            "VALUES('youtube', 'rtmp://x/app', 'cipher', 1, 'now')"
        )


def test_destination_secret_is_encrypted_immediately_and_never_in_public_views(
    db: Database, crypto: tuple[Callable[[str], str], Callable[[str], str]]
) -> None:
    plaintext = "PLAIN-STREAM-KEY"
    db.set_destination(DestinationKind.DOUYIN, "rtmps://push.example.test/live", plaintext)

    public = db.get_destination(DestinationKind.DOUYIN)
    runtime = db.get_destination_runtime(DestinationKind.DOUYIN)
    raw = db.execute_for_test(
        "SELECT encrypted_stream_key FROM destination_config WHERE kind='douyin'"
    ).fetchone()[0]
    assert raw == crypto[0](plaintext)
    assert plaintext not in raw
    assert public is not None and public.configured is True
    assert "encrypted_stream_key" not in public.__dataclass_fields__
    assert plaintext not in repr(public)
    assert runtime is not None and runtime.stream_key == plaintext
    assert plaintext not in repr(runtime)
    assert plaintext not in json.dumps(db.export_config(), sort_keys=True)
    assert "encrypted_stream_key" not in json.dumps(db.export_config(), sort_keys=True)


def test_destination_key_update_replaces_ciphertext_and_delete_is_scoped(db: Database) -> None:
    db.set_destination(DestinationKind.DOUYIN, "rtmp://one.test/app", "old-key")
    db.set_destination(DestinationKind.WECHAT, "rtmp://two.test/app", "keep-key")
    old_cipher = db.execute_for_test(
        "SELECT encrypted_stream_key FROM destination_config WHERE kind='douyin'"
    ).fetchone()[0]

    db.update_destination_key(DestinationKind.DOUYIN, "new-key")
    assert db.get_destination_runtime(DestinationKind.DOUYIN).stream_key == "new-key"  # type: ignore[union-attr]
    assert (
        db.execute_for_test(
            "SELECT encrypted_stream_key FROM destination_config WHERE kind='douyin'"
        ).fetchone()[0]
        != old_cipher
    )
    assert db.delete_destination(DestinationKind.DOUYIN) is True
    assert db.delete_destination(DestinationKind.DOUYIN) is False
    assert db.get_destination(DestinationKind.WECHAT) is not None


def test_empty_or_corrupt_destination_secrets_have_explicit_errors(db: Database) -> None:
    with pytest.raises(DestinationSecretError, match="empty"):
        db.set_destination(DestinationKind.DOUYIN, "rtmp://one.test/app", "  ")
    db.set_destination(DestinationKind.DOUYIN, "rtmp://one.test/app", "valid")
    db.execute_for_test(
        "UPDATE destination_config SET encrypted_stream_key='broken' WHERE kind='douyin'"
    )
    with pytest.raises(DestinationSecretError, match="decrypt"):
        db.get_destination_runtime(DestinationKind.DOUYIN)


@pytest.mark.parametrize(
    "base_server",
    [
        "rtmp://push.test/app/key",
        "rtmp://push.test/app?token=x",
        "https://push.test/app",
        "rtmp://u:p@push.test/app",
    ],
)
def test_destination_base_server_rejects_secret_bearing_or_non_rtmp_values(
    db: Database, base_server: str
) -> None:
    with pytest.raises(ValueError):
        db.set_destination(DestinationKind.DOUYIN, base_server, "key")


def test_app_settings_normalize_allowed_standby_file_and_enforce_bounds(
    db: Database, paths: AppPaths, tmp_path: Path
) -> None:
    standby = paths.standby_dir / "idle.mp4"
    standby.write_bytes(b"video")
    db.set_app_settings(
        AppSettings(standby_file=standby, backoff_initial_seconds=2.0, backoff_max_seconds=30.0)
    )
    stored = db.get_app_settings()
    assert stored.standby_file == standby.resolve()

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    with pytest.raises(PathPolicyError):
        db.set_app_settings(AppSettings(standby_file=outside))
    with pytest.raises(PathPolicyError):
        db.set_app_settings(AppSettings(standby_file=Path("https://evil.test/a.mp4")))
    with pytest.raises(PathPolicyError):
        db.set_app_settings(AppSettings(standby_file=Path("bad\nname.mp4")))
    with pytest.raises(ValueError, match="backoff"):
        db.set_app_settings(AppSettings(backoff_initial_seconds=0.1, backoff_max_seconds=30.0))
    with pytest.raises(ValueError, match="backoff"):
        db.set_app_settings(AppSettings(backoff_initial_seconds=20.0, backoff_max_seconds=10.0))


def test_selected_standby_root_policy_allows_only_files_below_that_root(
    paths: AppPaths,
    crypto: tuple[Callable[[str], str], Callable[[str], str]],
    tmp_path: Path,
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    media = selected / "idle.mp4"
    media.write_bytes(b"video")
    with Database(
        paths.database_file,
        paths=paths,
        selected_standby_roots=(selected,),
        encrypt_secret=crypto[0],
        decrypt_secret=crypto[1],
    ) as database:
        database.set_app_settings(AppSettings(standby_file=media))
        assert database.get_app_settings().standby_file == media.resolve()


def test_events_are_redacted_structured_retained_and_stably_sorted(db: Database) -> None:
    for index in range(5_010):
        db.add_event(
            "INFO",
            "output_status",
            {
                "index": index,
                "url": "rtmp://push.test/app/RAW-KEY?token=RAW-TOKEN",
                "cookie": "session=RAW-COOKIE",
                "authorization": "Bearer RAW-AUTH",
            },
        )

    events = db.list_events(limit=6_000)
    assert len(events) == 5_000
    assert [event.payload["index"] for event in events[:2]] == [10, 11]
    assert [event.id for event in events] == sorted(event.id for event in events)
    raw = db.dump_text()
    for secret in ("RAW-KEY", "RAW-TOKEN", "RAW-COOKIE", "RAW-AUTH"):
        assert secret not in raw
    assert events[-1].payload["cookie"] == "***"


def test_events_never_retain_urls_in_fields_or_free_form_messages(db: Database) -> None:
    raw_url = "https://media.example/live.flv?token=TOP-SECRET"
    db.add_event(
        "ERROR", "resolver_failed", {"source_url": raw_url, "message": f"failed {raw_url}"}
    )

    event = db.list_events()[0]
    assert event.payload == {"message": "failed ***", "source_url": "***"}
    assert "media.example" not in db.dump_text()


def test_event_messages_conservatively_redact_secret_assignments(
    db: Database, paths: AppPaths
) -> None:
    secrets = (
        "STREAM-SECRET",
        "TOKEN-SECRET",
        "PASSWORD-SECRET",
        "AUTH-SECRET",
        "COOKIE-SECRET",
        "GENERIC-SECRET",
    )
    message = (
        "stream_key=STREAM-SECRET token=TOKEN-SECRET password: PASSWORD-SECRET "
        "authorization=Bearer AUTH-SECRET cookie: COOKIE-SECRET secret=GENERIC-SECRET"
    )
    db.add_event("ERROR", "credential_failure", {"message": message})
    db.execute_for_test("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    query_text = json.dumps(db.list_events()[0].payload)
    database_bytes = paths.database_file.read_bytes()
    for secret in secrets:
        assert secret not in query_text
        assert secret.encode() not in database_bytes


def test_event_api_rejects_unstructured_payload(db: Database) -> None:
    with pytest.raises(TypeError, match="mapping"):
        db.add_event("INFO", "bad", "token=SECRET")  # type: ignore[arg-type]


def test_concurrent_event_writes_are_atomic_and_stably_ordered(db: Database) -> None:
    def writer(index: int) -> None:
        db.add_event("INFO", "concurrent", {"index": index})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(writer, range(240)))

    events = db.list_events(limit=500)
    assert len(events) == 240
    assert len({event.id for event in events}) == 240
    assert [event.id for event in events] == sorted(event.id for event in events)


def test_export_is_secret_free_json_serializable_and_import_ignores_secret_fields(
    db: Database,
) -> None:
    db.set_source("https://live.douyin.com/777", "origin", True)
    db.set_destination(DestinationKind.LOCAL_TEST, "rtmp://127.0.0.1/live", "LOCAL-SECRET")
    exported = db.export_config()
    encoded = json.dumps(exported)
    destinations = cast(list[dict[str, object]], exported["destinations"])
    assert destinations[0]["configured"] is True
    assert "LOCAL-SECRET" not in encoded
    assert "cipher" not in encoded.lower()

    db.import_config(
        {
            "source": {
                "room_identity": "https://live.douyin.com/888",
                "preferred_quality": None,
                "desired_running": False,
            },
            "destinations": [
                {
                    "kind": "local_test",
                    "base_server": "rtmp://127.0.0.1/new",
                    "enabled": False,
                    "configured": True,
                    "stream_key": "ATTACKER-KEY",
                    "encrypted_stream_key": "ATTACKER-CIPHER",
                }
            ],
        }
    )
    assert db.get_source().room_identity == "https://live.douyin.com/888"  # type: ignore[union-attr]
    assert db.get_destination_runtime(DestinationKind.LOCAL_TEST).stream_key == "LOCAL-SECRET"  # type: ignore[union-attr]


def test_import_validates_everything_before_one_atomic_transaction(db: Database) -> None:
    db.set_source("https://live.douyin.com/original", "origin", True)
    db.set_destination(DestinationKind.DOUYIN, "rtmp://one.test/app", "keep", enabled=True)

    with pytest.raises(ValueError):
        db.import_config(
            {
                "source": {
                    "room_identity": "https://live.douyin.com/replacement",
                    "preferred_quality": "hd",
                    "desired_running": False,
                },
                "destinations": [
                    {
                        "kind": "douyin",
                        "base_server": "https://invalid.test/app",
                        "enabled": False,
                    }
                ],
            }
        )

    assert db.get_source().room_identity == "https://live.douyin.com/original"  # type: ignore[union-attr]
    assert db.get_destination(DestinationKind.DOUYIN).enabled is True  # type: ignore[union-attr]


def test_database_adapts_controller_state_store_without_resolved_url(db: Database) -> None:
    db.set_destination(DestinationKind.DOUYIN, "rtmp://one.test/app", "one", enabled=False)
    db.set_destination(DestinationKind.WECHAT, "rtmp://two.test/app", "two", enabled=True)
    state = PersistedControllerState(
        room_identity="https://live.douyin.com/room-9?token=DROP-ME",
        desired_running=True,
        enabled_destinations=("douyin",),
    )
    run(db.save(state))
    restored = run(db.load("https://live.douyin.com/room-9"))

    assert restored == PersistedControllerState(
        room_identity="https://live.douyin.com/room-9",
        desired_running=True,
        enabled_destinations=("douyin",),
    )
    assert "DROP-ME" not in db.dump_text()
    assert "resolved" not in {
        row[1].lower() for row in db.execute_for_test("PRAGMA table_info(source_config)")
    }


def test_controller_load_builds_enabled_set_from_destination_rows(db: Database) -> None:
    db.set_source("https://live.douyin.com/room-state", None, True)
    db.set_destination(DestinationKind.DOUYIN, "rtmp://one.test/app", "one", enabled=True)
    db.set_destination(DestinationKind.WECHAT, "rtmp://two.test/app", "two", enabled=False)
    db.set_destination(DestinationKind.LOCAL_TEST, "rtmp://local.test/app", "local", enabled=True)

    restored = run(db.load("https://live.douyin.com/room-state"))

    assert restored is not None
    assert restored.enabled_destinations == ("douyin", "local_test")


def test_busy_database_has_distinct_error_and_is_not_reported_corrupt(
    paths: AppPaths, crypto: tuple[Callable[[str], str], Callable[[str], str]]
) -> None:
    first = Database(
        paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
    )
    second = Database(
        paths.database_file,
        paths=paths,
        encrypt_secret=crypto[0],
        decrypt_secret=crypto[1],
        busy_timeout_ms=10,
    )
    first.open()
    second.open()
    try:
        with first.transaction() as connection:
            connection.execute(
                "INSERT INTO events(created_at, level, event_type, payload_json) "
                "VALUES('now','INFO','locked','{}')"
            )
            with pytest.raises(DatabaseBusyError):
                second.add_event("INFO", "blocked", {})
    finally:
        second.close()
        first.close()


def test_two_database_instances_can_open_and_migrate_concurrently(
    paths: AppPaths, crypto: tuple[Callable[[str], str], Callable[[str], str]]
) -> None:
    barrier = threading.Barrier(2)

    def opener(_: int) -> int:
        database = Database(
            paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
        )
        barrier.wait()
        try:
            database.open()
            return database.schema_version
        finally:
            database.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(opener, range(2))) == [2, 2]


def test_open_rejects_schema_version_that_claims_missing_schema(
    paths: AppPaths, crypto: tuple[Callable[[str], str], Callable[[str], str]]
) -> None:
    with sqlite3.connect(paths.database_file) as connection:
        connection.execute(
            "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO schema_version VALUES(?, 'now')",
            [(1,), (2,)],
        )
    database = Database(
        paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
    )
    with pytest.raises(DatabaseCorruptError, match="schema"):
        database.open()


def test_open_rejects_non_contiguous_schema_version_history(
    paths: AppPaths, crypto: tuple[Callable[[str], str], Callable[[str], str]]
) -> None:
    with Database(
        paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
    ):
        pass
    with sqlite3.connect(paths.database_file) as connection:
        connection.execute("DELETE FROM schema_version WHERE version=1")

    database = Database(
        paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
    )
    with pytest.raises(DatabaseCorruptError, match="version"):
        database.open()


def test_v1_migration_discards_duplicate_enabled_json_and_preserves_destination_rows(
    paths: AppPaths, crypto: tuple[Callable[[str], str], Callable[[str], str]]
) -> None:
    with sqlite3.connect(paths.database_file) as connection:
        connection.execute("BEGIN IMMEDIATE")
        database_module.MIGRATIONS[0](connection)
        connection.execute("INSERT INTO schema_version VALUES(1, 'now')")
        connection.execute(
            "INSERT INTO source_config VALUES(1, 'https://live.douyin.com/legacy', NULL, 1, "
            "'not-json', 'now')"
        )
        connection.execute(
            "INSERT INTO destination_config VALUES('douyin', 'rtmp://one.test/app', "
            "'cipher:eno', 1, 'now')"
        )
        connection.commit()

    with Database(
        paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
    ) as migrated:
        columns = {
            row[1]
            for row in migrated.execute_for_test("PRAGMA table_info(source_config)").fetchall()
        }
        restored = run(migrated.load("https://live.douyin.com/legacy"))
        assert "enabled_destinations_json" not in columns
        assert restored is not None and restored.enabled_destinations == ("douyin",)


def test_tampered_settings_destination_and_event_json_fail_closed(
    db: Database, paths: AppPaths
) -> None:
    standby = paths.standby_dir / "safe.mp4"
    standby.write_bytes(b"video")
    db.set_app_settings(AppSettings(standby_file=standby))
    db.set_destination(DestinationKind.DOUYIN, "rtmp://one.test/app", "key")
    db.add_event("INFO", "valid", {"ok": True})
    db.execute_for_test("PRAGMA ignore_check_constraints=ON")
    db.execute_for_test(
        "UPDATE app_settings SET backoff_initial_seconds=0, standby_file='C:\\outside.mp4'"
    )
    db.execute_for_test(
        "UPDATE destination_config SET base_server='https://invalid.test/app' WHERE kind='douyin'"
    )
    db.execute_for_test("UPDATE events SET payload_json='{broken' WHERE event_type='valid'")

    with pytest.raises(DatabaseCorruptError, match="settings"):
        db.get_app_settings()
    with pytest.raises(DatabaseCorruptError, match="destination"):
        db.get_destination(DestinationKind.DOUYIN)
    with pytest.raises(DatabaseCorruptError, match="event"):
        db.list_events()


def test_dump_text_never_exposes_ciphertext_or_plaintext(db: Database) -> None:
    db.set_destination(DestinationKind.DOUYIN, "rtmp://one.test/app", "PLAIN-SECRET")
    dumped = db.dump_text()
    assert "PLAIN-SECRET" not in dumped
    assert "cipher:" not in dumped
    assert "encrypted_stream_key" not in dumped
    assert "configured" in dumped


def test_close_racing_reads_only_surfaces_typed_closed_error(
    paths: AppPaths, crypto: tuple[Callable[[str], str], Callable[[str], str]]
) -> None:
    database = Database(
        paths.database_file, paths=paths, encrypt_secret=crypto[0], decrypt_secret=crypto[1]
    )
    database.open()
    errors: list[BaseException] = []

    def reader() -> None:
        for _ in range(2_000):
            try:
                database.list_destinations()
            except DatabaseClosedError as exc:
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(reader)
        executor.submit(database.close).result()
        future.result()

    assert errors
    assert all(isinstance(error, DatabaseClosedError) for error in errors)


def test_transaction_rolls_back_all_writes_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction() as connection:
        connection.execute(
            "INSERT INTO events(created_at, level, event_type, payload_json) VALUES('now','INFO','x','{}')"
        )
        raise RuntimeError("abort")
    assert db.list_events() == []
