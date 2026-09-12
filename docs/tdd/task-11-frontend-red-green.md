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

The first attempt could not collect the intentionally missing modules because this managed host's
offline npm cache lacked package metadata for several already-cached tarballs. After narrowly
recovering the installed test runner, collection reached Vite but this host rejected its child
process creation with `spawn EPERM`. The failure occurs in Vite/esbuild before a test module is
transformed, not in application code. A threads-pool retry removed the Vitest fork requirement but
the same host restriction blocked Vite's esbuild transform.

The test sources nevertheless precede production implementation and cover:

- session/no-store, mutation token and ETag headers, structured 409/412/422 errors, AbortSignal;
- stable loading skeleton, empty setup guidance, all required status variants;
- source validation, canonical response, field feedback, disabled pending controls and conflicts;
- fixed key masks with empty password values and independent destination reconnect operations;
- start, active-output stop confirmation, keyboard Escape and initial confirmation focus;
- log filtering, pagination, retry, empty state and conservative display redaction;
- visible-only polling, hidden/unmount aborts and stale response sequencing;
- landmark order used by the responsive one-column layout.

## GREEN implementation

- `ApiClient` centralizes typed requests, ETag storage, structured errors, same-origin credentials,
  no-store session retrieval, browser-owned Origin, token headers, and cancellation.
- `App` loads six initial resources concurrently, keeps a stable skeleton, polls every two seconds
  only while visible, aborts superseded/unmounted requests, and ignores stale status responses.
- Source and destination components keep independent request/error state and never put an existing
  stream key into an input value. Conflict responses refresh the affected resource.
- Start/stop/reconnect/test/save controls call real API methods. Stop confirmation appears only for
  active outputs and supports focus plus Escape dismissal.
- Monitor states combine shape and text. Missing metrics remain an explicit em dash because the
  current backend status schema does not expose numeric FPS/bitrate/speed/uptime/reconnect fields.
- Logs are cursor-paged, locally filtered, and recursively redacted again before rendering.
- The CSS uses the approved OKLCH palette, fixed 12px-or-less panel corners, no gradients or glass,
  equal desktop outputs, mobile single-column flow, visible focus, AA-oriented contrast, and
  reduced-motion handling.

## Verification

Completed on this host:

```text
npm run typecheck  PASS
npm run lint       PASS (strict TypeScript lint gate)
git diff --check   PASS (line-ending notice only)
```

Attempted but host-blocked before application execution:

```text
npm test           Vite/esbuild child process: spawn EPERM
npm run build      Vite/esbuild child process: spawn EPERM
```

The production source contains no credential literals. Test-only sentinel secrets are excluded by
the Vite entry graph. Because a build and preview could not start on this host, no browser screenshots
are claimed. Rerun `npm install`, `npm test`, and `npm run build` on a host that permits esbuild child
processes; no real platform access is required.
