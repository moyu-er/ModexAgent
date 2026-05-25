"""Tests for [Screen] snapshot in CommandTool and ProcessTool results.

When cursor_key_mode is APPLICATION (TUI program detected), running results
include a [Screen] section with the current terminal segment content.
"""

from __future__ import annotations

import asyncio

import pytest

from framework.tools.terminal.command_tool import CommandTool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import BaseTerminalManager
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.process_tool import ProcessTool
from framework.tools.terminal.pty_keys import CursorKeyMode
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, ProcessStatus, ShellFamily, ShellInfo, TerminalVisibility


class FakeBackend:
    """Minimal fake backend for screen snapshot tests."""

    platform = Platform.WINDOWS
    visibility = TerminalVisibility.HIDDEN

    def __init__(self) -> None:
        self.started = False
        self.writes: list[str] = []
        self._preread_buffer: list[TerminalRead] = []
        self.reads: list[TerminalRead] = []
        self.alive = True
        self._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)
        self._command_written = False

    async def start(self, shell, cwd, env) -> None:
        self.started = True

    async def write(self, data: str) -> None:
        self.writes.append(data)
        if not self._command_written and "\r" in data:
            self._command_written = True
            self.reads.extend(self._preread_buffer)
            self._preread_buffer.clear()

    async def read_pending(self, timeout: float, max_size: int) -> TerminalRead:
        if self.reads:
            return self.reads.pop(0)
        await asyncio.sleep(0)
        return TerminalRead()

    async def current_segment(self) -> TerminalSegment:
        return self._segment

    async def interrupt(self) -> None:
        self.writes.append("\x03")

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

    async def read(self, timeout: float, max_size: int) -> str:
        r = await self.read_pending(timeout, max_size)
        return r.raw


def _make_command_tool(
    config: TerminalRuntimeConfig | None = None,
) -> tuple[CommandTool, BaseTerminalManager, ProcessRegistry]:
    cfg = config or TerminalRuntimeConfig()
    manager = BaseTerminalManager(
        shell_info=ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS),
        visibility=TerminalVisibility.HIDDEN,
        backend_factory=FakeBackend,
        config=cfg,
    )
    registry = ProcessRegistry(config=cfg)
    tool = CommandTool(manager=manager, registry=registry, config=cfg)
    return tool, manager, registry


# ---------------------------------------------------------------------------
# CommandTool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_screen_section_when_tui_active() -> None:
    """When cursor_key_mode is APPLICATION, [Screen] appears in running result."""
    cfg = TerminalRuntimeConfig(
        default_command_timeout_seconds=60,
        command_tool_outer_timeout_seconds=70,
    )
    tool, manager, _registry = _make_command_tool(cfg)
    session = await manager.get_or_create("default")
    backend: FakeBackend = session._backend

    # Simulate TUI mode: set cursor_key_mode to APPLICATION
    session.cursor_key_mode = CursorKeyMode.APPLICATION

    # Provide screen content
    backend._segment = TerminalSegment(
        text="  1 hello.py\n  2 print('hi')\n~ \n\"hello.py\" 3L, 42C",
        cursor_line="\"hello.py\" 3L, 42C",
        is_empty_prompt=False,
    )

    # No output so the command stays running until yield_ms
    backend._preread_buffer = []

    result = await tool.execute(command="vim file.py", terminal="default", yield_ms=50, timeout=5)

    assert "status: running" in result
    assert "[Screen]" in result
    assert "hello.py" in result
    assert "send_keys" in result


@pytest.mark.asyncio
async def test_command_no_screen_when_normal_mode() -> None:
    """When cursor_key_mode is NORMAL, [Screen] does not appear."""
    cfg = TerminalRuntimeConfig(
        default_command_timeout_seconds=60,
        command_tool_outer_timeout_seconds=70,
    )
    tool, manager, _registry = _make_command_tool(cfg)
    session = await manager.get_or_create("default")
    backend: FakeBackend = session._backend

    # Normal mode (default)
    assert session.cursor_key_mode == CursorKeyMode.UNKNOWN

    backend._preread_buffer = []
    backend._segment = TerminalSegment(
        text="some output",
        cursor_line="some output",
        is_empty_prompt=False,
    )

    result = await tool.execute(command="cat file", terminal="default", yield_ms=50, timeout=5)

    assert "status: running" in result
    assert "[Screen]" not in result


