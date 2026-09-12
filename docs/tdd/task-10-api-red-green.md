# Task 10 local API TDD evidence

## Scope

Task 10 adds only the localhost FastAPI control boundary: strict Pydantic v2 contracts,
dependency-injected source/destination/control/test/event routes, structured errors, fixed secret
masking, optimistic write conflict handling, lifecycle cleanup, optional frontend mounting, and
localhost Host/Origin/session-token enforcement. The destination test operation is injected and
bounded; only an explicit user request runs a one-second FFmpeg publication probe.

## RED

`tests/integration/test_api.py` was created before the production API package. The first focused
run was:

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_api.py -q -p no:cacheprovider
```

It failed during collection for the expected missing feature:

```text
ModuleNotFoundError: No module named 'restream_studio.api'
1 error in 0.55s
```

The test file covers all routes plus strict bodies, closed destination kinds, canonical source
storage, structured errors, response/repr/log/OpenAPI secret absence, start prerequisites,
explicit local-test mode, idempotent stop, isolated reconnect, bounded injected diagnostics,
event cursor bounds, unsupported filters, Host/Forwarded/Origin/token controls, lifecycle cleanup,
optional static assets, and concurrent idempotent/conflicting writes.

## GREEN implementation

- `api/schemas.py` uses `ConfigDict(extra="forbid", strict=True)`, constrained strings and
  `SecretStr`; validators reject controls, invalid Douyin identities, malformed RTMP endpoints,
  key-bearing server paths, invalid ports, and out-of-range lengths.
- Source writes reuse `normalize_douyin_url`; only the canonical identity reaches persistence.
- Destination responses expose a constant `********` mask and never serialize plaintext,
  ciphertext, key length, or suffix. The only accepted kinds are `douyin`, `wechat_channels`, and
  `local_test`.
- Start requires a source and either an enabled/configured real destination or an explicitly
  requested local destination while local-test mode is enabled. Stop delegates idempotently.
- Reconnect receives one enum target. Destination tests use an injected adapter under a five-second
  timeout and return one of two fixed diagnostics.
- Events accept only bounded integer `limit`/`cursor`, reject extra filter parameters, use the
  persistence API rather than caller SQL, and apply a final recursive redaction pass.
- A process-start session token, exact same-host HTTP Origin, direct localhost Host, and rejection
  of all forwarding headers protect mutations and DNS rebinding. CORS middleware is absent.
- The lifespan opens dependencies, initializes the controller, then always stops the controller
  and closes the database. Static assets mount only when an injected directory exists, after an
  explicit API catch-all.
- Uvicorn binds `127.0.0.1` and disables proxy-header trust.

## Verification and environment block

The API test module collects successfully:

```text
27 tests collected in 0.46s
```

Full HTTP execution was attempted after implementation. This managed Windows environment blocks
the local socket pair used by AnyIO/TestClient while constructing its event loop; the run stalled
at the first test. A five-second faulthandler capture located the block in
`socket._fallback_socketpair -> asyncio.proactor_events._make_self_pipe`, before application
startup or any endpoint code. The command was stopped rather than left running. This is an
environment execution block, not a passing GREEN claim.

The no-network schema/OpenAPI smoke completed:

```text
schema/openapi smoke: PASS
```

Static verification after the implementation uses:

```powershell
.venv\Scripts\python.exe -m ruff check src/restream_studio/api src/restream_studio/main.py tests/integration/test_api.py
.venv\Scripts\python.exe -m mypy src tests
.venv\Scripts\python.exe -m compileall -q src/restream_studio/api src/restream_studio/main.py tests/integration/test_api.py
```

The focused HTTP suite should be rerun on a host that permits Python's internal event-loop socket
pair. No network or real platform credentials are required by the tests.

## Specification review follow-up

Three review regressions were added test-first. The first focused RED failed at import because the
new pure start-precondition helper did not exist. After adding the helper, the corrected focused
suite is GREEN:

```text
11 passed, 28 deselected in 0.49s
```

The follow-up makes mutation authorization fail closed when lifespan has not generated a nonempty
token while still invoking `secrets.compare_digest`; an async `ASGITransport` regression directly
targets the app without lifespan, and a socket-free direct invocation returned the fixed 403.
RTMP(S) host validation now rejects all whitespace, malformed
DNS labels, malformed or nonglobal IP literals, userinfo, controls, missing hosts, unsafe ports,
and key-bearing paths. Pure schema/helper tests avoid network and lifecycle dependencies. Start
validation now preserves the established error order: destination readiness first, then source
configuration. After the quality review additions, the full file collects 46 test cases.

## Quality review closure

The P0 quality review removes the placeholder controller. `RuntimeManager` now reads only complete
database configuration and dynamically assembles `DouyinResolver`, the bounded media probe,
credential-bearing destination adapters, `OutputSupervisor` instances, and a real `Controller`.
No dummy stream key is synthesized. Default reconnect and short connection-test callbacks are the
runtime methods; tests can still inject deterministic substitutes without platform access.

Configuration PUT operations now hold one async write lock across persistence and runtime rebuild.
They merge omitted destination fields, rebuild from the new source/server/key/enabled values, and
restore the prior database snapshot plus runtime when application fails. A running controller is
stopped before the replacement is exposed and the replacement resumes when the old runtime was
running. Concurrent stale writes return 409, while a byte-for-byte semantic retry is idempotent.

Migration 4 adds a checked JSON revision map to the existing singleton `app_settings` table.
Source and each destination have independent persisted revisions. GET emits an ETag, PUT requires
quoted `If-Match`, and successful semantic changes increment the persisted value. A reopen test
proved source revision 7 and destination revision 3 survive database close/open.

Real destinations perform asynchronous DNS resolution immediately before a test or FFmpeg
connection and reject the operation if any returned address is loopback, private, link-local,
reserved, or otherwise non-global. Explicit `local_test` accepts only loopback resolution, with
`localhost` supported. DNS can change between validation and the child process connection; this
TOCTOU residual remains bounded by revalidation on every reconnect and cannot be eliminated
without pinning an address, which would change TLS/SNI and platform routing semantics. No fixed
SNI is fabricated.

`GET /api/session` is Host-checked, exact-origin checked, non-cacheable, and returns the generated
session token only during an active lifespan. Shutdown clears the token, app dependency reference,
and runtime references before closing SQLite; a later lifespan generates a different token. The
API catch-all precedes static assets and covers GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS, TRACE,
and CONNECT with the same JSON 404.

Additional focused evidence:

```text
14 pure schema/handler/security/runtime tests passed
29 ffmpeg command regressions passed
59 database tests passed, including persisted-revision reopen and stale transactional write
ASGI lifecycle/session/etag/catchall: PASS
ASGI persisted-write/runtime-apply rollback: PASS
default RuntimeManager unconfigured/start guard: PASS
RuntimeManager real Controller start/stop with offline injected resolver: PASS
ruff: All checks passed
mypy: Success, 40 source files
```

The full TestClient suite remains subject to the recorded managed-host event-loop socket-pair
restriction. The direct ASGI checks use no platform network and the DNS policy test injects fixed
answers. Production DNS/TCP functions were not invoked by tests.

## Quality re-review closure

The re-review began with three focused RED failures: `Controller.shutdown` and the atomic database
snapshot methods did not exist, and `restream_studio.destination_test` failed import during test
collection. The new tests were then driven GREEN without platform or network access.

- Controller shutdown now has a non-persisting path. Runtime replacement and application shutdown
  stop/reap old output processes without writing the old controller's desired/enabled state back
  over a newly committed API transaction. Operator `/stop` retains the persisting path.
- A runtime rebuild observes both the outgoing runtime intent and the desired-running value loaded
  by the replacement controller. Persistent `desired_running=true` therefore starts monitoring on
  process restart. A synchronous resume failure is represented as a safe `ERROR` snapshot rather
  than a false running state; configuration-apply failures still propagate so the API can restore
  the old database snapshot and revision.
- Source and destination GETs now obtain content and ETag revision in one SQLite read transaction.
  Start obtains source plus all public destination readiness fields in one transaction, and start,
  stop, and configuration PUTs share the API write lock. Revision increments remain inside the
  successful configuration write transaction only.
- `DestinationTester` first validates every DNS answer. Its TCP/TLS preflight connects to one of
  those validated IP literals; RTMPS uses a verifying default SSL context and passes the original
  hostname as `server_hostname`, preserving certificate verification and SNI. It then invokes a
  bounded FFmpeg probe with generated black video and silence for one second against the complete
  server/key URL. Timeout and oversized stderr paths kill and reap the child, authentication text
  maps only to a fixed failed diagnostic, and command repr/display forms contain only a constant
  mask. Fake connector/process tests prove the validated IP and original SNI are used and that the
  secret exists only in the subprocess argv.
- FFmpeg's normal network output API does not expose a supported way to pin an already-resolved IP
  while independently retaining the RTMPS hostname for SNI/certificate checks. Production starts
  and reconnects therefore revalidate all current addresses immediately before process creation,
  but FFmpeg resolves the hostname again. DNS changes in that interval remain an explicit TOCTOU
  residual; this implementation does not claim IP pinning for the production FFmpeg connection.
- Lifespan cleanup attempts runtime shutdown, clears the token and dependency references, clears
  runtime references, and closes SQLite independently. A cleanup failure produces one fixed local
  lifecycle error only after the remaining cleanup steps have run.

Focused GREEN evidence for this re-review:

```text
34 passed: full controller integration module plus four DestinationTester regressions
60 passed: full database unit module, including atomic snapshot and revision tests
14 passed: socket-free API schema/security/helper tests plus focused additions
3 passed: runtime resume/rebuild, safe resume failure, and failure-tolerant lifespan cleanup
mypy: Success, 42 source files
ruff: All checks passed
```

The managed Windows event-loop socket-pair limitation remains applicable to the full TestClient
module. No network, DNS lookup, platform credential, or real publishing endpoint was used in the
GREEN evidence above.

A wider synchronous regression command was also attempted and stopped at the requested 30-second
bound after 31 progress markers rather than being left running; it produced no failure before the
stop, but is not reported as a passing suite.
