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
The explicit mapping is `origin -> OD`, `blue -> BD`, `ultra -> UHD`, `high -> HD`,
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

## RED — preferred quality availability and actual fallback

Review found that `origin` and `blue` were incorrectly mapped to the same StreamGet code and
that the adapter trusted the requested quality when StreamGet returned another URL. Tests
were added for distinct `OD`/`BD` codes, preferred-quality presence, missing-preference
fallback, and actual returned-URL identification.

The first focused RED run reported exit code `1`; `2 failed, 19 passed, 1 deselected in
0.19s`. The two failures showed `blue -> OD` and a `blue` request sending `OD` rather than
`BD`. After the first implementation edit exposed an ordering defect, the next RED run
reported `4 failed, 17 passed, 1 deselected in 0.22s` with an `UnboundLocalError` before the
raw StreamGet data was fetched. Both are command observations from the same uncommitted
working-tree cycle.

## GREEN — availability-aware selection

The adapter now derives available qualities from StreamGet's raw FLV/HLS maps, selects the
preferred quality only when present, otherwise applies the documented internal order, and
matches returned media URLs back to the raw maps to identify StreamGet fallback accurately.
The focused GREEN run reported exit code `0`; `21 passed, 1 deselected in 0.04s`.

Inspection of installed StreamGet 4.0.10 confirmed its public `StreamData` documentation
mentions `OD`, `BD`, `UHD`, and `HD`. Its current Douyin `get_quality_index` implementation
omits `BD` from its local index table, so the adapter deliberately does not trust the echoed
quality label when a returned URL can be matched to raw quality data.

## RED — StreamGet 4.0.10 BD implementation regression

A focused regression reproduced the installed Douyin implementation's behavior: its
`fetch_stream_url(data, "BD")` path can select index zero and return origin URLs. The test
requires the adapter to avoid that path when mature parsed raw quality maps are available.

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not live_network" tests/unit/test_douyin_resolver.py::test_default_adapter_bypasses_streamget_bd_fallback_and_selects_raw_blue_urls -q
```

Observed result: exit code `1`; `1 failed in 0.16s`. The assertion showed that
`fetch_stream_url` was still called with `BD` instead of selecting the parsed raw BD URLs.
This is command evidence from the same uncommitted RED/GREEN cycle, not a claim inferred from
Git history.

## GREEN — direct raw-quality URL selection

When `fetch_web_stream_data` returns recognized per-quality FLV/HLS maps, the adapter now
selects the preferred or ranked fallback itself and returns those exact raw URLs. It calls
`fetch_stream_url` only when no recognized multi-quality map is available. This preserves
StreamGet as the parser while avoiding its Douyin BD index defect.

The first complete focused GREEN run reported exit code `0`;
`21 passed, 1 deselected in 0.07s`.

## RED — boundary hardening review

Tests were added in the uncommitted working tree for unsafe media destinations, signed URL
expiry, strict raw status, safe representation, real StreamGet quality keys, and injectable
candidate probing. The first complete focused run reported exit code `1`;
`9 failed, 27 passed, 1 deselected in 0.26s`. Eight failures accepted unsafe URL forms and
one showed missing signed-URL expiry derivation. No live-network test ran.

## GREEN — hardened StreamGet boundary

The adapter now accepts only raw status integer `2` or `4`, classifies known rate-limit
responses and exceptions, separates timeout/network failures, validates media URLs without
DNS resolution, recognizes sanitized `FULL_HD1`, `HD1`, `SD1`, and `SD2` structures, and
emits only a non-sensitive count when unknown quality keys are ignored. An injectable async
candidate probe supports per-protocol filtering and ranked quality fallback; without a probe,
Task 6 remains responsible for ffprobe-level playability verification. Signed URL expiry is
derived from `expiry`, `expires`, `expire`, or hexadecimal `wsTime`, otherwise a documented
five-minute lifetime is used. URL-bearing `ResolvedStream` fields are excluded from repr.

The focused GREEN run reported exit code `0`; `38 passed, 1 deselected in 0.07s`.
After explicit 429/retry-after and timeout classification regressions were added, the final
focused run reported `40 passed, 1 deselected in 0.09s`.
