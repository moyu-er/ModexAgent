from __future__ import annotations

import asyncio

import pytest

from framework.tools.terminal.command_tool import CommandTool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import BaseTerminalManager
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, ProcessStatus, ShellFamily, ShellInfo, TerminalVisibility


class FakeBackend:
    """Minimal fake backend for CommandTool tests.

    Reads queued *before* a command write are held in ``_preread_buffer``
    and only released after ``write()`` is called.  This prevents
    ``ensure_started() → _discard_pending_output()`` from consuming the
    test's queued reads.
    """

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
        # First write is the command — release pre-queued reads
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


def make_tool(
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_returns_completed_when_prompt_detected() -> None:
    """Solution A: prompt detection returns completed for fast commands."""
    tool, manager, _registry = make_tool()
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    # Queue reads in preread buffer — released after command write
    backend._preread_buffer = [
        TerminalRead(stdout="done\n", raw="done\n"),
        TerminalRead(),
    ]
    backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    result = await tool.execute(command="echo done")

    assert "status: completed" in result
    assert "done" in result


@pytest.mark.asyncio
async def test_command_background_returns_running_session_id() -> None:
    """background=True returns immediately with status running."""
    tool, _manager, registry = make_tool()

    result = await tool.execute(command="npm run dev", background=True)

    running = registry.list_running()
    assert "status: running" in result
    assert len(running) == 1
    assert running[0].command == "npm run dev"


@pytest.mark.asyncio
async def test_command_timeout_returns_timed_out_with_captured_output() -> None:
    """timeout fires before yield_ms, returns partial output."""
    cfg = TerminalRuntimeConfig(
        default_command_timeout_seconds=1,
        long_running_timeout_seconds=5,
        command_tool_outer_timeout_seconds=3,
        prompt_stabilize_ms=0,
    )
    tool, manager, _registry = make_tool(cfg)
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    # Backend stays alive but we give it one chunk of partial output
    backend._preread_buffer = [TerminalRead(stdout="partial\n", raw="partial\n")]
    backend._segment = TerminalSegment(text="...", cursor_line="...", is_empty_prompt=False)

    result = await tool.execute(command="slow")

    assert "status: timed_out" in result
    assert "partial" in result


@pytest.mark.asyncio
async def test_command_returns_running_with_waiting_for_input_hint() -> None:
    """waiting_for_input from registry triggers early running return."""
    cfg = TerminalRuntimeConfig(
        input_wait_idle_ms=100,
        input_wait_early_min_elapsed_ms=50,
    )
    tool, manager, registry = make_tool(cfg)
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    # Output then silence — alive stays True
    backend._preread_buffer = [TerminalRead(stdout="password:\n", raw="password:\n")]
    backend._segment = TerminalSegment(text="password:", cursor_line="password:", is_empty_prompt=False)
    backend.alive = True

    result = await tool.execute(command="ssh host")

    assert "status: running" in result


@pytest.mark.asyncio
async def test_command_format_completed_structure() -> None:
    """Verify the completed result has the right section headers."""
    tool, manager, _registry = make_tool()
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend._preread_buffer = [TerminalRead(stdout="hello\n", raw="hello\n"), TerminalRead()]
    backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    result = await tool.execute(command="echo hello")

    assert "[Command Result]" in result
    assert "session_id: ps-" in result
    assert "terminal: default" in result
    assert "duration_ms:" in result
    assert "[Output]" in result


@pytest.mark.asyncio
async def test_command_format_timed_out_structure() -> None:
    """Verify the timed-out result has the right fields."""
    cfg = TerminalRuntimeConfig(
        default_command_timeout_seconds=1,
        long_running_timeout_seconds=5,
        command_tool_outer_timeout_seconds=3,
        prompt_stabilize_ms=0,
    )
    tool, manager, _registry = make_tool(cfg)
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend._preread_buffer = [TerminalRead(stdout="build...\n", raw="build...\n")]
    backend._segment = TerminalSegment(text="...", cursor_line="...", is_empty_prompt=False)

    result = await tool.execute(command="build")

    assert "[Command Result]" in result
    assert "status: timed_out" in result
    assert "timed_out: true" in result
    assert "[State]" in result
    assert "Timed out after" in result


@pytest.mark.asyncio
async def test_command_registry_tracks_process() -> None:
    """Registry has the process recorded after execute."""
    tool, _manager, registry = make_tool()

    await tool.execute(command="echo hi", background=True)

    running = registry.list_running()
    assert len(running) == 1
    assert running[0].command == "echo hi"
    assert running[0].terminal == "default"


@pytest.mark.asyncio
async def test_command_process_exits_authoritative() -> None:
    """Process exit (is_alive=False) is the authoritative completion signal."""
    tool, manager, _registry = make_tool()
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    # Backend dies immediately after one read
    backend._preread_buffer = [TerminalRead(stdout="output\n", raw="output\n")]
    backend._segment = TerminalSegment(text="", cursor_line="", is_empty_prompt=False)

    # Make is_alive return False — process has exited
    async def always_dead() -> bool:
        return False

    backend.is_alive = always_dead  # type: ignore[assignment]

    result = await tool.execute(command="exit")

    assert "status: completed" in result


@pytest.mark.asyncio
async def test_command_writes_newline_to_session() -> None:
    """Verify the command is written with \\r (readline ending)."""
    tool, manager, _registry = make_tool()
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend.reads = [TerminalRead()]
    backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    await tool.execute(command="ls")

    # Should have written "ls\r"
    assert any("ls\r" in w for w in backend.writes)


@pytest.mark.asyncio
async def test_command_tool_properties() -> None:
    """Tool name, description, parameters are correct."""
    tool, _, _ = make_tool()

    assert tool.name == "command"
    assert "command" in tool.description.lower() or "terminal" in tool.description.lower()
    params = tool.parameters
    assert "command" in params["properties"]
    assert "background" in params["properties"]
    assert "long_running" in params["properties"]
    assert params["required"] == ["command"]
    # Removed parameters must not be present
    for removed in ("terminal", "workdir", "env", "timeout", "yield_ms", "pty"):
        assert removed not in params["properties"]


@pytest.mark.asyncio
async def test_command_long_running_uses_extended_timeout() -> None:
    """long_running=True uses the extended timeout from config."""
    cfg = TerminalRuntimeConfig(
        default_command_timeout_seconds=1,
        long_running_timeout_seconds=3,
        command_tool_outer_timeout_seconds=5,
        prompt_stabilize_ms=0,
        # Prevent input-wait detection from firing before the 3s timeout
        input_wait_early_min_elapsed_ms=60_000,
    )
    tool, manager, _registry = make_tool(cfg)
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    # With default timeout (1s), this would time out quickly.
    # With long_running (3s), it should also time out but report the longer timeout.
    backend._preread_buffer = [TerminalRead(stdout="partial\n", raw="partial\n")]
    backend._segment = TerminalSegment(text="...", cursor_line="...", is_empty_prompt=False)

    result = await tool.execute(command="long-build", long_running=True)

    assert "status: timed_out" in result
    # The message should reference the long_running timeout (3s), not default (1s)
    assert "Timed out after 3s" in result
