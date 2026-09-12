# Task 8 controller TDD evidence

## Scope

Task 8 adds one deterministic controller for one configured source. Resolver, media probe,
destination supervisors, wall/monotonic clock, sleep, and optional persistence are injected
through protocols. Public snapshots and persisted state are immutable and contain identities and
desired state only; resolved source URLs, destination URLs, and stream keys never enter them.

The committed integration suite is pure fake: it creates no socket, network request, or child
process. Its small synchronous coroutine driver avoids constructing the host-blocked Windows
Proactor event loop.

## RED

The controller integration tests were written before production code.

Initial command:

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_controller.py -x -q
```

Observed expected failure (exit code `1`):

```text
E   ModuleNotFoundError: No module named 'restream_studio.orchestration'
ERROR tests/integration/test_controller.py
```

Two review regressions also produced genuine RED results before their fixes:

- a failed standby restart was never retried on a later source failure (`'live' != 'standby'`);
- `stop()` swallowed cancellation of its caller (`Failed: DID NOT RAISE CancelledError`).

The destination-identity hardening test also failed first because a raw RTMP URL was accepted as
a public identity.

## GREEN implementation

- A single controller task owns one configured room identity. Lifecycle and polling are
  serialized separately; every controller state mutation and snapshot publication uses an
  `asyncio.Lock`.
- Source attempts always resolve the room identity again, validate URL lifetime before probing,
  and never retain a resolved URL in snapshots or persistence.
- Initial offline state remains `MONITORING`; a valid live source is probed before enabled outputs
  receive prepared commands.
- Source failures use `2, 5, 10, 20, 30, 30...` seconds. Resolver rate limits use a positive
  `retry_after` verbatim. Output-supervisor state is observed but never interpreted as source
  loss, so one output reconnect cannot stop or restart its healthy peer.
- Sixty seconds of continuous source failure selects a validated local/generated standby ID.
  Commands are prepared before restart. Destination preparation/restart failures are isolated,
  and only the failed destination is retried.
- Standby recovery requires two successful resolve-plus-probe attempts at least three seconds
  apart. Any failed probe resets the success count.
- `start`, `stop`, and `cancel` are idempotent. Controller-task cancellation closes outputs and
  propagates; `stop()` distinguishes expected cancellation of its owned task from cancellation of
  the caller.

Focused GREEN command:

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_controller.py -q -p no:cacheprovider
```

Result:

```text
13 passed in 0.04s
```

## Lifecycle verification without a committed shim

A one-shot Python process replaced `socket.socketpair` with a UDP loopback pair only inside that
process, then ran the real `start()`/`stop()`/`cancel()` methods with fake ports. Two concurrent
starts retained one task; repeated stop/cancel completed; `asyncio.all_tasks()` found no leaked
background task:

```text
lifecycle: PASS; leaked tasks: 0
```

No event-loop or socket shim was added to the repository.

## Verification

```text
npm run lint
All checks passed!

npm run typecheck
Success: no issues found in 30 source files

.venv\Scripts\python.exe -m pytest tests/unit tests/integration/test_controller.py \
  --ignore=tests/unit/test_ffprobe.py -q -p no:cacheprovider
151 passed, 1 deselected in 0.20s
```

`npm run verify` completed Ruff and strict mypy, then pytest collection was blocked by two
pre-existing inaccessible root directories, `pytest-cache-files-c2lyex6b` and `tmp0fe8fhgz`
(`PermissionError: [WinError 5]`). The focused Task 8 suite and synchronous regression command
avoid those unrelated paths explicitly.

`npm run build` was attempted and blocked before the build backend ran because pip could not write
its build-tracker entry under `G:\CodexData\tmp` (`PermissionError: [Errno 13]`). This is an
environment filesystem restriction, not a package or Task 8 compilation failure.

## Specification review closure

Five deterministic regressions were added before changing the controller. After adding only the
new safe output-error enum required for test collection, the focused suite produced six behavioral
failures:

```text
6 failed, 12 passed in 0.24s
```

The RED failures proved that the previous implementation started outputs after a probe crossed the
URL-validity threshold, slept 30 seconds past the standby deadline, propagated an arbitrary
destination `Exception`, published `LIVE` after a concurrent disable, aborted cleanup on an
ordinary stop exception, and failed to clean the second destination after cancellation from the
first.

The GREEN changes are:

- resolved URL freshness is checked both before and immediately after media probing;
- every source-failure sleep is bounded by the remaining continuous-failure standby deadline, and
  waking on that deadline performs the standby switch without another delayed resolve cycle;
- preparation, restart, and stop failures publish only enum error categories, never exception text;
  ordinary `Exception` instances are isolated while `CancelledError`, `SystemExit`, and
  `KeyboardInterrupt` retain base-exception semantics;
- enabled/input/error/generation state shares the controller state lock. Output switching captures
  a generation, validates it immediately before restart, and validates it again afterward. A
  disable during an in-flight restart invalidates publication and forces the stale output stopped,
  without awaiting a supervisor while holding the state lock;
- all-output cleanup invalidates generations first, executes every destination in its own
  `try`/`finally`, records ordinary failures safely, completes later destinations after a
  cancellation, then re-raises the cancellation.

Focused GREEN result after this review:

```text
18 passed in 0.05s
```

Final review verification also passed Ruff, strict mypy over 30 source files, and the synchronous
regression slice with `156 passed, 1 deselected in 0.21s`.
