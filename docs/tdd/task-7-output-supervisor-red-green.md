# Task 7 output supervisor TDD evidence

## Scope

The runtime integration cases use only `sys.executable` plus
`tests/fixtures/child_process.py`. It performs no network requests and does not contact a live
streaming platform. The 13 collected cases cover process start/health/stop, timeout kill,
concurrent idempotent stop, exit status, bounded and redacted stderr, FFmpeg progress metrics,
the exact retry schedule, terminal authentication failures, destination/process isolation, and
cancellation cleanup.

## RED

Tests and the deterministic child fixture were created before production code.

Command:

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_output_supervisor.py -x
```

Observed result (exit code `1`):

```text
collected 0 items / 1 error
E   ModuleNotFoundError: No module named 'restream_studio.media.process'
ERROR tests/integration/test_output_supervisor.py
```

The failure was expected because `AsyncProcess` and `OutputSupervisor` did not yet exist.

## GREEN implementation

- `AsyncProcess` launches argv exclusively through `asyncio.create_subprocess_exec`, with no
  shell, and gives every Windows child a new process group.
- Graceful stop uses `CTRL_BREAK_EVENT` on Windows and process-group `SIGTERM` on POSIX, then
  kills after a bounded timeout. Concurrent stops serialize and every stop path waits/reaps.
- stderr capture uses bounded chunk parsing rather than an unbounded `readline`; redaction is
  applied before any diagnostic enters snapshots or reprs.
- Metrics accept only `fps`, `bitrate`, `speed`, `out_time`, and `progress`, with bounded values.
- One supervisor owns one destination and at most one active child. It reuses `OutputState`,
  exposes validated `transition`, retries transient exits at `2, 5, 10, 20, 30, 30...`, and
  terminates retrying after authentication rejection.
- Cancellation closes the owned child and re-raises `CancelledError`.

## Windows async test environment blocker

The post-implementation focused test was run individually with an external pytest faulthandler
deadline as requested:

```powershell
.venv\Scripts\python.exe -m pytest `
  tests/integration/test_output_supervisor.py::test_async_process_starts_collects_metrics_and_stops_cleanly `
  -vv -o faulthandler_timeout=5
