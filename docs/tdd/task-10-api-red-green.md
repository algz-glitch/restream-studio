# Task 10 local API TDD evidence

## Scope

Task 10 adds only the localhost FastAPI control boundary: strict Pydantic v2 contracts,
dependency-injected source/destination/control/test/event routes, structured errors, fixed secret
masking, optimistic write conflict handling, lifecycle cleanup, optional frontend mounting, and
localhost Host/Origin/session-token enforcement. The destination test operation is injected and
bounded to five seconds; it does not start a long-running publication.

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
configuration. The full file now collects 39 test cases.
