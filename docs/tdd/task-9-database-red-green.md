# Task 9 database TDD evidence

## Scope

Task 9 adds the local configuration boundary only: portable application paths, an idempotently
migrated SQLite database, DPAPI-backed destination secrets, bounded settings, redacted event
retention, secret-free JSON export, and the Task 8 `ControllerStateStore` adapter. No network,
resolver, media, UI, or packaging behavior was added.

The schema contains exactly five application tables: `schema_version`, `source_config`,
`destination_config`, `app_settings`, and `events`. SQLite is opened with WAL,
`foreign_keys=ON`, a 5000 ms busy timeout, explicit transactions, and deterministic close.

## RED

The Task 9 tests were created before production files. The initial focused command was:

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_database.py -q -p no:cacheprovider
```

It failed during collection as expected:

```text
ModuleNotFoundError: No module named 'restream_studio.config'
1 error in 0.22s
```

After the first GREEN implementation, four additional hardening tests were added. They failed
before the corresponding schema and event-redaction changes:

```text
4 failed, 22 deselected in 0.30s
```

The failures proved that direct SQL could insert a media URL, an HLS-looking identity, or a query
token into `source_config`, and that event payloads retained raw HTTPS URLs in URL fields and
free-form messages.

## GREEN

- `AppPaths.create()` resolves either injected storage or `%LOCALAPPDATA%\RestreamStudio`, creates
  `data`, `logs`, and `data\standby`, and wraps path/setup failures in `ConfigPathError`.
- Migration 1 is applied inside `BEGIN IMMEDIATE`; its version row and DDL roll back together.
  Reopening is idempotent, corrupt storage has a typed error, and newer unknown schemas are
  rejected.
- `source_config` is a checked singleton. Only a normalized Douyin room identity, quality,
  desired-running flag, and destination identities are stored. Table checks reject queries,
  fragments, FLV/HLS-looking paths, and non-Douyin identities. Resolved media URLs have no column.
- The existing `DestinationKind` is mapped exactly to `douyin`, `wechat_channels`, and
  `local_test`; the primary key and SQL check enforce one row per supported kind. Base-server
  validation excludes userinfo, query data, fragments, non-RTMP schemes, and key-bearing paths.
- Destination write/update APIs accept plaintext only at their boundary and encrypt before the
  transaction. Public immutable DTOs expose only `configured`; runtime decryption is explicit and
  its secret field is excluded from repr. Empty keys, failed encryption, missing destinations,
  and damaged ciphertext are explicit errors. Export and import never transport secret fields.
- Standby files resolve to existing absolute local files beneath the managed standby directory or
  an injected user-selected root. URLs, controls, missing files, and traversal/outside paths are
  rejected. Backoff values are bounded from 1 through 300 seconds and preserve
  `initial <= maximum`.
- Event payloads are recursively redacted before serialization, then all HTTP/RTMP URLs are
  removed from structured URL fields and free text. Insert plus deletion beyond the latest 5000
  rows is one transaction. IDs provide stable ascending order; a threaded writer regression
  verifies atomicity.
- Async `load`/`save` methods satisfy the controller protocol while persisting only canonical
  identity and desired state. No resolved URL crosses that adapter.

Focused GREEN used a process-local `pathlib.Path.mkdir` mode shim because this host maps pytest's
Windows `0700` temp-directory mode to an inaccessible ACL. The shim is not committed and changes
only temp-directory creation mode:

```text
26 passed in 4.78s
```

## Verification

```text
npm run lint
All checks passed!

npm run typecheck
Success: no issues found in 34 source files

pytest tests/unit tests/integration/test_controller.py
  --ignore=tests/unit/test_ffprobe.py -q -p no:cacheprovider