```

It did not enter the test body. The faulthandler stack consistently stopped while pytest-asyncio
was creating the Windows Proactor event loop:

```text
Timeout (0:00:05)!
File "socket.py", line 295 in accept
File "socket.py", line 627 in _fallback_socketpair
File "asyncio/proactor_events.py", line 787 in _make_self_pipe
File "asyncio/windows_events.py", line 316 in __init__
```

The independent reproduction
`.venv\Scripts\python.exe -c "import socket; print(socket.socketpair())"` also timed out. This
confirms a host socketpair restriction before application code executes, not an output supervisor
failure. The hung test process was terminated externally. No asyncio/test shim was added.

Collection remains healthy:

```text
13 tests collected
```

## Verification

- `npm run lint`: exit `0`, all checks passed (Ruff also reported two pre-existing inaccessible
  cache/temp paths).
- `npm run typecheck`: exit `0`, `Success: no issues found in 27 source files`.
- Synchronous regression slice excluding the host-blocked ffprobe asyncio module: exit `0`,
  `137 passed, 1 deselected`.
- `npm run build`: blocked before build backend execution by `PermissionError` creating pip's
  build-tracker file. Repointing `TEMP`/`TMP` to the workspace `build` directory produced the same
  host filesystem denial.
- Focused Task 7 runtime tests are blocked at Windows Proactor socketpair creation as evidenced
  above. Full `npm run verify` reaches pytest after clean lint/typecheck, then collection is blocked
  by a pre-existing inaccessible root `tmp0fe8fhgz` directory. No indefinite wait was left
  running.

## Specification review follow-up

Five additional regression cases were written before the review fixes:

1. stop racing a delayed `create_subprocess_exec` must reap the child that starts later;
2. cancellation during backoff must leave no named retry/stop tasks pending;
3. an authentication marker followed by 75 diagnostic lines must remain terminal after the marker
   is evicted from the 50-line tail;
4. timeout escalation must terminate both a fixture parent and its independently verified child
   PID;
5. `traceback.format_exc()`, `str`, `repr`, and `__cause__` must not expose a start-error URL.

The implementation adds an `AsyncProcess` lifecycle lock and stop intent, `finally` cleanup for
both backoff wait tasks, a sticky boolean `auth_failed`, Windows `taskkill /PID <integer> /T /F`
without a shell (with process-kill fallback), POSIX group kill, and `ProcessStartError from None`.
The suite now collects 16 Task 7 cases.

The requested process-local command set `socket._LOCALHOST = "127.0.0.1"`. On this host that
constant was already `127.0.0.1`, while Python's TCP fallback socketpair still blocked in
`accept`. A process-only UDP loopback wakeup pair allowed pytest-asyncio to start without adding
any repository shim. Under that runner, the traceback/cause regression passed. Real subprocess
cases then failed immediately at the host boundary with `PermissionError(13, WinError 5)` from
`asyncio.create_subprocess_exec`; they did not hang and no child application code ran. This is
separate from the implementation assertions and is retained here rather than reported as a green
runtime suite.

## Final review follow-up: public start cancellation and asynchronous taskkill

### RED

Two deterministic fake-process tests were added before implementation. With the process-local
loopback runner, both failed against the previous code:

- cancelling public `start()` timed out waiting for `child_stopped`, proving its internal runner
  continued in the background;
- the Windows escalation test timed out waiting for `taskkill_entered`, proving the implementation
  called synchronous `subprocess.run` instead of the patched async subprocess boundary.

### GREEN

`start()` now names and owns its newly created runner and live-wait task. Caller cancellation
cancels and awaits both, waits for runner cancellation cleanup, closes any child that crossed the
startup boundary, restores `STOPPED`, and re-raises `CancelledError`. A caller merely joining an
already-running supervisor does not acquire ownership of that existing runner.

Windows tree escalation now launches the fixed argv
`taskkill /PID <integer> /T /F` with `asyncio.create_subprocess_exec`. It applies a five-second
timeout, kills and reaps a stuck taskkill helper, falls back to the already-owned main-process
handle when taskkill fails, and never invokes a shell. The concurrent regression proves peer
event-loop work progresses while the fake taskkill helper is deliberately blocked.

Focused GREEN command result:

```text
2 passed, 1 warning in 0.08s
```

No loopback or asyncio shim was added to the repository.

## Quality review follow-up

Six review regressions were added before production changes. RED evidence included a concurrent
start joiner timing out on `_live` after the runner failed, and the FFmpeg command assertion
reporting zero `-progress` arguments. The new cases cover joiner failure/stop termination,
contextual 403 classification, parent-exits/child-survives cleanup, reconnect reset across an
explicit stop/start boundary, taskkill-helper cancellation cleanup, and production progress argv.

Implementation changes:

- every start caller races its own bounded live-event task against the shared runner; the owner
  still owns cancellation cleanup, while joiners receive runner failure, cancellation, or an
  explicit `stopped before becoming live` error instead of waiting forever;
- auth matching uses bounded phrases and HTTP/RTMP status context, never a bare `403` substring;
- the process group ID is retained on POSIX and killed after the parent exits; Windows children are
  attached at startup to a kill-on-close Job Object, with async taskkill as a fallback;
- a newly owned public-start session resets `reconnect_count`, while reconnects within that run
  remain monotonic;
- taskkill uses a separately tracked waiter which is killed and shield-reaped on timeout or caller
  cancellation; main-process fallback waits are shielded on cancellation;
- generated FFmpeg argv now contains `-progress pipe:2 -nostats`; ordinary diagnostics and parsed
  progress lines continue sharing the bounded redacted stderr reader.

Runnable focused evidence on this host:

```text
7 passed, 1 warning in 0.08s
29 passed, 1 warning in 0.06s
```

Real nested-child cases remain subject to the documented host `CreateProcess` denial. They retain
per-test asyncio deadlines and deterministic local Python fixtures; no test shim was committed.

Final verification after the quality-review implementation:

```text
8 runnable Task 7 cases passed in 0.09s
138 synchronous regression cases passed, 1 deselected, in 0.19s
25 Task 7 cases collected
Ruff: All checks passed
mypy: Success, no issues found in 27 source files
```

## Remaining review closure

RED was recorded for the final three review items:

- the deterministic delayed-runner test failed because `_run_generation` did not exist and the
  old `run()` could clear a stop intent after runner creation;
- the metrics test retained `out_time_seconds=3599999996400.0` for an excessive timestamp;
- the failed-Job-attach test required taskkill to precede any graceful parent signal.

GREEN changes use one supervisor lifecycle lock and a monotonically increasing generation. A new
public session clears stop intent before creating its runner; `_run_generation` never clears it,
so a stop between `create_task` and first runner execution remains authoritative. FFmpeg out-time
is finite and bounded to seven days (`604800` seconds), including the exact boundary.

On Windows, Job attachment remains immediate after `create_subprocess_exec` returns. There is an
unavoidable very short create-to-attach interval because `CREATE_SUSPENDED` would break the public
asyncio subprocess contract and private transports are intentionally not used. Production output
children are FFmpeg processes and do not normally create descendants in that interval. If Job
attachment fails, `tree_control_failed` is exposed in the safe snapshot and stop escalates through
async taskkill while the parent PID is still alive, before attempting graceful parent exit.

Final focused evidence:

```text
11 runnable Task 7 cases passed, 17 deselected, in 0.09s
138 synchronous regression cases passed, 1 deselected, in 0.19s
28 Task 7 cases collected
Ruff and strict mypy passed
```
