"""Project binding admission is independent of expensive pool assembly."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from bot.acp.runtime import AcpEntryConfig, AcpRuntime


class _ControlledRuntime(AcpRuntime):
    def __init__(self, config_dir: Path) -> None:
        super().__init__(config_dir, AcpEntryConfig())
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.boot_roots: list[Path] = []
        self.fail_boot = False

    async def _boot(self, project_root: Path) -> None:
        self.boot_roots.append(project_root)
        self.started.set()
        await self.release.wait()
        if self.fail_boot:
            raise RuntimeError("configured pool unavailable")


async def test_constructing_runtime_does_not_bind_or_boot(tmp_path: Path) -> None:
    runtime = _ControlledRuntime(tmp_path)
    assert runtime.project_root is None
    assert runtime.boot_roots == []
    await runtime.close()
    assert runtime.boot_roots == []


async def test_invalid_project_does_not_claim_process(tmp_path: Path) -> None:
    runtime = _ControlledRuntime(tmp_path)
    with pytest.raises(ValueError, match="directory"):
        await runtime.bind_project(tmp_path / "missing")
    assert runtime.project_root is None
    runtime.release.set()
    await runtime.bind_project(tmp_path)
    assert runtime.boot_roots == [tmp_path.resolve()]
    await runtime.close()


async def test_relative_project_is_not_inferred_from_process_cwd(tmp_path: Path) -> None:
    runtime = _ControlledRuntime(tmp_path)
    runtime.release.set()
    with pytest.raises(ValueError, match="absolute"):
        await runtime.bind_project(Path("."))
    assert runtime.project_root is None
    await runtime.close()


async def test_concurrent_opens_share_boot_and_reject_another_project(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    runtime = _ControlledRuntime(tmp_path)
    first = asyncio.create_task(runtime.bind_project(tmp_path))
    await runtime.started.wait()
    second = asyncio.create_task(runtime.bind_project(tmp_path / "."))
    with pytest.raises(ValueError, match="different project"):
        await runtime.bind_project(other)
    runtime.release.set()
    await asyncio.gather(first, second)
    assert runtime.boot_roots == [tmp_path.resolve()]
    with pytest.raises(ValueError, match="different project"):
        await runtime.bind_project(other)
    await runtime.close()


async def test_cancelling_one_open_does_not_cancel_shared_boot(tmp_path: Path) -> None:
    runtime = _ControlledRuntime(tmp_path)
    first = asyncio.create_task(runtime.bind_project(tmp_path))
    await runtime.started.wait()
    second = asyncio.create_task(runtime.bind_project(tmp_path))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    runtime.release.set()
    await second
    assert runtime.boot_roots == [tmp_path.resolve()]
    await runtime.close()


async def test_close_during_boot_drains_boot_and_prevents_reopening(tmp_path: Path) -> None:
    runtime = _ControlledRuntime(tmp_path)
    opened = asyncio.create_task(runtime.bind_project(tmp_path))
    await runtime.started.wait()
    await runtime.close()
    with pytest.raises(asyncio.CancelledError):
        await opened
    with pytest.raises(RuntimeError, match="closed"):
        await runtime.bind_project(tmp_path)
    await runtime.close()


async def test_failed_boot_is_not_retried_and_close_can_finish(tmp_path: Path) -> None:
    runtime = _ControlledRuntime(tmp_path)
    runtime.fail_boot = True
    runtime.release.set()
    with pytest.raises(RuntimeError, match="pool unavailable"):
        await runtime.bind_project(tmp_path)
    # The failed boot drains the runtime through the single close owner, so
    # rebinding is rejected by the closed gate rather than retrying boot.
    with pytest.raises(RuntimeError, match="closed"):
        await runtime.bind_project(tmp_path)
    assert runtime.boot_roots == [tmp_path.resolve()]
    await runtime.close()
    await runtime.close()


async def test_concurrent_waiters_on_failed_boot_all_see_error_and_single_close(tmp_path: Path) -> None:
    runtime = _ControlledRuntime(tmp_path)
    runtime.fail_boot = True
    first = asyncio.create_task(runtime.bind_project(tmp_path))
    await runtime.started.wait()
    second = asyncio.create_task(runtime.bind_project(tmp_path))
    runtime.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    # Both waiters see the boot error (the failure path re-raises after
    # close); the shared boot task ran exactly once.
    assert all(isinstance(value, RuntimeError) for value in results)
    assert runtime.boot_roots == [tmp_path.resolve()]
    # The runtime is closed after the failure — subsequent binds are gated.
    with pytest.raises(RuntimeError, match="closed"):
        await runtime.bind_project(tmp_path)
    await runtime.close()
