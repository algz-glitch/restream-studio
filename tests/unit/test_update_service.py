from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from restream_studio.update.contracts import (
    DownloadResult,
    UpdateCheckResult,
    UpdateErrorCode,
    UpdateManifest,
    Version,
)
from restream_studio.update.helper import (
    INSTALLER_ARGUMENTS,
    HelperArguments,
    parse_arguments,
    run_helper,
    wait_for_process,
)
from restream_studio.update.service import (
    UpdateOperationError,
    UpdateService,
    UpdateSnapshot,
    UpdateStatus,
)


def manifest(content: bytes = b"verified-installer") -> UpdateManifest:
    return UpdateManifest(
        schema_version=1,
        version=Version(0, 2, 0),
        installer_url=(
            "https://github.com/algz-glitch/restream-studio/releases/download/"
            "v0.2.0/RestreamStudio-Setup-0.2.0.exe"
        ),
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        published_at=datetime(2026, 9, 13, tzinfo=UTC),
        release_url=(
            "https://github.com/algz-glitch/restream-studio/releases/tag/v0.2.0"
        ),
    )


class FakeClient:
    def __init__(self, result: UpdateCheckResult, content: bytes = b"verified-installer") -> None:
        self.result = result
        self.content = content
        self.check_calls = 0
        self.download_calls = 0

    def check_for_update(self, current_version: str) -> UpdateCheckResult:
        assert current_version == "0.1.0"
        self.check_calls += 1
        return self.result

    def download_installer_result(
        self, selected: UpdateManifest, destination: Path
    ) -> DownloadResult:
        self.download_calls += 1
        destination.write_bytes(self.content)
        return DownloadResult.succeeded(selected, destination)


