# Task 11 frontend TDD evidence

## Scope

Task 11 adds the React 19 operator dashboard only. It consumes the existing Task 10 API and does
not alter backend schemas, controller behavior, credentials, or platform adapters. `PRODUCT.md`
and `DESIGN.md` are committed with the implementation.

## RED

Tests were written before `frontend/src/api.ts`, `App.tsx`, or production components existed.
The initial focused command was:

```powershell
npm test -- --run
```

The follow-up RED pass added focused regressions for partial initialization failure, per-section
retry, cross-runtime `AbortError`, event request sequencing, two-second visibility polling, session
bootstrap failure, backend-message suppression, and dialog trigger focus restoration. Before the
implementation changed, the focused run stopped during collection because the pre-existing partial
`node_modules` tree lacked `@vitest/utils`; rebuilding the lock from the required offline cache then
reported the exact unavailable metadata request as
`https://registry.npmjs.org/@testing-library%2fjest-dom` (`ENOTCACHED`).

The test sources nevertheless precede production implementation and cover:

- session/no-store, mutation token and ETag headers, structured 409/412/422 errors, AbortSignal;
- stable loading skeleton, empty setup guidance, all required status variants;
- source validation, canonical response, field feedback, disabled pending controls and conflicts;
- fixed key masks with empty password values and independent destination reconnect operations;
- start, active-output stop confirmation, keyboard Escape and initial confirmation focus;
- log filtering, pagination, retry, empty state and conservative display redaction;
- visible-only polling, hidden/unmount aborts and stale response sequencing;
- landmark order used by the responsive one-column layout.

The Task 11 quality follow-up added RED regressions before its production edits for:

- completion-based status polling that never aborts a slow visible request on a two-second tick;
- immediate hidden-state cancellation, immediate visible-state refresh, and resumed scheduling;
- fixed, redacted fallback copy when a 409/412 conflict refresh also fails;
- native table header/data-cell semantics;
- Tab and Shift+Tab dialog focus cycling plus background `inert`/`aria-hidden` restoration;
- the root frontend-then-wheel build pipeline and a real FastAPI GET of `/` plus every referenced
  `/assets/` build artifact.

## GREEN implementation

- `ApiClient` centralizes typed requests, ETag storage, structured errors, same-origin credentials,
  no-store session retrieval, browser-owned Origin, token headers, and cancellation.
- `App` starts all six initial resources concurrently but settles each source, destination, status,
  log, and session section independently. Every failed section has a real scoped retry, while only
  its own pending state renders a skeleton.
- Status polling uses one in-flight request and recursive `setTimeout`, scheduling the next request
  two seconds after completion. Hidden state clears scheduling and aborts immediately; visible state
  refreshes immediately. Sequence guards still reject adapters that ignore `AbortSignal`.
- Source and destination components keep independent request/error state and never put an existing
  stream key into an input value. Conflict responses refresh the affected resource, and a failed
  refresh is caught separately with fixed frontend-only copy.
- Start/stop/reconnect/test/save controls call real API methods. Control failures use fixed frontend
  copy rather than rendering backend `message` values. Stop confirmation appears only for active
  outputs, restores focus to its trigger, and supports Escape dismissal.
- Monitor uses native `table`, `th`, and `td` semantics. States combine shape and text. Missing metrics remain an explicit em dash because the
  current backend status schema does not expose numeric FPS/bitrate/speed/uptime/reconnect fields.
- Logs are cursor-paged, locally filtered, and recursively redacted again before rendering.
- Vite emits directly to `src/restream_studio/static`; default FastAPI dependencies mount that
  packaged directory and setuptools includes its generated files in distributions. Root `build`
  runs `frontend:build` before `backend:wheel`, while `backend:wheel` remains independently callable.
- The CSS is expanded into maintainable rules and uses the approved OKLCH palette, fixed
  12px-or-less panel corners, no gradients or glass,
  equal desktop outputs, mobile single-column flow, visible focus, AA-oriented contrast, and
  reduced-motion handling.

## Verification

Bounded follow-up verification (each command capped at 60 seconds):

```text
pytest -k packaged                         PASS (1 passed, 51 deselected)
full test_api.py                           TERMINATED (60 seconds without output)
npm install --offline                      BLOCKED: ENOTCACHED for
  https://registry.npmjs.org/@testing-library%2fjest-dom
npx typescript@5.9.3 --offline             BLOCKED: ENOTCACHED for
  https://registry.npmjs.org/typescript
```

The original build symptom was reproduced as `ERR_MODULE_NOT_FOUND: rollup`. An initial offline
install exposed a damaged partial dependency tree; a clean lock/install could not be recreated from
`G:\CodexData\cache\npm` because the metadata entries above were unavailable to npm. Therefore no
fabricated or incomplete `package-lock.json` is committed, and no dependency/cache tree is vendored.
Frontend test, lint, typecheck, build, generated-asset GET, and full backend verification remain
unverified on this host until those exact cache entries are supplied.

For the quality follow-up, `npm shrinkwrap` was attempted and produced only the workspace shell plus
the pre-existing partial Rollup tree. `npm install --package-lock-only --offline` then failed with
`ENOTCACHED` for `@testing-library/jest-dom`. The incomplete shrinkwrap was deleted and is not part
of the commit. Fresh short checks passed for Ruff, mypy, and the build-pipeline contract test; the
frontend-dependent checks remain blocked by the same incomplete dependency tree.
