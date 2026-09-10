# Task 5: Replaceable Douyin resolver adapter TDD evidence

## Scope

This task adds only the resolver protocol, typed adapter errors, a replaceable Douyin
component boundary, sanitized live/offline fixtures, deterministic quality selection, and
an opt-in live-network schema test. It does not add caching, persistence, or SQLite access.

## RED — resolver contract absent

The Task 5 tests and sanitized fixtures were added before production implementation.

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_douyin_resolver.py -q
```

Observed result: exit code `1`; test collection stopped with:

```text
ModuleNotFoundError: No module named 'restream_studio.source.base'
```

This was the expected failure because neither the resolver protocol nor the Douyin adapter
existed. No network request occurred during this RED run.

## GREEN — offline adapter contract

The minimum implementation added:

- an async `LiveSourceResolver` protocol and typed resolver exceptions;
- normalization through the Task 3 `normalize_douyin_url` boundary;
- injectable async callables, sync callables, and client objects;
- contained third-party failures and typed rate-limit/protocol mappings;
- live/offline mapping and `origin > blue > ultra > high > standard > smooth` selection;
- immutable FLV/HLS candidate tuples and backward-compatible `ResolvedStream` fields;
- no signed-URL cache, persistence, or SQLite integration;
- a `live_network` test excluded by default and gated by `DOUYIN_TEST_ROOM_URL`.

Offline command:

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not live_network" tests/unit/test_douyin_resolver.py -v
```

Observed result: exit code `0`; `15 passed, 1 deselected in 0.05s`.

The local Windows Python runtime could not initialize pytest-asyncio's Proactor event loop:
its internal `socketpair` fallback blocked in `socket.accept`. The offline tests therefore
drive adapter-only coroutines directly; the opt-in integration test retains normal async
pytest execution. This was an environment-level event-loop initialization issue, not a
resolver network call. The live-network test was not executed.
