from __future__ import annotations

import time

import pytest

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import BaseTerminalManager
from framework.tools.terminal.poll_loop import PollOutcome, poll_until_settled
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo, TerminalVisibility


class DeadBackend:
    """Backend that is immediately dead."""

    platform = Platform.WINDOWS  # type: ignore[assignment]
    visibility = TerminalVisibility.HIDDEN  # type: ignore[assignment]

    async def start(self, *a, **kw): pass
    async def write(self, data: str): pass
    async def read_pending(self, timeout: float, max_size: int) -> TerminalRead:
        return TerminalRead()
    async def current_segment(self) -> TerminalSegment:
        return TerminalSegment(text="")
    async def interrupt(self): pass
    async def terminate(self): pass
    async def kill(self): pass
    async def is_alive(self) -> bool:
        return False
    def stdin_writable(self) -> bool:
        return False
    async def drain_startup(self): pass
    async def clear_input_line(self): pass
    def mark_command_boundary(self): pass


class SilentAliveBackend:
    """Backend that is alive but produces no output."""

    platform = Platform.WINDOWS  # type: ignore[assignment]
    visibility = TerminalVisibility.HIDDEN  # type: ignore[assignment]

    async def start(self, *a, **kw): pass
    async def write(self, data: str): pass
    async def read_pending(self, timeout: float, max_size: int) -> TerminalRead:
        return TerminalRead()
    async def current_segment(self) -> TerminalSegment:
        return TerminalSegment(text="output", is_empty_prompt=False)
    async def interrupt(self): pass
    async def terminate(self): pass
    async def kill(self): pass
    async def is_alive(self) -> bool:
        return True
    def stdin_writable(self) -> bool:
        return True
    async def drain_startup(self): pass
    async def clear_input_line(self): pass
    def mark_command_boundary(self): pass


@pytest.mark.asyncio
async def test_poll_exits_on_process_exit() -> None:
    cfg = TerminalRuntimeConfig(default_yield_ms=100)
    manager = BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=DeadBackend,
        config=cfg,
    )
    registry = ProcessRegistry(config=cfg)
    session = await manager.get_default()
    proc = registry.create(command="test", terminal="default", cwd=None, pid=None)

    result = await poll_until_settled(
        session, registry, proc.id, cfg,
        yield_ms=100, timeout_seconds=5,
    )

    assert result.outcome == PollOutcome.PROCESS_EXIT


@pytest.mark.asyncio
async def test_poll_yields_after_window() -> None:
    cfg = TerminalRuntimeConfig(default_yield_ms=10)
    manager = BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=SilentAliveBackend,
        config=cfg,
    )
    registry = ProcessRegistry(config=cfg)
    session = await manager.get_default()
    proc = registry.create(command="test", terminal="default", cwd=None, pid=None)

    result = await poll_until_settled(
        session, registry, proc.id, cfg,
        yield_ms=10, timeout_seconds=5,
    )

    assert result.outcome == PollOutcome.YIELDED


@pytest.mark.asyncio
async def test_poll_detects_stuck() -> None:
    cfg = TerminalRuntimeConfig(default_yield_ms=30_000)  # high yield
    manager = BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=SilentAliveBackend,
        config=cfg,
    )
    registry = ProcessRegistry(config=cfg)
    session = await manager.get_default()
    proc = registry.create(command="test", terminal="default", cwd=None, pid=None)

    # Simulate 16s of silence
    session._last_byte_at = time.monotonic() - 16.0

    result = await poll_until_settled(
        session, registry, proc.id, cfg,
        yield_ms=30_000, timeout_seconds=60,
    )

    assert result.outcome == PollOutcome.STUCK