193 passed, 1 deselected in 5.36s
```

The synchronous regression command used the same uncommitted mkdir-mode shim and an explicit
base temp directory. It contains all Task 9 tests and the existing controller suite; no network is
used.

Plain `npm test` collected 252 tests but stopped on two pre-existing inaccessible root
directories, `pytest-cache-files-c2lyex6b` and `tmp0fe8fhgz`, with `WinError 5`. `npm run build`
was attempted and stopped before the build backend because pip could not create a build-tracker
entry under `G:\CodexData\tmp` (`PermissionError: [Errno 13]`). These are host filesystem/ACL
blocks; the focused and synchronous suites above provide the executable Task 9 evidence.

## Quality review closure

Thirteen review regressions were added before their fixes. The first RED run stopped at collection
because `DatabaseBusyError` did not exist. After the initial implementation, a narrower schema
history regression produced the expected behavioral RED:

```text
Failed: DID NOT RAISE DatabaseCorruptError
1 failed, 38 deselected in 0.28s
```

The review GREEN changes are:

- event input is mapping-only; URL removal and recursive key redaction are followed by conservative
  free-text masking for `stream_key`, `token`, `password`, `passwd`, `secret`, `authorization`,
  and `cookie` assignments. Tests checkpoint WAL and verify both queried JSON and database bytes;
- schema v2 removes legacy `enabled_destinations_json`. Controller load derives its enabled tuple
  only from `destination_config.enabled`; controller save updates those rows, while `set_source`
  does not modify destination state. Migration ignores malformed legacy JSON and preserves rows;
- every migration acquires `BEGIN IMMEDIATE` before re-reading the version. SQLite busy/locked
  conditions receive four bounded attempts and become `DatabaseBusyError`, never corruption.
  Two independent instances concurrently opening a fresh database converge on schema v2;
- v2 adds SQL checks for backoff bounds/order, RTMP(S) server shape, booleans, and JSON validity.
  Every public read repeats semantic validation; path policy, destination server, desired state,
  and event JSON tampering fail closed as `DatabaseCorruptError`;
- open validates contiguous version history, the exact five-table/column contract, required check
  clauses, and the exact event index. Missing/falsely complete schemas are rejected;
- import validates the complete source, destination, and settings payload before entering one
  write transaction. Secret-shaped import fields remain ignored and any failure leaves all rows
  unchanged;
- `dump_text()` now renders only the public secret-free export and already-redacted events. It
  contains neither plaintext nor DPAPI ciphertext;
- connection lookup and each SQLite operation are covered by the same reentrant lock. A stress
  regression racing close against reads observes only `DatabaseClosedError`, never leaked
  `sqlite3.ProgrammingError`.

Final review verification:

```text
39 focused Task 9 tests passed
206 synchronous regression tests passed, 1 deselected
Ruff: All checks passed
mypy: Success, no issues found in 34 source files
```

## Identity and import re-review closure

The final two findings were reproduced before production changes. The focused RED run reported
13 failures: both database and real-controller tests rejected the missing
`controller_identity` API, v1 migration returned no enabled identities, and all ten string,
integer, or null pseudo-boolean imports were accepted.

Schema v3 adds a unique, non-empty, non-secret `controller_identity` to each of the three strict
destination-kind rows. New rows default to the persisted kind identity; explicit identities such
as `primary` and `secondary` survive public DTO/export, controller save, and controller load.
Controller save matches enabled identities against `controller_identity`, never against kind.

For direct v1 upgrades, migration 2 now retains the legacy `enabled_destinations_json` in a
migration-only table before removing the duplicate source column. Migration 3 validates and
deduplicates that list and maps only identities that explicitly resolve to a known kind. Existing
v2 databases preserve their current enabled flags and receive kind-based default identities.

Import now accepts `desired_running` and destination `enabled` only when `type(value) is bool`.
Explicit strings, integers, and null are rejected during full-payload validation before the single
write transaction, leaving source and destination rows unchanged.

The real Task 8 `Controller` is exercised with `ConfiguredDestination("primary", ...)` and
`ConfiguredDestination("secondary", ...)` against the SQLite store: disable, save, fresh
controller initialize, and snapshot all preserve the two configured identities.

Final commands and results are recorded after implementation formatting:

```text
tests/unit/test_database.py: 50 passed
tests/unit/test_database.py + tests/integration/test_controller.py: 80 passed
synchronous regression: 218 passed, 1 deselected
Ruff: All checks passed
mypy: Success, no issues found in 34 source files
git diff --check: clean
```

## Fail-closed migration final review

Two final regressions were written first. RED showed that an explicit empty
`controller_identity` silently became the kind default, and a v1 source containing only the
unknown identity `secondary` was guessed as the first destination:

```text
2 failed, 50 deselected in 0.35s
```

Migration 3 no longer assigns unknown legacy identities by row order. It enables only identities
with an explicit, deterministic mapping to a supported destination kind. Unknown, malformed, or
conflicting identities leave unmatched destinations disabled and insert one structured
`migration_reconciliation_required` warning event containing only scope, required flag, and
unmapped count. No legacy identity or credential is copied into that event. A two-destination v1
fixture containing only `secondary` verifies that both outputs remain disabled.

`set_destination()` now treats only `None` as the request for a kind-based default identity. An
explicit empty string reaches the normal identity validator and fails before encryption or any
database write.

Final verification:

```text
tests/unit/test_database.py: 52 passed
tests/unit/test_database.py + tests/integration/test_controller.py: 82 passed
synchronous regression: 220 passed, 1 deselected
Ruff: All checks passed
mypy: Success, no issues found in 34 source files
git diff --check: clean
```

## Atomic controller-load snapshot review

Four final regressions were added first. A trace-controlled second SQLite connection committed a
new desired state between the source and enabled-destination reads; RED returned the old source
state with the new destination set. Three tampered identity cases (URL, control character, and
space) also passed through unchanged:

```text
4 failed, 52 deselected in 0.53s
```

`load()` now holds the instance lock and an explicit `BEGIN DEFERRED` read transaction across both
queries. The first source read establishes one WAL snapshot, so a concurrent connection may commit
without producing a mixed result. Commit/rollback and busy/corruption translation remain typed.

Every enabled `controller_identity` is validated again at the read boundary. Any malformed stored
identity becomes `DatabaseCorruptError` with no raw identity value in the message; `ValueError`
does not escape.

Final verification:

```text
tests/unit/test_database.py: 56 passed
tests/unit/test_database.py + tests/integration/test_controller.py: 86 passed
synchronous regression: 224 passed, 1 deselected
Ruff: All checks passed
mypy: Success, no issues found in 34 source files
git diff --check: clean
```

## Legacy event migration redaction review

The v1 migration regression was added before changing production code. Its fixture stores one
valid legacy payload containing plaintext `stream_key`/`token` assignments and a sensitive
structured field, plus one malformed payload containing plaintext. RED failed in migration 2
because the raw `INSERT SELECT` copied malformed JSON into the v2 JSON-validity constraint:

```text
1 failed, 56 deselected in 0.31s
MigrationError: Migration 2 failed
```

Migration 2 now reads legacy events individually. Object payloads pass through the same recursive
`redact()` and conservative free-text/URL sanitization used by the current event API before being
serialized into the replacement table. Invalid JSON and non-object JSON are replaced with the
secret-free `{reason: invalid_legacy_payload, redacted: true}` marker; no malformed source text is
retained. Legacy event metadata is also allowlisted, and SQLite secure deletion scrubs pages freed
when the old event table is dropped.

The GREEN regression verifies all four fixture secrets are absent from `list_events()`,
`dump_text()`, and the database plus sidecar file bytes after close.

Final verification:

```text
focused v1 event migration: 1 passed, 56 deselected
tests/unit/test_database.py: 57 passed
tests/unit/test_database.py + tests/integration/test_controller.py: 87 passed
synchronous regression: 225 passed, 1 deselected
Ruff: All checks passed (three pre-existing inaccessible-directory warnings)
mypy: Success, no issues found in 34 source files
```