@pytest.mark.asyncio
async def test_command_no_screen_on_completed() -> None:
    """[Screen] does not appear in completed results (only running)."""
    tool, manager, _registry = _make_command_tool()
    session = await manager.get_or_create("default")
    backend: FakeBackend = session._backend

    session.cursor_key_mode = CursorKeyMode.APPLICATION
    backend._preread_buffer = [
        TerminalRead(stdout="done\n", raw="done\n"),
        TerminalRead(),
    ]
    backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    result = await tool.execute(command="echo done", terminal="default", yield_ms=10000, timeout=10)

    assert "status: completed" in result
    assert "[Screen]" not in result


# ---------------------------------------------------------------------------
# ProcessTool tests
# ---------------------------------------------------------------------------


class FakeTerminalWithScreen:
    """Fake terminal session with cursor_key_mode and current_segment."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.interrupted = False
        self.killed = False
        self.name = "default"
        self.cursor_key_mode = CursorKeyMode.UNKNOWN
        self._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    async def write(self, data: str) -> None:
        self.writes.append(data)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def terminate(self) -> None:
        self.killed = True

    async def is_alive(self) -> bool:
        return not self.killed

    async def current_segment(self) -> TerminalSegment:
        return self._segment


class FakeManagerWithScreen:
    def __init__(self, terminal: FakeTerminalWithScreen) -> None:
        self.terminal = terminal

    async def get_or_create(self, name, workdir=None):
        return self.terminal


@pytest.mark.asyncio
async def test_process_poll_screen_when_tui_active() -> None:
    """ProcessTool poll includes [Screen] when terminal is in APPLICATION mode."""
    registry = ProcessRegistry()
    terminal = FakeTerminalWithScreen()
    terminal.cursor_key_mode = CursorKeyMode.APPLICATION
    terminal._segment = TerminalSegment(
        text="  1 main.py\n  2 import os\n~\n\"main.py\" 2L, 28C",
        cursor_line="\"main.py\" 2L, 28C",
        is_empty_prompt=False,
    )
    tool = ProcessTool(registry=registry, manager=FakeManagerWithScreen(terminal))

    session = registry.create(command="vim main.py", terminal="default", cwd=None, pid=1)
    registry.append_output(session.id, "stdout", "opening...\n")

    result = await tool.execute(action="poll", session_id=session.id)

    assert "[Screen]" in result
    assert "main.py" in result
    assert "send_keys" in result


@pytest.mark.asyncio
async def test_process_poll_no_screen_when_normal_mode() -> None:
    """ProcessTool poll does not include [Screen] in NORMAL/UNKNOWN mode."""
    registry = ProcessRegistry()
    terminal = FakeTerminalWithScreen()
    tool = ProcessTool(registry=registry, manager=FakeManagerWithScreen(terminal))

    session = registry.create(command="python script.py", terminal="default", cwd=None, pid=2)
    registry.append_output(session.id, "stdout", "running...\n")

    result = await tool.execute(action="poll", session_id=session.id)

    assert "[Screen]" not in result


@pytest.mark.asyncio
async def test_process_poll_screen_empty_segment_omitted() -> None:
    """[Screen] is omitted when the segment text is empty/whitespace."""
    registry = ProcessRegistry()
    terminal = FakeTerminalWithScreen()
    terminal.cursor_key_mode = CursorKeyMode.APPLICATION
    terminal._segment = TerminalSegment(text="   ", cursor_line="   ", is_empty_prompt=False)
    tool = ProcessTool(registry=registry, manager=FakeManagerWithScreen(terminal))

    session = registry.create(command="vim", terminal="default", cwd=None, pid=3)
    registry.append_output(session.id, "stdout", "start\n")

    result = await tool.execute(action="poll", session_id=session.id)

    assert "[Screen]" not in result
