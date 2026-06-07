"""Shared test fixtures for terminal tool tests."""

from __future__ import annotations

import pytest

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import BaseTerminalManager
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.session import TerminalSession
from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo, TerminalVisibility


class FakeBackend:
    """Controllable terminal backend for testing."""

    platform = Platform.WINDOWS
    visibility = TerminalVisibility.HIDDEN
    window_title = "fake"

    def __init__(self) -> None:
        self.started = False
        self.writes: list[str] = []
        self._read_queue: list[TerminalRead] = []
        self._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)
        self._alive = True
        self._output_buffer_text = ""

    async def start(self, shell=None, cwd=None, env=None) -> None:
        self.started = True

    async def write(self, data: str) -> None:
        self.writes.append(data)

    async def read_pending(self, timeout=0.05, max_size=65536) -> TerminalRead:
        if self._read_queue:
            return self._read_queue.pop(0)
        return TerminalRead()

    async def read(self, timeout=0.05, max_size=65536) -> str:
        r = await self.read_pending(timeout, max_size)
        return r.raw

    async def current_segment(self) -> TerminalSegment:
        return self._segment

    async def interrupt(self) -> None:
        self.writes.append("\x03")

    async def terminate(self) -> None:
        self._alive = False

    async def kill(self) -> None:
        self._alive = False

    async def is_alive(self) -> bool:
        return self._alive

    def stdin_writable(self) -> bool:
        return self._alive

    async def drain_startup(self) -> None:
        pass

    async def clear_input_line(self) -> None:
        pass

    def mark_command_boundary(self) -> None:
        pass

    def output_buffer_text(self) -> str:
        return self._output_buffer_text


def make_session(
    *,
    name: str = "test",
    visible: bool = False,
    shell_family: ShellFamily = ShellFamily.BASH,
) -> TerminalSession:
    """Create a TerminalSession with FakeBackend for testing."""
    backend = FakeBackend()
    if visible:
        backend.visibility = TerminalVisibility.VISIBLE
    return TerminalSession(
        name=name,
        backend=backend,
        shell_info=ShellInfo(shell_family, "bash", Platform.WINDOWS),
    )


def make_manager_and_registry(
    *,
    config: TerminalRuntimeConfig | None = None,
) -> tuple[BaseTerminalManager, ProcessRegistry]:
    """Create a BaseTerminalManager + ProcessRegistry pair for testing."""
    cfg = config or TerminalRuntimeConfig()
    manager = BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=FakeBackend,
        config=cfg,
    )
    registry = ProcessRegistry(config=cfg)
    return manager, registry
