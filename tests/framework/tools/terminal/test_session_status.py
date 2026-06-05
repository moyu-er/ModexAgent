from __future__ import annotations

import time

import pytest

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import BaseTerminalManager
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
    TerminalCommandStatus,
    TerminalVisibility,
)


class FakeBackend:
    """Minimal fake backend for session status tests."""

    platform = Platform.WINDOWS
    visibility = TerminalVisibility.HIDDEN

    def __init__(self) -> None:
        self.started = False
        self.alive = True
        self._next_reads: list[TerminalRead] = []
        self._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    async def start(self, shell: str | None = None, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self.started = True

    async def write(self, data: str) -> None:
        pass

    async def read_pending(self, timeout: float = 0.1, max_size: int = 65536) -> TerminalRead:
        if self._next_reads:
            return self._next_reads.pop(0)
        return TerminalRead()

    async def read(self, timeout: float = 0.1, max_size: int = 65536) -> str:
        r = await self.read_pending(timeout, max_size)
        return r.raw

    async def current_segment(self) -> TerminalSegment:
        return self._segment

    async def interrupt(self) -> None:
        pass

    async def terminate(self) -> None:
        self.alive = False

    async def kill(self) -> None:
        self.alive = False

    async def is_alive(self) -> bool:
        return self.alive

    def stdin_writable(self) -> bool:
        return self.alive

    async def drain_startup(self) -> None:
        pass

    async def clear_input_line(self) -> None:
        pass

    def mark_command_boundary(self) -> None:
        pass

    def output_buffer_text(self) -> str:
        return ""


def _make_session():
    cfg = TerminalRuntimeConfig()
    manager = BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=FakeBackend,
        config=cfg,
    )
    return manager


@pytest.mark.asyncio
async def test_refresh_output_returns_terminal_read() -> None:
    manager = _make_session()
    session = await manager.get_default()
    backend: FakeBackend = session._backend
    backend._next_reads = [TerminalRead(stdout="hello\n", raw="hello\n")]

    result = await session.refresh_output(timeout=0.1)
    assert result.stdout == "hello\n"


@pytest.mark.asyncio
async def test_refresh_output_safe_when_dead() -> None:
    manager = _make_session()
    session = await manager.get_default()
    backend: FakeBackend = session._backend
    backend.alive = False

    result = await session.refresh_output(timeout=0.1)
    assert result.stdout == ""


@pytest.mark.asyncio
async def test_last_byte_at_updates_on_poll() -> None:
    manager = _make_session()
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    before = session._last_byte_at
    backend._next_reads = [TerminalRead(stdout="data\n", raw="data\n")]
    await session.poll_once(timeout=0.1)
    after = session._last_byte_at

    assert after > before


@pytest.mark.asyncio
async def test_last_byte_at_unchanged_on_empty_poll() -> None:
    manager = _make_session()
    session = await manager.get_default()

    before = session._last_byte_at
    await session.poll_once(timeout=0.01)
    after = session._last_byte_at

    assert after == before


@pytest.mark.asyncio
async def test_command_status_idle_when_prompt() -> None:
    manager = _make_session()
    session = await manager.get_default()
    backend: FakeBackend = session._backend
    backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)
    # Simulate some data received so we're not in UNKNOWN state
    backend._next_reads = [TerminalRead(stdout="$ ", raw="$ ")]
    await session.poll_once(timeout=0.1)

    status = await session.command_status()
    assert status == TerminalCommandStatus.IDLE


@pytest.mark.asyncio
async def test_command_status_completed_when_dead() -> None:
    manager = _make_session()
    session = await manager.get_default()
    backend: FakeBackend = session._backend
    backend.alive = False

    status = await session.command_status()
    assert status == TerminalCommandStatus.COMPLETED


@pytest.mark.asyncio
async def test_command_status_waiting_input_when_marker() -> None:
    manager = _make_session()
    session = await manager.get_default()
    backend: FakeBackend = session._backend
    backend._segment = TerminalSegment(
        text="Enter password: ", cursor_line="Enter password: ", is_empty_prompt=False
    )
    backend._next_reads = [TerminalRead(stdout="Enter password: ", raw="Enter password: ")]
    await session.poll_once(timeout=0.1)

    status = await session.command_status()
    assert status == TerminalCommandStatus.WAITING_INPUT


@pytest.mark.asyncio
async def test_command_status_executing_when_bytes_flowing() -> None:
    manager = _make_session()
    session = await manager.get_default()
    backend: FakeBackend = session._backend
    backend._segment = TerminalSegment(
        text="downloading...", cursor_line="downloading...", is_empty_prompt=False
    )

    status = await session.command_status()
    assert status == TerminalCommandStatus.EXECUTING


@pytest.mark.asyncio
async def test_command_status_stuck_when_silent_15s() -> None:
    manager = _make_session()
    session = await manager.get_default()
    backend: FakeBackend = session._backend
    backend._segment = TerminalSegment(
        text="frozen output", cursor_line="frozen output", is_empty_prompt=False
    )
    # Wind back _last_byte_at to simulate 16s of silence
    session._last_byte_at = time.monotonic() - 16.0

    status = await session.command_status()
    assert status == TerminalCommandStatus.STUCK


@pytest.mark.asyncio
async def test_last_command_output_returns_text() -> None:
    manager = _make_session()
    session = await manager.get_default()
    backend: FakeBackend = session._backend
    backend._segment = TerminalSegment(
        text="$ echo hi\nhi\n$ ", cursor_line="$ ", is_empty_prompt=True
    )

    result = await session.last_command_output()
    assert "echo hi" in result
    assert "hi" in result
