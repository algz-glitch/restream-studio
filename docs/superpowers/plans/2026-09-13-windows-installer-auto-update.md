# Windows Installer and Auto Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a standard Windows installer with shortcuts, uninstall support, localhost firewall setup, and a verified GitHub Releases auto-update workflow for `algz-glitch/restream-studio`.

**Architecture:** Keep update networking, validation, state management, and installer execution in separate Python modules behind the existing localhost-only FastAPI boundary. Build the per-user installer with Inno Setup and publish deterministic release metadata through GitHub Actions. The frontend exposes real update state and explicit install/restart actions without interrupting an active relay.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, React/TypeScript, PyInstaller, Inno Setup 6, GitHub Actions, GitHub Releases.

---

### Task 1: Strict update contracts and GitHub client

**Files:**
- Create: `src/restream_studio/update/__init__.py`
- Create: `src/restream_studio/update/contracts.py`
- Create: `src/restream_studio/update/client.py`
- Test: `tests/unit/test_update_contracts.py`
- Test: `tests/unit/test_update_client.py`

- [ ] **Step 1: Write failing contract tests**

Test exact SemVer ordering, `schema_version == 1`, 64-character SHA-256, positive size, HTTPS-only URLs, `github.com/algz-glitch/restream-studio/releases/download/` allowlisting, malformed JSON rejection, and downgrade rejection.

```python
def test_manifest_accepts_only_the_configured_github_release_path() -> None:
    manifest = UpdateManifest.model_validate(VALID)
    assert manifest.version == Version(0, 1, 1)
    with pytest.raises(ValidationError):
        UpdateManifest.model_validate({**VALID, "installer_url": "https://evil.example/setup.exe"})
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_update_contracts.py tests\unit\test_update_client.py -q`

Expected: collection failure because update modules do not exist.

- [ ] **Step 3: Implement immutable contracts and bounded client**

Define `Version`, `UpdateManifest`, `UpdateCheckResult`, `UpdateErrorCode`, and `GitHubUpdateClient`. Use `urllib.request` with a 10-second timeout, a 1 MiB manifest limit, streamed installer download, an exact byte-count check, and SHA-256 verification before `os.replace(partial, final)`.

```python
MANIFEST_URL = "https://github.com/algz-glitch/restream-studio/releases/latest/download/latest.json"

def validate_download_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        raise ValueError("installer URL is not approved")
    if not parsed.path.startswith("/algz-glitch/restream-studio/releases/download/"):
        raise ValueError("installer URL is outside the configured repository")
    return value
```

- [ ] **Step 4: Run focused lint, types, and tests**

Run: `.venv\Scripts\python.exe -m ruff check src\restream_studio\update tests\unit\test_update_contracts.py tests\unit\test_update_client.py`

Run: `.venv\Scripts\python.exe -m mypy src\restream_studio\update tests\unit\test_update_contracts.py tests\unit\test_update_client.py`

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_update_contracts.py tests\unit\test_update_client.py -q`

Expected: all commands pass.

- [ ] **Step 5: Commit**

```powershell
git add src/restream_studio/update tests/unit/test_update_contracts.py tests/unit/test_update_client.py
git commit -m "feat: add verified GitHub update client"
```

### Task 2: Update service, helper, and protected API

**Files:**
- Create: `src/restream_studio/update/service.py`
- Create: `src/restream_studio/update/helper.py`
- Modify: `src/restream_studio/api/schemas.py`
- Modify: `src/restream_studio/api/routes.py`
- Modify: `src/restream_studio/main.py`
- Test: `tests/unit/test_update_service.py`
- Test: `tests/integration/test_api.py`

- [ ] **Step 1: Write failing state and API tests**

Cover idle/current/available/downloading/ready/failed states, 24-hour automatic check throttling, single-flight checks, active-relay install rejection, authenticated mutation requirements, and secret-safe fixed diagnostics.

```python
response = client.post("/api/update/install", json={})
assert response.status_code == 409
assert response.json()["error"]["code"] == "relay_must_be_stopped"
```

- [ ] **Step 2: Confirm focused failures**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_update_service.py tests\integration\test_api.py -q`

Expected: update imports or routes are missing.

- [ ] **Step 3: Implement service and helper**

Persist only non-secret update timestamps/status under the existing application data directory. Expose `GET /api/update`, `POST /api/update/check`, `POST /api/update/download`, and `POST /api/update/install`. The install route checks `desired_running is False`, requires a verified downloaded installer, and launches the bundled helper with a PID, installer path, installed EXE path, and restart flag.

```python
if snapshot.desired_running:
    raise ApiError(409, "relay_must_be_stopped", "Stop all outputs before installing")
await deps.update_service.install(current_pid=os.getpid())
```

- [ ] **Step 4: Verify focused and full backend tests**

Run: `.venv\Scripts\python.exe -m ruff check src tests`

Run: `.venv\Scripts\python.exe -m mypy src tests`

Run: `.venv\Scripts\python.exe -m pytest tests -q`

Expected: all selected tests pass with only documented upstream deprecation warnings.

- [ ] **Step 5: Commit**

```powershell
git add src/restream_studio tests/unit/test_update_service.py tests/integration/test_api.py
git commit -m "feat: add protected update service"
```

