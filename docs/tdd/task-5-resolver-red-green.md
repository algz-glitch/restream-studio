# Task 5: Replaceable Douyin resolver adapter TDD evidence

## Scope

This task adds only the resolver protocol, typed adapter errors, a replaceable Douyin
component boundary backed by StreamGet 4.0.10, sanitized live/offline fixtures,
deterministic quality selection, and an opt-in live-network schema test. It does not add
caching, persistence, or SQLite access.

The RED/GREEN entries below are command evidence collected during development before each
working-tree commit. They describe observed command order; they do not claim that Git commit
history independently proves test-first ordering.

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

## RED — real default component contract missing

After review identified that the original default named a nonexistent `douyin_live.resolve`
module contract, tests were added for StreamGet's documented `DouyinLiveStream` API and
quality codes.

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not live_network" tests/unit/test_douyin_resolver.py -q
```

Observed result: exit code `1`; collection failed with:

```text
ImportError: cannot import name 'QUALITY_CODE_BY_NAME' from 'restream_studio.source.douyin'
1 error in 0.22s
```

This RED and the following GREEN occurred in the same uncommitted working-tree change. No
network test was selected.

## GREEN — StreamGet 4.0.10 adapter

The default factory now imports `DouyinLiveStream`, calls
`fetch_web_stream_data(normalized_url)`, then calls `fetch_stream_url(data, quality_code)`.
The explicit mapping is `origin -> OD`, `blue -> OD`, `ultra -> UHD`, `high -> HD`,
`standard -> SD`, and `smooth -> LD`. Both `StreamData` objects and equivalent dictionaries
are normalized at the adapter boundary; injected fixture components remain supported.

Observed result for the same offline command: exit code `0`;
`17 passed, 1 deselected in 0.06s`.

Dependency installation and import/API smoke checks then confirmed:

```text
Successfully installed ... streamget-4.0.10 ...
4.0.10
<class 'streamget.DouyinLiveStream'>
(self, url: str, process_data: bool = True) -> dict
(self, json_data: dict, video_quality: str | int | None = None) -> streamget.StreamData
No broken requirements found.
```

The dependency is pinned as `streamget==4.0.10`. These smoke checks imported and inspected
the installed package only; they did not resolve a room or access Douyin.

After dictionary-result and default-boundary exception regressions were added, final Task 5
verification reported `19 passed, 1 deselected in 0.07s`. The complete default suite reported
`87 passed, 1 deselected in 0.14s`; the deselected test was `live_network`.
