from __future__ import annotations

import asyncio
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

import psutil  # type: ignore[import-untyped]
import pytest

from restream_studio.domain import DestinationKind, OutputState
from restream_studio.media.process import AsyncProcess
from restream_studio.outputs.supervisor import OutputSupervisor

FIXTURE = Path(__file__).parents[1] / "fixtures" / "child_process.py"


def child(mode: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, str(FIXTURE), mode, *args)


async def eventually(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_async_process_starts_collects_metrics_and_stops_cleanly() -> None:
    process = AsyncProcess(child("healthy"))
    await process.start()
    await eventually(lambda: process.metrics.get("progress") == "continue")

    assert process.pid is not None and psutil.pid_exists(process.pid)
    assert process.metrics == {
        "fps": 29.97,
        "bitrate_kbps": 4123.5,
        "speed": 1.02,
        "out_time_seconds": 62.5,
        "progress": "continue",
    }

    returncode = await asyncio.wait_for(process.stop(timeout=1.0), timeout=2.0)
    assert returncode == 0
    assert process.returncode == 0
    assert process.running is False
    assert not psutil.pid_exists(process.pid)


@pytest.mark.asyncio
async def test_async_process_kills_after_timeout_and_concurrent_stop_is_idempotent() -> None:
    process = AsyncProcess(child("ignore-stop"))
    await process.start()
    await eventually(lambda: process.metrics.get("progress") == "continue")
    pid = process.pid

    results = await asyncio.wait_for(
        asyncio.gather(process.stop(timeout=0.05), process.stop(timeout=0.05)), timeout=2.0
    )

    assert results[0] == results[1] == process.returncode
    assert process.was_killed is True
    assert pid is not None and not psutil.pid_exists(pid)


@pytest.mark.asyncio
async def test_stop_during_subprocess_creation_reaps_the_late_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = asyncio.create_subprocess_exec
    create_entered = asyncio.Event()
    release_create = asyncio.Event()

    async def delayed_create(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        create_entered.set()
        await release_create.wait()
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_create)
    process = AsyncProcess(child("healthy"))
    start_task = asyncio.create_task(process.start())
    await asyncio.wait_for(create_entered.wait(), timeout=1.0)
    stop_task = asyncio.create_task(process.stop(timeout=0.5))
    await asyncio.sleep(0)
    release_create.set()

    await asyncio.wait_for(asyncio.gather(start_task, stop_task), timeout=2.0)
    assert process.pid is not None
    assert not psutil.pid_exists(process.pid)


@pytest.mark.asyncio
async def test_exit_code_tail_bounds_metrics_allowlist_and_redaction() -> None:
    secret = "super-secret-stream-key"
    target = f"rtmp://live.example.com/app/{secret}?token=private-token"
    process = AsyncProcess(
        child("exit", "--code", "23", "--lines", "12", "--line-length", "80", "--secret", target),
        sensitive_values=(target, secret, "private-token"),
        max_tail_entries=3,
        max_line_chars=48,
        max_tail_bytes=120,
    )
    await process.start()

    assert await asyncio.wait_for(process.wait(), timeout=2.0) == 23
    snapshot = process.snapshot()
    rendered = repr(snapshot) + repr(process)
    assert len(snapshot.stderr_tail) <= 3
    assert sum(len(line.encode()) for line in snapshot.stderr_tail) <= 120
    assert all(len(line) <= 48 for line in snapshot.stderr_tail)
    assert target not in rendered and secret not in rendered and "private-token" not in rendered
    assert snapshot.metrics == {}


@pytest.mark.asyncio
async def test_supervisor_transitions_live_then_reconnects_with_capped_schedule() -> None:
    delays: list[float] = []
    release = asyncio.Event()

    async def retry_wait(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= 6:
            await release.wait()

    supervisor = OutputSupervisor(
        DestinationKind.DOUYIN,
        child("exit", "--code", "9"),
        retry_wait=retry_wait,
    )
    task = asyncio.create_task(supervisor.run())
    await eventually(lambda: len(delays) >= 6)

    assert supervisor.state is OutputState.RECONNECTING
    assert delays == [2.0, 5.0, 10.0, 20.0, 30.0, 30.0]
    assert supervisor.reconnect_count == 6
    await supervisor.stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert supervisor.snapshot().state is OutputState.STOPPED


@pytest.mark.asyncio
async def test_supervisor_start_reaches_live_and_stop_reaps_process() -> None:
    supervisor = OutputSupervisor(DestinationKind.DOUYIN, child("healthy"))

    await asyncio.wait_for(supervisor.start(), timeout=2.0)
    pid = supervisor.pid
    assert supervisor.state is OutputState.LIVE
    assert pid is not None and psutil.pid_exists(pid)

    await asyncio.wait_for(supervisor.stop(), timeout=2.0)
    assert supervisor.snapshot().state is OutputState.STOPPED
    assert not psutil.pid_exists(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["authentication failed", "HTTP 403", "invalid stream key", "publish denied"],
)
async def test_authentication_rejection_is_terminal(message: str) -> None:
    secret = "terminal-secret"
    target = f"rtmp://live.example.com/app/{secret}"
    supervisor = OutputSupervisor(
        DestinationKind.WECHAT,
        child("auth-fail", "--message", message, "--secret", target, "--lines", "75"),
        sensitive_values=(target, secret),
    )
    assert target not in repr(supervisor) and secret not in repr(supervisor)
    await asyncio.wait_for(supervisor.run(), timeout=2.0)

    assert supervisor.state is OutputState.AUTH_FAILED
    assert supervisor.reconnect_count == 0
    rendered = repr(supervisor) + repr(supervisor.snapshot())
    assert target not in rendered and secret not in rendered
    assert supervisor.process is not None
    assert all(message.casefold() not in line.casefold() for line in supervisor.process.stderr_tail)


@pytest.mark.asyncio
async def test_cancelling_during_backoff_reaps_internal_wait_tasks() -> None:
    waiting = asyncio.Event()

    async def blocked_retry(_delay: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    supervisor = OutputSupervisor(
        DestinationKind.DOUYIN,
        child("exit", "--code", "9"),
        retry_wait=blocked_retry,
    )
    task = asyncio.create_task(supervisor.run())
    await asyncio.wait_for(waiting.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    await asyncio.sleep(0)

    pending_names = {item.get_name() for item in asyncio.all_tasks() if item is not asyncio.current_task()}
    assert "output-retry-delay" not in pending_names
    assert "output-retry-stop" not in pending_names


@pytest.mark.asyncio
async def test_timeout_kill_terminates_descendant_process_tree() -> None:
    process = AsyncProcess(child("spawn-descendant-ignore-stop"))
    await process.start()
    await eventually(lambda: any(line.startswith("descendant_pid=") for line in process.stderr_tail))
    descendant_line = next(line for line in process.stderr_tail if line.startswith("descendant_pid="))
    descendant_pid = int(descendant_line.partition("=")[2])
    assert psutil.pid_exists(descendant_pid)

    await asyncio.wait_for(process.stop(timeout=0.05), timeout=3.0)
    await eventually(lambda: not psutil.pid_exists(descendant_pid))

    assert process.was_killed is True
    assert not psutil.pid_exists(descendant_pid)


@pytest.mark.asyncio
async def test_two_supervisors_isolate_processes_and_stop_groups() -> None:
    first = OutputSupervisor(DestinationKind.DOUYIN, child("healthy"))
    second = OutputSupervisor(DestinationKind.WECHAT, child("healthy"))
    first_task = asyncio.create_task(first.run())
    second_task = asyncio.create_task(second.run())
    await eventually(lambda: first.state is OutputState.LIVE and second.state is OutputState.LIVE)
    first_pid = first.pid
    second_pid = second.pid

    assert first.process is not None
    first.process.kill()
    await eventually(lambda: first.state is OutputState.RECONNECTING)
    assert second.state is OutputState.LIVE
    assert second_pid is not None and psutil.pid_exists(second_pid)

    await first.stop()
    assert second.state is OutputState.LIVE
    assert second_pid is not None and psutil.pid_exists(second_pid)
    await second.stop()
    await asyncio.gather(first_task, second_task)
    assert first_pid is not None and not psutil.pid_exists(first_pid)
    assert not psutil.pid_exists(second_pid)


@pytest.mark.asyncio
async def test_cancelling_supervisor_reaps_child_and_propagates_cancelled_error() -> None:
    supervisor = OutputSupervisor(DestinationKind.LOCAL_TEST, child("healthy"))
    task = asyncio.create_task(supervisor.run())
    await eventually(lambda: supervisor.state is OutputState.LIVE)
    pid = supervisor.pid

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    assert pid is not None and not psutil.pid_exists(pid)
    assert supervisor.state is OutputState.STOPPED


@pytest.mark.asyncio
async def test_malformed_and_unbounded_progress_fields_are_discarded() -> None:
    process = AsyncProcess(child("malformed-metrics"))
    await process.start()
    await asyncio.wait_for(process.wait(), timeout=2.0)

    assert process.metrics == {}
    assert all(len(line) <= 512 for line in process.stderr_tail)


@pytest.mark.asyncio
async def test_start_error_traceback_has_no_sensitive_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "rtmp://live.example.com/app/secret-in-start-error"

    async def fail_create(*_args: str, **_kwargs: object) -> asyncio.subprocess.Process:
        raise OSError(f"cannot spawn {secret}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_create)
    process = AsyncProcess(("ffmpeg", secret), sensitive_values=(secret,))

    with pytest.raises(RuntimeError) as raised:
        try:
            await process.start()
        except RuntimeError:
            formatted = traceback.format_exc()
            raise

    assert raised.value.__cause__ is None
    assert secret not in formatted
    assert secret not in str(raised.value) and secret not in repr(raised.value)