### Task 3: Frontend update center

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/components/UpdateCard.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/tests/api.test.ts`
- Modify: `frontend/src/tests/App.test.tsx`

- [ ] **Step 1: Write failing frontend tests**

Test current/available/downloading/ready/failure render states, manual check, download, install, active-relay disabled state, double-click prevention, abort handling, and fixed error copy.

```tsx
expect(await screen.findByRole('region', { name: '软件更新' })).toBeVisible()
await user.click(screen.getByRole('button', { name: '检查更新' }))
expect(api.checkUpdate).toHaveBeenCalledTimes(1)
```

- [ ] **Step 2: Confirm failures**

Run: `npm --prefix frontend test -- src/tests/api.test.ts src/tests/App.test.tsx`

Expected: missing update types and component assertions fail.

- [ ] **Step 3: Implement typed API and responsive card**

Add strict runtime validators and authenticated actions. Render version, state, progress, release link, and one valid primary action at a time. Never display local download paths or raw exception text.

- [ ] **Step 4: Verify frontend**

Run: `npm run typecheck`

Run: `npm run lint`

Run: `npm test`

Run: `npm run frontend:build`

Expected: all frontend gates pass.

- [ ] **Step 5: Commit**

```powershell
git add frontend
git commit -m "feat: add software update center"
```

### Task 4: Inno Setup installer and uninstall lifecycle

**Files:**
- Create: `packaging/restream-studio.iss`
- Create: `packaging/firewall-install.ps1`
- Create: `packaging/firewall-remove.ps1`
- Create: `scripts/build-installer.ps1`
- Modify: `packaging/restream-studio.spec`
- Modify: `scripts/package.ps1`
- Modify: `scripts/verify.ps1`
- Modify: `tests/unit/test_packaging_contract.py`

- [ ] **Step 1: Write failing packaging contract tests**

Assert per-user installation, x64 mode, fixed AppId, desktop/start-menu icons, uninstall registration, data preservation by default, optional data deletion, firewall install/remove hooks, bundled update helper, and output filename.

- [ ] **Step 2: Confirm packaging contract failures**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_packaging_contract.py -q`

Expected: missing `.iss` and installer markers fail.

- [ ] **Step 3: Implement installer and build script**

Use `PrivilegesRequired=lowest` for per-user files and an elevated PowerShell helper only for exact-program loopback firewall changes. Build with `ISCC.exe`; discover a local Inno installation first and otherwise install Inno Setup through pinned `winget` package metadata.

```ini
[Setup]
DefaultDirName={localappdata}\Programs\RestreamStudio
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName=Restream Studio

[Icons]
Name: "{autodesktop}\Restream Studio"; Filename: "{app}\RestreamStudio.exe"
Name: "{group}\Restream Studio"; Filename: "{app}\RestreamStudio.exe"
```

- [ ] **Step 4: Build and inspect installer**

Run: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build-installer.ps1 -Clean`

Expected: `dist\installer\RestreamStudio-Setup-0.1.0.exe` exists and its SHA-256 is printed.

- [ ] **Step 5: Commit**

```powershell
git add packaging scripts tests/unit/test_packaging_contract.py
git commit -m "feat: build standard Windows installer"
```

### Task 5: GitHub repository and release automation

**Files:**
- Create: `.github/workflows/release.yml`
- Create: `scripts/write-update-manifest.py`
- Create: `tests/unit/test_release_workflow_contract.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing workflow tests**

Assert Windows runner, tag-only release trigger, locked Node/Python setup, full `verify.ps1`, installer build, SHA-256 manifest generation, artifact upload, Release upload, and `contents: write` permissions.

- [ ] **Step 2: Confirm workflow failures**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_release_workflow_contract.py -q`

Expected: workflow and manifest writer are absent.

- [ ] **Step 3: Implement manifest writer and workflow**

The manifest writer receives version, installer path, repository and tag, calculates size/hash itself, and writes strict schema version 1 JSON. The workflow publishes only when every gate passes.

- [ ] **Step 4: Create and push the public repository**

Create `algz-glitch/restream-studio` as public through the authenticated GitHub UI, add it as `origin`, push the feature branch, and verify the repository page and branch commit. Login, CAPTCHA, and final Release publication remain user-controlled.

- [ ] **Step 5: Commit workflow and documentation**

```powershell
git add .github scripts/write-update-manifest.py tests/unit/test_release_workflow_contract.py README.md
git commit -m "ci: publish verified GitHub releases"
```

### Task 6: Installation, upgrade, uninstall, and regression acceptance

**Files:**
- Create: `scripts/smoke-installer.ps1`
- Modify: `scripts/verify.ps1`
- Modify: `tests/unit/test_packaging_contract.py`

- [ ] **Step 1: Add installer smoke contract**

Require isolated install directory, shortcut checks, registry uninstall checks, exact loopback firewall boundary, packaged API smoke, configuration persistence, controlled upgrade, uninstall, port/process cleanup, and optional data retention evidence.

- [ ] **Step 2: Execute a real first install**

Run the generated installer with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=<isolated path>`, then verify installed files, shortcuts, uninstall registry entry, and application health.

- [ ] **Step 3: Execute controlled update and upgrade tests**

Serve a local manifest/installer fixture through a loopback HTTP adapter injected only in tests. Verify available/download/hash mismatch/success/active-relay block. Run the installer again as an upgrade and verify source/destination configuration remains present and masked.

- [ ] **Step 4: Execute uninstall tests**

Call the registered uninstaller silently. Verify application files, shortcuts, processes, ports, and firewall rule are removed while user data remains. Execute a second isolated install with the delete-data task and verify its isolated data root is removed.

- [ ] **Step 5: Run every release gate**

Run: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1`

Expected: Python lint/types/tests, frontend types/lint/tests/build, local dual-target RTMP E2E, package smoke, installer build, and installer lifecycle smoke all pass.

- [ ] **Step 6: Record final evidence and commit**

```powershell
git add scripts/smoke-installer.ps1 scripts/verify.ps1 tests/unit/test_packaging_contract.py
git commit -m "test: close installer update lifecycle"
```

Record installer path, SHA-256, Git commit, test counts, screenshot paths, and explicit unsigned status. Do not label official Douyin or WeChat Channels platform acceptance as passed without their real preview evidence.
