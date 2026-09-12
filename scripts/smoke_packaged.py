"""Run an end-to-end smoke test against the frozen Windows application."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: object
    text: str
    etag: str | None


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def dynamic_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def request(
    base_url: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
    timeout: float = 10.0,
) -> Response:
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    merged = {"Accept": "application/json", **(headers or {})}
    if encoded is not None:
        merged["Content-Type"] = "application/json"
    outbound = urllib.request.Request(
        f"{base_url}{path}", data=encoded, headers=merged, method=method
    )
    try:
        incoming = urllib.request.urlopen(outbound, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        etag = exc.headers.get("ETag")
    else:
        with incoming:
            raw = incoming.read()
            status = incoming.status
            etag = incoming.headers.get("ETag")
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        parsed = text
    return Response(status=status, body=parsed, text=text, etag=etag)


def expect(response: Response, status: int, label: str) -> dict[str, Any]:
    require(response.status == status, f"{label}: expected HTTP {status}, got {response.status}: {response.text}")
    require(isinstance(response.body, dict), f"{label}: response is not a JSON object")
    return cast(dict[str, Any], response.body)


def require_etag(response: Response, label: str) -> str:
    require(response.etag is not None, f"{label}: ETag is missing")
    return cast(str, response.etag)


def start_package(executable: Path, data_root: Path, port: int, run_root: Path) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["RESTREAM_STUDIO_PORT"] = str(port)
    env["RESTREAM_STUDIO_DATA_DIR"] = str(data_root)
    stdout = (run_root / f"package-{port}.stdout.txt").open("wb")
    stderr = (run_root / f"package-{port}.stderr.txt").open("wb")
    try:
        process = subprocess.Popen(
            [str(executable)],
            cwd=executable.parent,
            env=env,
            stdout=stdout,
            stderr=stderr,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        stdout.close()
        stderr.close()
        raise
    stdout.close()
    stderr.close()
    return process


def wait_ready(process: subprocess.Popen[bytes], base_url: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        require(process.poll() is None, f"packaged application exited early with code {process.returncode}")
        try:
            health = request(base_url, "GET", "/health", timeout=2)
            if health.status == 200 and isinstance(health.body, dict) and health.body.get("status") == "ok":
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    raise RuntimeError("packaged application readiness timed out")


def stop_package(process: subprocess.Popen[bytes], port: int) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if port_available(port):
            return
        time.sleep(0.1)
    raise RuntimeError(f"packaged application did not release port {port}")


def find_edge() -> Path:
    candidates = (
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("Microsoft Edge is required for packaged visual smoke")


def session_success_count(log_file: Path) -> int:
    if not log_file.is_file():
        return 0
    return log_file.read_text(encoding="utf-8", errors="replace").count(
        '"GET /api/session HTTP/1.1" 200 OK'
    )


def browser_smoke(edge: Path, base_url: str, run_root: Path, server_log: Path) -> dict[str, str]:
    outputs: dict[str, str] = {}
    sessions_before = session_success_count(server_log)
    homepage = request(base_url, "GET", "/")
    require(homepage.status == 200, "packaged homepage did not return HTTP 200")
    require("<title>Restream Studio</title>" in homepage.text, "packaged homepage title is missing")
    homepage_file = run_root / "homepage.html"
    homepage_file.write_text(homepage.text, encoding="utf-8")
    outputs["homepage"] = str(homepage_file)
    for name, size in (("desktop", "1440,1000"), ("mobile", "390,844")):
        screenshot = run_root / f"ui-{name}.png"
        common = [
            str(edge),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--virtual-time-budget=5000",
            f"--window-size={size}",
        ]
        screenshot_command = [
            *common,
            f"--user-data-dir={run_root / f'edge-profile-{name}-shot'}",
            f"--screenshot={screenshot}",
            base_url,
        ]
        screenshot_run = subprocess.run(
            screenshot_command,
            check=False,
            capture_output=True,
            timeout=30,
        )
        require(
            screenshot_run.returncode == 0,
            f"Edge {name} screenshot failed: {screenshot_run.stderr.decode(errors='replace')}",
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (
            not screenshot.is_file() or screenshot.stat().st_size <= 10_000
        ):
            time.sleep(0.1)
        require(screenshot.is_file() and screenshot.stat().st_size > 10_000, f"Edge {name} screenshot missing")

        outputs[f"{name}_screenshot"] = str(screenshot)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and session_success_count(server_log) < sessions_before + 2:
        time.sleep(0.1)
    require(
        session_success_count(server_log) >= sessions_before + 2,
        "real browsers did not complete both session bootstrap requests",
    )
    return outputs


def api_smoke(base_url: str) -> tuple[dict[str, bool], list[str]]:
    checks: dict[str, bool] = {}
    origin = base_url

    session_denied = request(base_url, "GET", "/api/session")
    expect(session_denied, 403, "session origin guard")
    checks["origin_guard"] = True

    session = expect(request(base_url, "GET", "/api/session", headers={"Origin": origin}), 200, "session")
    token = session.get("session_token")
    require(isinstance(token, str) and len(token) >= 32, "session token is missing or too short")
    token = cast(str, token)
    mutation = {"Origin": origin, "X-Restream-Session": token}

    source_get = request(base_url, "GET", "/api/source")
    source = expect(source_get, 200, "get source")
    require(source.get("configured") is False, "fresh source state is invalid")
    source_etag = require_etag(source_get, "get source")

    unauthorized = request(
        base_url,
        "PUT",
        "/api/source",
        headers={"Origin": origin, "If-Match": source_etag},
        body={"room_url": "https://live.douyin.com/123456", "preferred_quality": "origin"},
    )
    expect(unauthorized, 403, "mutation session guard")
    checks["session_guard"] = True

    start = request(base_url, "POST", "/api/control/start", headers=mutation, body={"local_test": False})
    expect(start, 409, "start precondition")
    reconnect = request(base_url, "POST", "/api/destinations/douyin/reconnect", headers=mutation, body={})
    expect(reconnect, 409, "reconnect precondition")
    checks["control_preconditions"] = True

    source_put = request(
        base_url,
        "PUT",
        "/api/source",
        headers={**mutation, "If-Match": source_etag},
        body={"room_url": "https://live.douyin.com/123456", "preferred_quality": "origin"},
    )
    source_saved = expect(source_put, 200, "save source")
    require(source_saved.get("room_identity") == "https://live.douyin.com/123456", "source normalization mismatch")
    source_saved_etag = require_etag(source_put, "save source")
    require(source_saved_etag != source_etag, "source revision did not advance")

    conflict = request(
        base_url,
        "PUT",
        "/api/source",
        headers={**mutation, "If-Match": source_etag},
        body={"room_url": "https://live.douyin.com/654321", "preferred_quality": "origin"},
    )
    conflict_body = expect(conflict, 409, "stale source update")
    require(isinstance(conflict_body.get("error"), dict) and conflict_body["error"].get("code") == "write_conflict", "write conflict code mismatch")
    checks["revision_conflict"] = True

    invalid_secret = "validation-secret-must-not-leak-9842"
    invalid = request(
        base_url,
        "PUT",
        "/api/destinations/douyin",
        headers={**mutation, "If-Match": '"0"'},
        body={"base_server": "http://bad.invalid/live", "stream_key": invalid_secret, "enabled": True},
    )
    expect(invalid, 422, "destination validation")
    require(invalid_secret not in invalid.text, "validation response leaked stream key")
    checks["validation_redaction"] = True

    keys = {
        "douyin": "smoke-douyin-key-18f8e7",
        "wechat_channels": "smoke-wechat-key-5df103",
    }
    for kind, secret in keys.items():
        current = request(base_url, "GET", f"/api/destinations/{kind}")
        current_body = expect(current, 200, f"get {kind}")
        require(current_body.get("configured") is False, f"fresh {kind} state is invalid")
        current_etag = require_etag(current, f"get {kind}")
        saved = request(
            base_url,
            "PUT",
            f"/api/destinations/{kind}",
            headers={**mutation, "If-Match": current_etag},
            body={"base_server": "rtmps://smoke.invalid/live", "stream_key": secret, "enabled": True},
        )
        saved_body = expect(saved, 200, f"save {kind}")
        require(saved_body.get("configured") is True, f"{kind} was not configured")
        require(saved_body.get("masked_stream_key") == "********", f"{kind} key was not masked")
        require(secret not in saved.text, f"{kind} response leaked stream key")
        tested = request(base_url, "POST", f"/api/destinations/{kind}/test", headers=mutation, body={}, timeout=8)
        tested_body = expect(tested, 200, f"test {kind}")
        require(tested_body.get("ok") is False and tested_body.get("diagnostic") == "connection_failed", f"{kind} negative diagnostic mismatch")
    checks["destination_crud_and_diagnostics"] = True

    started = expect(
        request(base_url, "POST", "/api/control/start", headers=mutation, body={"local_test": False}),
        200,
        "configured start",
    )
    require(started.get("status") == "started", "configured start status mismatch")
    running = expect(request(base_url, "GET", "/api/status"), 200, "running status")
    require(running.get("desired_running") is True, "controller did not enter desired running state")
    stopped = expect(request(base_url, "POST", "/api/control/stop", headers=mutation, body={}), 200, "stop")
    require(stopped.get("status") == "stopped", "stop status mismatch")
    status = expect(request(base_url, "GET", "/api/status"), 200, "status")
    require(status.get("desired_running") is False and isinstance(status.get("outputs"), list), "status schema mismatch")
    events = expect(request(base_url, "GET", "/api/events?limit=100&cursor=0"), 200, "events")
    require(isinstance(events.get("items"), list), "events schema mismatch")
    bad_query = request(base_url, "GET", "/api/events?unknown=1")
    expect(bad_query, 400, "events query allowlist")
    checks["configured_start_status_stop"] = True
    checks["status_events"] = True
    return checks, [*keys.values(), invalid_secret]


def persistence_smoke(base_url: str) -> dict[str, bool]:
    source = expect(request(base_url, "GET", "/api/source"), 200, "persisted source")
    require(source.get("room_identity") == "https://live.douyin.com/123456", "source did not persist")
    for kind in ("douyin", "wechat_channels"):
        destination = expect(request(base_url, "GET", f"/api/destinations/{kind}"), 200, f"persisted {kind}")
        require(destination.get("configured") is True, f"{kind} did not persist")
        require(destination.get("masked_stream_key") == "********", f"persisted {kind} key is not masked")
    return {"restart_persistence": True}


def scan_for_secrets(root: Path, secrets: list[str]) -> None:
    needles = [item.encode("utf-8") for item in secrets]
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0].startswith("edge-profile-"):
            continue
        data = path.read_bytes()
        for needle in needles:
            require(needle not in data, f"plaintext stream key found in artifact: {path}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def command_output(command: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def release_metadata(executable: Path) -> dict[str, object]:
    repository = Path(__file__).resolve().parents[1]
    ffmpeg = executable.parent / "_internal" / "ffmpeg.exe"
    require(ffmpeg.is_file(), "bundled FFmpeg is missing")
    ffmpeg_version = command_output([str(ffmpeg), "-version"], cwd=executable.parent).splitlines()[0]
    return {
        "git_commit": command_output(["git", "rev-parse", "HEAD"], cwd=repository),
        "git_clean": command_output(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=repository
        )
        == "",
        "executable_sha256": file_sha256(executable),
        "executable_size": executable.stat().st_size,
        "windows": platform.platform(),
        "ffmpeg": ffmpeg_version,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    executable = args.exe.resolve()
    require(executable.is_file(), f"package executable not found: {executable}")
    started_at = datetime.now(UTC).isoformat()
    run_root = (args.artifacts.resolve() / f"packaged-full-smoke-{uuid.uuid4().hex}")
    run_root.mkdir(parents=True)
    data_root = run_root / "app-data"
    checks: dict[str, bool] = {}
    visual: dict[str, str] = {}
    secrets: list[str] = []

    first_port = dynamic_port()
    first = start_package(executable, data_root, first_port, run_root)
    try:
        first_url = f"http://127.0.0.1:{first_port}"
        wait_ready(first, first_url)
        api_checks, secrets = api_smoke(first_url)
        checks.update(api_checks)
        visual = browser_smoke(
            find_edge(),
            first_url,
            run_root,
            run_root / f"package-{first_port}.stdout.txt",
        )
        checks["desktop_and_mobile_render"] = True
        checks["browser_session_bootstrap"] = True
    finally:
        stop_package(first, first_port)
    checks["first_process_cleanup"] = True

    second_port = dynamic_port()
    second = start_package(executable, data_root, second_port, run_root)
    try:
        second_url = f"http://127.0.0.1:{second_port}"
        wait_ready(second, second_url)
        checks.update(persistence_smoke(second_url))
    finally:
        stop_package(second, second_port)
    checks["restart_process_cleanup"] = True

    scan_for_secrets(run_root, secrets)
    checks["plaintext_secret_scan"] = True
    result = {
        "status": "passed",
        "started_at_utc": started_at,
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "executable": str(executable),
        "release": release_metadata(executable),
        "checks": checks,
        "visual_evidence": visual,
        "database": str(data_root / "data" / "restream-studio.sqlite3"),
    }
    result_file = run_root / "result.json"
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**result, "result_file": str(result_file)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
