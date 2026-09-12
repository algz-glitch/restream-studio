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