@pytest.mark.asyncio
async def test_check_transitions_are_consistent_and_persist_only_safe_metadata(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    client = FakeClient(UpdateCheckResult.available(manifest()))
    service = UpdateService(tmp_path / "data" / "update", client=client, clock=lambda: now)

    assert service.snapshot().status is UpdateStatus.IDLE
    checked = await service.check()

    assert checked.status is UpdateStatus.AVAILABLE
    assert checked.available_version == "0.2.0"
    assert checked.release_url is not None and checked.release_url.endswith("/v0.2.0")
    assert checked.error_code is None and checked.error_message is None
    persisted = json.loads((tmp_path / "data" / "update" / "state.json").read_text("utf-8"))
    assert persisted == {
        "available_version": "0.2.0",
        "current_version": "0.1.0",
        "error_code": None,
        "last_checked_at": "2026-09-13T12:00:00+00:00",
        "status": "available",
    }
    assert "installer_url" not in json.dumps(persisted)


@pytest.mark.asyncio
async def test_current_and_failed_checks_never_retain_available_fields(tmp_path: Path) -> None:
    client = FakeClient(UpdateCheckResult.current())
    service = UpdateService(tmp_path / "update", client=client)
    current = await service.check()
    assert current.status is UpdateStatus.CURRENT
    assert current.available_version is None and current.release_url is None

    client.result = UpdateCheckResult.failed(UpdateErrorCode.NETWORK, "private endpoint detail")
    failed = await service.check()
    assert failed.status is UpdateStatus.FAILED
    assert failed.available_version is None and failed.release_url is None
    assert failed.error_code == "network"
    assert failed.error_message == "Update check failed"
    assert "private endpoint detail" not in repr(failed)


@pytest.mark.asyncio
async def test_unexpected_client_failure_becomes_fixed_failed_state(tmp_path: Path) -> None:
    class BrokenClient(FakeClient):
        def check_for_update(self, current_version: str) -> UpdateCheckResult:
            del current_version
            raise RuntimeError("secret transport implementation detail")

    service = UpdateService(
        tmp_path / "update", client=BrokenClient(UpdateCheckResult.current())
    )
    failed = await service.check()
    assert failed.status is UpdateStatus.FAILED
    assert failed.error_code == "check_failed"
    assert failed.error_message == "Update check failed"
    assert "secret transport" not in repr(failed)


@pytest.mark.asyncio
async def test_automatic_check_is_throttled_for_24_hours_and_manual_check_is_not(
    tmp_path: Path,
) -> None:
    moments = [datetime(2026, 9, 13, 12, tzinfo=UTC)]
    client = FakeClient(UpdateCheckResult.current())
    service = UpdateService(tmp_path / "update", client=client, clock=lambda: moments[0])
    await service.check()

    moments[0] += timedelta(hours=23, minutes=59)
    await service.automatic_check()
    assert client.check_calls == 1

    await service.check()
    assert client.check_calls == 2
    moments[0] += timedelta(hours=24)
    await service.automatic_check()
    assert client.check_calls == 3

    moments[0] -= timedelta(hours=25)
    await service.automatic_check()
    assert client.check_calls == 4


@pytest.mark.asyncio
async def test_restart_discards_manifest_but_preserves_throttle_until_manual_check(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    first_client = FakeClient(UpdateCheckResult.available(manifest()))
    first = UpdateService(tmp_path / "update", client=first_client, clock=lambda: now)
    await first.check()

    restarted_client = FakeClient(UpdateCheckResult.available(manifest()))
    restarted = UpdateService(
        tmp_path / "update",
        client=restarted_client,
        clock=lambda: now + timedelta(minutes=1),
    )
    assert restarted.snapshot().status is UpdateStatus.IDLE
    assert restarted.snapshot().last_checked_at == now

    throttled = await restarted.automatic_check()
    assert throttled.status is UpdateStatus.IDLE
    assert restarted_client.check_calls == 0

    checked = await restarted.check()
    assert checked.status is UpdateStatus.AVAILABLE
    assert restarted_client.check_calls == 1
    assert (await restarted.download()).status is UpdateStatus.READY


@pytest.mark.asyncio
async def test_future_persisted_timestamp_is_discarded_and_does_not_throttle(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 13, 12, tzinfo=UTC)
    first = UpdateService(
        tmp_path / "update",
        client=FakeClient(UpdateCheckResult.current()),
        clock=lambda: now,
    )
    await first.check()
    client = FakeClient(UpdateCheckResult.current())
    restarted = UpdateService(
        tmp_path / "update",
        client=client,
        clock=lambda: now - timedelta(minutes=1),
    )
    assert restarted.snapshot() == UpdateSnapshot(UpdateStatus.IDLE, "0.1.0")
    assert not (tmp_path / "update" / "state.json").exists()
    await restarted.automatic_check()
    assert client.check_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        UpdateCheckResult.current(),
        UpdateCheckResult.available(manifest()),
        UpdateCheckResult.failed(UpdateErrorCode.NETWORK, "private detail"),
    ],
)
async def test_check_persistence_failure_keeps_previous_state_for_every_result(
    tmp_path: Path, result: UpdateCheckResult
) -> None:
    writes = 0

    def writer(_candidate: UpdateSnapshot) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise PermissionError("private denied path")

    service = UpdateService(
        tmp_path / "update",
        client=FakeClient(result),
        state_writer=writer,
    )
    with pytest.raises(UpdateOperationError) as error:
        await service.check()
    assert error.value.code == "filesystem"
    assert error.value.message == "Update state could not be saved"
    assert service.snapshot().status is UpdateStatus.CHECKING


@pytest.mark.asyncio
async def test_denied_initial_state_write_does_not_start_check(
    tmp_path: Path,
) -> None:
    client = FakeClient(UpdateCheckResult.current())
    service = UpdateService(
        tmp_path / "update",
        client=client,
        state_writer=lambda _candidate: (_ for _ in ()).throw(
            PermissionError("private read-only path")
        ),
    )
    with pytest.raises(UpdateOperationError) as error:
        await service.check()
    assert error.value.code == "filesystem"
    assert service.snapshot().status is UpdateStatus.IDLE
    assert client.check_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("download_succeeds", [True, False])
