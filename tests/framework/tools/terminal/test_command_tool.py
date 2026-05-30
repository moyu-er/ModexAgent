from __future__ import annotations

import asyncio

import pytest

from framework.tools.terminal.command_tool import CommandTool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.managers import BaseTerminalManager
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo, TerminalVisibility


class FakeBackend:
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
        if not self._command_written and "\n" in data:
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

    def mark_command_boundary(self) -> None:
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
    """Prompt detection returns XML with completed status."""
    tool, manager, _registry = make_tool()
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend._preread_buffer = [
        TerminalRead(stdout="done\n", raw="done\n"),
        TerminalRead(),
    ]
    backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    result = await tool.execute(command="echo done")

    assert "<command_result>" in result
    assert "<status>completed</status>" in result
    assert "done" in result


@pytest.mark.asyncio
async def test_command_returns_running_when_yield_window_expires() -> None:
    """Default yield window returns running status with XML format."""
    cfg = TerminalRuntimeConfig(default_yield_ms=10)
    tool, _manager, registry = make_tool(cfg)

    result = await tool.execute(command="npm run dev")

    running = registry.list_running()
    assert len(running) == 1
    assert running[0].command == "npm run dev"
    assert "<command_result>" in result
    assert "<status>running</status>" in result
    assert "Command still running" in result


@pytest.mark.asyncio
async def test_command_timeout_returns_partial_output() -> None:
    """Timeout kills process and returns XML with timed_out status."""
    cfg = TerminalRuntimeConfig(
        default_command_timeout_seconds=1,
        command_tool_outer_timeout_seconds=3,
        prompt_stabilize_ms=0,
        default_yield_ms=60_000,  # above timeout — never yield
    )
    tool, manager, _registry = make_tool(cfg)
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend._preread_buffer = [TerminalRead(stdout="partial\n", raw="partial\n")]
    backend._segment = TerminalSegment(text="...", cursor_line="...", is_empty_prompt=False)

    result = await tool.execute(command="slow")

    assert "<command_result>" in result
    assert "<status>timed_out</status>" in result
    assert "partial" in result
    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_command_returns_running_with_waiting_for_input_hint() -> None:
    """Idle detection produces input_wait status in XML."""
    cfg = TerminalRuntimeConfig(
        default_yield_ms=60_000,  # above timeout — never yield normally
        input_wait_idle_ms=100,
        initial_idle_threshold_ms=50,
        default_command_timeout_seconds=5,
        command_tool_outer_timeout_seconds=10,
    )
    tool, manager, registry = make_tool(cfg)
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend._preread_buffer = [TerminalRead(stdout="password:\n", raw="password:\n")]
    backend._segment = TerminalSegment(text="password:", cursor_line="password:", is_empty_prompt=False)
    backend.alive = True

    result = await tool.execute(command="ssh host")

    assert "<command_result>" in result
    assert "<status>input_wait</status>" in result
    assert "waiting for input" in result.lower()
    assert "password:" in result


@pytest.mark.asyncio
async def test_command_completed_output_is_xml() -> None:
    """Completed result is structured XML, no legacy headers."""
    tool, manager, _registry = make_tool()
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend._preread_buffer = [TerminalRead(stdout="hello\n", raw="hello\n"), TerminalRead()]
    backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    result = await tool.execute(command="echo hello")

    assert "<command_result>" in result
    assert "<status>completed</status>" in result
    assert "hello" in result
    # No legacy structured headers
    assert "[Command Result]" not in result
    assert "[Output]" not in result
    assert "[State]" not in result


@pytest.mark.asyncio
async def test_command_timed_out_format() -> None:
    """Timed-out result has XML format with timed_out status."""
    cfg = TerminalRuntimeConfig(
        default_command_timeout_seconds=1,
        command_tool_outer_timeout_seconds=3,
        prompt_stabilize_ms=0,
        default_yield_ms=60_000,
    )
    tool, manager, _registry = make_tool(cfg)
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend._preread_buffer = [TerminalRead(stdout="build...\n", raw="build...\n")]
    backend._segment = TerminalSegment(text="...", cursor_line="...", is_empty_prompt=False)

    result = await tool.execute(command="build")

    assert "<command_result>" in result
    assert "<status>timed_out</status>" in result
    assert "build..." in result
    assert "timed out" in result.lower()
    # No legacy headers
    assert "[Command Result]" not in result
    assert "[State]" not in result


@pytest.mark.asyncio
async def test_command_registry_tracks_process() -> None:
    """Registry records the process after execute."""
    cfg = TerminalRuntimeConfig(default_yield_ms=10)
    tool, _manager, registry = make_tool(cfg)

    await tool.execute(command="echo hi")

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

    backend._preread_buffer = [TerminalRead(stdout="output\n", raw="output\n")]
    backend._segment = TerminalSegment(text="", cursor_line="", is_empty_prompt=False)

    async def always_dead() -> bool:
        return False

    backend.is_alive = always_dead  # type: ignore[assignment]

    result = await tool.execute(command="exit")

    assert "<command_result>" in result
    assert "<status>completed</status>" in result
    assert "output" in result


@pytest.mark.asyncio
async def test_command_uses_submit_command() -> None:
    """Command is submitted via submit_command (readline ending \\n for bash)."""
    tool, manager, _registry = make_tool()
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    backend.reads = [TerminalRead()]
    backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    await tool.execute(command="ls")

    # submit_command uses shell_family.command_ending() which is \n for bash
    assert any("ls\n" in w for w in backend.writes)


@pytest.mark.asyncio
async def test_command_tool_properties() -> None:
    """Tool has minimal parameter set: only command."""
    tool, _, _ = make_tool()

    assert tool.name == "command"
    assert "terminal" in tool.description.lower() or "command" in tool.description.lower()
    params = tool.parameters
    assert "command" in params["properties"]
    assert params["required"] == ["command"]
    # Only command should be present
    assert len(params["properties"]) == 1
    # Removed parameters must not be present
    for removed in ("background", "long_running", "terminal", "workdir", "env", "timeout", "yield_ms", "pty"):
        assert removed not in params["properties"]


@pytest.mark.asyncio
async def test_command_no_output_returns_placeholder() -> None:
    """No output returns '(no output)' placeholder in XML when process exits."""
    tool, manager, _registry = make_tool()
    session = await manager.get_default()
    backend: FakeBackend = session._backend

    # Backend dies immediately with no output
    backend.alive = False

    async def always_dead() -> bool:
        return False

    backend.is_alive = always_dead  # type: ignore[assignment]

    result = await tool.execute(command="true")

    assert "(no output)" in result
    assert "<status>completed</status>" in result
    assert "<command_result>" in result
