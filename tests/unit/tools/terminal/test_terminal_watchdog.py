from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import pytest

from modex_agent.tools.terminal.backends.base import TerminalBackend
from modex_agent.tools.terminal.managers import BaseTerminalManager
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.results import TerminalRead
from modex_agent.tools.terminal.types import (
    Platform,
    ProcessStatus,
    ShellFamily,
    ShellInfo,
    TerminalVisibility,
)
from modex_agent.tools.terminal.watchdog import TerminalWatchdog


class _FakeBackend(TerminalBackend):
    def __init__(self) -> None:
        super().__init__()
        self._alive = False

    @property
    def platform(self) -> Platform:
        return Platform.LINUX

    @property
    def visibility(self) -> TerminalVisibility:
        return TerminalVisibility.HIDDEN

    async def start(
        self,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        del shell, cwd, env
        self._alive = True

    def _shell_family(self) -> ShellFamily:
        return ShellFamily.BASH

    async def interrupt(self) -> None:
        return

    async def is_alive(self) -> bool:
        return self._alive

    async def terminate(self) -> None:
        self._alive = False

    async def kill(self) -> None:
        self._alive = False

    def stdin_writable(self) -> bool:
        return True

    async def read_pending(self, timeout: float = 5.0, max_size: int = 65536) -> TerminalRead:
        del timeout, max_size
        return TerminalRead()


class _FlakyTerminateBackend(_FakeBackend):
    """Fake backend whose ``terminate()`` raises while ``fail_terminate`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_terminate = False

    async def terminate(self) -> None:
        if self.fail_terminate:
            raise RuntimeError("terminate failed")
        self._alive = False


class _WatchdogManager(BaseTerminalManager):
    def __init__(self, *, fail_name: str | None = None) -> None:
        super().__init__(
            shell_info=ShellInfo(
                family=ShellFamily.BASH,
                path="/bin/bash",
                platform=Platform.LINUX,
            ),
            visibility=TerminalVisibility.HIDDEN,
            backend_factory=_FakeBackend,
        )
        self._fail_name = fail_name
        self.closed = asyncio.Event()

    async def close(self, name: str) -> bool:
        if name == self._fail_name:
            raise RuntimeError("close failed")
        closed = await super().close(name)
        if closed:
            self.closed.set()
        return closed


async def test_expired_session_is_timed_out_and_default_tab_reselected() -> None:
    manager = _WatchdogManager()
    await manager.get_or_create("other")
    await manager.get_or_create("expired")
    registry = ProcessRegistry()
    session = registry.create(command="sleep 999", terminal="expired", cwd=None, pid=None)
    session.deadline_at = time.monotonic() - 1
    watchdog = TerminalWatchdog(manager, registry, interval_s=0.01)

    watchdog.start()
    await asyncio.wait_for(manager.closed.wait(), timeout=0.2)
    await watchdog.stop()

    finished = registry.get_finished(session.id)
    assert finished is not None
    assert finished.status is ProcessStatus.TIMED_OUT
    assert finished.timed_out is True
    assert manager.get("expired") is None
    assert (await manager.get_default()).name == "other"


async def test_non_expired_session_remains_running_after_several_ticks() -> None:
    manager = _WatchdogManager()
    await manager.get_or_create("active")
    registry = ProcessRegistry()
    session = registry.create(command="sleep 1", terminal="active", cwd=None, pid=None)
    session.deadline_at = time.monotonic() + 60
    watchdog = TerminalWatchdog(manager, registry, interval_s=0.01)

    watchdog.start()
    await asyncio.sleep(0.04)
    await watchdog.stop()

    assert registry.get_running(session.id) is session
    assert manager.get("active") is not None


async def test_stop_cancels_scanner_task_and_is_idempotent() -> None:
    watchdog = TerminalWatchdog(_WatchdogManager(), ProcessRegistry(), interval_s=0.01)

    await watchdog.stop()
    watchdog.start()
    task = watchdog._task
    watchdog.start()
    await watchdog.stop()
    await asyncio.sleep(0)
    await watchdog.stop()

    assert task is not None
    assert task.done()


async def test_close_failure_isolated_from_other_expired_tabs() -> None:
    manager = _WatchdogManager(fail_name="broken")
    await manager.get_or_create("healthy")
    await manager.get_or_create("broken")
    registry = ProcessRegistry()
    healthy = registry.create(command="sleep 2", terminal="healthy", cwd=None, pid=None)
    broken = registry.create(command="sleep 1", terminal="broken", cwd=None, pid=None)
    healthy.deadline_at = time.monotonic() - 1
    broken.deadline_at = time.monotonic() - 1
    watchdog = TerminalWatchdog(manager, registry, interval_s=0.01)

    watchdog.start()
    await asyncio.wait_for(manager.closed.wait(), timeout=0.2)

    assert manager.get("broken") is not None
    assert manager.get("healthy") is None
    assert registry.get_finished(healthy.id) is healthy
    assert registry.get_running(broken.id) is broken
    assert registry.get_finished(broken.id) is None

    manager.closed.clear()
    manager._fail_name = None
    await asyncio.wait_for(manager.closed.wait(), timeout=0.2)
    await watchdog.stop()

    assert watchdog._task is None
    assert manager.get("broken") is None
    assert registry.get_finished(broken.id) is broken
    assert broken.status is ProcessStatus.TIMED_OUT


async def test_close_failure_retried_next_tick_through_real_manager() -> None:
    """Production close-failure semantics: the REAL BaseTerminalManager
    pops the session before terminating the backend, so a backend
    ``terminate()`` that raises escapes ``close()`` AFTER the pop.

    Tick 1: close raises → the session stays RUNNING, no finished entry
    (the tab is already gone from the manager). Tick 2: close returns
    False (tab already popped) — SUCCESS for the converged contract —
    and the session is marked TIMED_OUT."""
    created: list[_FlakyTerminateBackend] = []

    def _factory() -> _FlakyTerminateBackend:
        backend = _FlakyTerminateBackend()
        created.append(backend)
        return backend

    manager = BaseTerminalManager(
        shell_info=ShellInfo(
            family=ShellFamily.BASH,
            path="/bin/bash",
            platform=Platform.LINUX,
        ),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=_factory,
    )
    await manager.get_or_create("expired")
    broken_backend = created[-1]
    broken_backend.fail_terminate = True
    registry = ProcessRegistry()
    session = registry.create(command="sleep 999", terminal="expired", cwd=None, pid=None)
    session.deadline_at = time.monotonic() - 1
    watchdog = TerminalWatchdog(manager, registry, interval_s=0.25)

    watchdog.start()
    # Tick 1: the tab is popped, then terminate() raises out of close().
    await _wait_until(lambda: manager.get("expired") is None)
    assert registry.get_running(session.id) is session
    assert registry.get_finished(session.id) is None

    broken_backend.fail_terminate = False
    # Tick 2: close finds no session (already popped) → False → mark TIMED_OUT.
    await _wait_until(lambda: registry.get_finished(session.id) is not None)
    await watchdog.stop()

    finished = registry.get_finished(session.id)
    assert finished is session
    assert finished.status is ProcessStatus.TIMED_OUT
    assert finished.timed_out is True


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    pytest.fail(f"condition not met within {timeout}s")


def test_start_requires_running_event_loop() -> None:
    watchdog = TerminalWatchdog(_WatchdogManager(), ProcessRegistry())

    with pytest.raises(RuntimeError, match="running event loop"):
        watchdog.start()