async def test_download_persistence_failure_keeps_previous_state_for_all_results(
    tmp_path: Path, download_succeeds: bool
) -> None:
    selected = manifest()

    class DownloadClient(FakeClient):
        def download_installer_result(
            self, selected_manifest: UpdateManifest, destination: Path
        ) -> DownloadResult:
            if download_succeeds:
                return super().download_installer_result(selected_manifest, destination)
            return DownloadResult.failed(
                selected_manifest, UpdateErrorCode.NETWORK, "private download detail"
            )

    service = UpdateService(
        tmp_path / "update",
        client=DownloadClient(UpdateCheckResult.available(selected)),
    )
    await service.check()
    writes = 0

    def writer(_candidate: UpdateSnapshot) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise PermissionError("private denied path")

    service._state_writer = writer
    with pytest.raises(UpdateOperationError) as error:
        await service.download()
    assert error.value.code == "filesystem"
    assert service.snapshot().status is UpdateStatus.DOWNLOADING


@pytest.mark.asyncio
async def test_denied_download_transition_keeps_available_and_skips_network(
    tmp_path: Path,
) -> None:
    client = FakeClient(UpdateCheckResult.available(manifest()))
    service = UpdateService(tmp_path / "update", client=client)
    await service.check()
    previous = service.snapshot()
    service._state_writer = lambda _candidate: (_ for _ in ()).throw(
        PermissionError("private read-only path")
    )
    with pytest.raises(UpdateOperationError) as error:
        await service.download()
    assert error.value.code == "filesystem"
    assert service.snapshot() == previous
    assert client.download_calls == 0


@pytest.mark.asyncio
async def test_check_is_single_flight_and_sync_client_does_not_block_event_loop(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    class BlockingClient(FakeClient):
        def check_for_update(self, current_version: str) -> UpdateCheckResult:
            loop.call_soon_threadsafe(entered.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=2)
            return super().check_for_update(current_version)

    service = UpdateService(
        tmp_path / "update", client=BlockingClient(UpdateCheckResult.current())
    )
    first = asyncio.create_task(service.check())
    await asyncio.wait_for(entered.wait(), timeout=1)
    await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
    with pytest.raises(UpdateOperationError) as error:
        await service.check()
    assert error.value.code == "check_in_progress"
    release.set()
    await first


@pytest.mark.asyncio
async def test_download_becomes_ready_and_install_revalidates_before_launch(
    tmp_path: Path,
) -> None:
    launched: list[list[str]] = []
    shutdowns = 0

    async def shutdown() -> None:
        nonlocal shutdowns
        shutdowns += 1

    client = FakeClient(UpdateCheckResult.available(manifest()))
    executable = (tmp_path / "RestreamStudio.exe").resolve()
    executable.write_bytes(b"app")
    service = UpdateService(
        tmp_path / "update",
        client=client,
        executable=executable,
        helper_command=("python", "-m", "restream_studio.update.helper"),
        launcher=lambda command: launched.append(list(command)),
        shutdown_callback=shutdown,
        process_create_time=lambda pid: 1234.5 if pid == 4321 else 0.0,
    )
    await service.check()
    ready = await service.download()
    assert ready.status is UpdateStatus.READY
    assert ready.available_version == "0.2.0"
    assert not hasattr(ready, "installer_path")

    await service.install(current_pid=4321)
    assert launched == [[
        "python",
        "-m",
        "restream_studio.update.helper",
        "--pid",
        "4321",
        "--installer",
        str((tmp_path / "update" / "RestreamStudio-Setup-0.2.0.exe").resolve()),
        "--executable",
        str(executable),
        "--expected-sha256",
        manifest().sha256,
        "--expected-size",
        str(manifest().size),
        "--process-created-at",
        "1234.5",
    ]]
    await asyncio.sleep(0)
    assert shutdowns == 1

    tampered_service = UpdateService(
        tmp_path / "tampered-update",
        client=client,
        executable=executable,
        launcher=lambda command: launched.append(list(command)),
        shutdown_callback=shutdown,
        process_create_time=lambda _pid: 1234.5,
    )
    await tampered_service.check()
    await tampered_service.download()
    installer = tmp_path / "tampered-update" / "RestreamStudio-Setup-0.2.0.exe"
    installer.write_bytes(b"tampered")
    with pytest.raises(UpdateOperationError) as error:
        await tampered_service.install(current_pid=4321)
    assert error.value.code == "installer_verification_failed"
    assert len(launched) == 1


@pytest.mark.asyncio
async def test_background_check_does_not_block_startup_and_shutdown_cancels_it(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    service = UpdateService(tmp_path / "update", client=FakeClient(UpdateCheckResult.current()))

    async def blocked() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service.automatic_check = blocked  # type: ignore[assignment]
    service.start_background()
    await asyncio.wait_for(entered.wait(), timeout=1)
    await service.shutdown()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_background_expected_error_is_consumed_without_loop_warning(tmp_path: Path) -> None:
    contexts: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    service = UpdateService(tmp_path / "update", client=FakeClient(UpdateCheckResult.current()))

    async def fail_expected() -> UpdateSnapshot:
        raise UpdateOperationError("filesystem", "Update state could not be saved")

    service.automatic_check = fail_expected  # type: ignore[method-assign]
    try:
        service.start_background()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await service.shutdown()
        assert contexts == []
    finally:
        loop.set_exception_handler(previous)


@pytest.mark.asyncio
async def test_sync_shutdown_callback_is_scheduled_after_install_returns(tmp_path: Path) -> None:
    callbacks: list[str] = []
    client = FakeClient(UpdateCheckResult.available(manifest()))
    executable = (tmp_path / "RestreamStudio.exe").resolve()
    executable.write_bytes(b"app")
    service = UpdateService(
        tmp_path / "update",
        client=client,
        executable=executable,
        launcher=lambda _command: object(),
        shutdown_callback=lambda: callbacks.append("shutdown"),
        process_create_time=lambda _pid: 123.0,
    )
    await service.check()
    await service.download()
    await service.install(current_pid=123)
    assert callbacks == []
    await asyncio.sleep(0)
    assert callbacks == ["shutdown"]

    for operation in (service.check, service.download):
        with pytest.raises(UpdateOperationError) as error:
            await operation()
        assert error.value.code == "update_installing"
    with pytest.raises(UpdateOperationError) as error:
        await service.install(current_pid=123)
    assert error.value.code == "update_installing"
    assert service.snapshot().status is UpdateStatus.READY


def test_helper_rejects_relative_missing_or_mismatched_paths(tmp_path: Path) -> None:
    installer = (tmp_path / "wrong.exe").resolve()
    installer.write_bytes(b"x")
    executable = (tmp_path / "RestreamStudio.exe").resolve()
    digest = hashlib.sha256(b"x").hexdigest()

    def arguments(pid: str, selected: str, app: str) -> list[str]:
        return [
            "--pid", pid, "--installer", selected, "--executable", app,
            "--expected-sha256", digest, "--expected-size", "1",
            "--process-created-at", "1.0",
        ]

    with pytest.raises(ValueError, match="absolute"):
        parse_arguments(arguments("4", "setup.exe", str(executable)))
    with pytest.raises(ValueError, match="name"):
        parse_arguments(arguments("4", str(installer), str(executable)))
    with pytest.raises(ValueError, match="PID"):
        parse_arguments(arguments("0", str(installer), str(executable)))
    with pytest.raises(ValueError, match="invalid"):
        parse_arguments([
            "--pid", "4", "--pid", "5", "--installer", str(installer),
            "--executable", str(executable),
        ])
    valid_installer = (tmp_path / "RestreamStudio-Setup-0.2.0.exe").resolve()
    valid_installer.write_bytes(b"installer")
    parsed = parse_arguments(
        arguments("4", str(valid_installer), str(executable))
    )
    assert (parsed.expected_sha256, parsed.expected_size, parsed.process_created_at) == (
        digest,
        1,
        1.0,
    )
    for option, invalid in (
        ("--expected-sha256", "A" * 64),
        ("--expected-size", "-1"),
        ("--process-created-at", "nan"),
    ):
        invalid_arguments = arguments("4", str(valid_installer), str(executable))
        invalid_arguments[invalid_arguments.index(option) + 1] = invalid
        with pytest.raises(ValueError, match="invalid"):
            parse_arguments(invalid_arguments)
    with pytest.raises(ValueError, match="RestreamStudio.exe"):
        HelperArguments(
            4, valid_installer, (tmp_path / "Other.exe").resolve(),
            hashlib.sha256(b"installer").hexdigest(), 9, 1.0,
        )
    assert HelperArguments(
        4, valid_installer, (tmp_path / "RESTREAMSTUDIO.EXE").resolve(),
        hashlib.sha256(b"installer").hexdigest(), 9, 1.0,
    ).executable.name == "RESTREAMSTUDIO.EXE"


def test_helper_waits_runs_inno_and_only_then_starts_application(tmp_path: Path) -> None:
    installer = (tmp_path / "RestreamStudio-Setup-0.2.0.exe").resolve()
    installer.write_bytes(b"installer")
    executable = (tmp_path / "RestreamStudio.exe").resolve()
    order: list[object] = []

    def wait(pid: int, created_at: float, timeout: float) -> bool:
        order.append(("wait", pid, created_at, timeout))
        return True

    def run(command: Sequence[str]) -> int:
        order.append(list(command))
        executable.write_bytes(b"installed app")
        return 0

    def launch(command: Sequence[str]) -> object:
        order.append(list(command))
        return object()

    result = run_helper(
        HelperArguments(
            123, installer, executable, hashlib.sha256(b"installer").hexdigest(), 9, 4.5
        ),
        wait_for_pid=wait,
        run_installer=run,
        launch=launch,
    )
    assert result == 0
    assert order == [
        ("wait", 123, 4.5, 120.0),
        [str(installer), *INSTALLER_ARGUMENTS],
        [str(executable)],
    ]


def test_helper_does_not_install_if_pid_wait_times_out(tmp_path: Path) -> None:
    installer = (tmp_path / "RestreamStudio-Setup-0.2.0.exe").resolve()
    installer.write_bytes(b"installer")
    executable = (tmp_path / "RestreamStudio.exe").resolve()
    launches: list[Sequence[str]] = []
    def record_install(command: Sequence[str]) -> int:
        launches.append(command)
        return 0

    result = run_helper(
        HelperArguments(
            123, installer, executable, hashlib.sha256(b"installer").hexdigest(), 9, 4.5
        ),
        wait_for_pid=lambda _pid, _created, _timeout: False,
        run_installer=record_install,
        launch=lambda command: launches.append(command),
    )
    assert result == 2
    assert launches == []


def test_helper_revalidates_installer_after_wait_before_execution(tmp_path: Path) -> None:
    original = b"verified-installer"
    installer = (tmp_path / "RestreamStudio-Setup-0.2.0.exe").resolve()
    installer.write_bytes(original)
    executable = (tmp_path / "RestreamStudio.exe").resolve()
    executions: list[Sequence[str]] = []

    def replace_during_wait(_pid: int, _created: float, _timeout: float) -> bool:
        installer.write_bytes(b"replaced-installer")
        return True

    def record_install(command: Sequence[str]) -> int:
        executions.append(command)
        return 0

    result = run_helper(
        HelperArguments(
            123,
            installer,
            executable,
            hashlib.sha256(original).hexdigest(),
            len(original),
            4.5,
        ),
        wait_for_pid=replace_during_wait,
        run_installer=record_install,
        launch=lambda command: executions.append(command),
    )
    assert result == 5
    assert executions == []


def test_helper_treats_reused_pid_as_original_process_exited() -> None:
    class ReusedProcess:
        def create_time(self) -> float:
            return 99.0

        def wait(self, timeout: float) -> None:
            raise AssertionError(f"reused PID must not be waited: {timeout}")

    assert wait_for_process(
        123,
        4.5,
        120.0,
        process_factory=lambda _pid: ReusedProcess(),
    )
