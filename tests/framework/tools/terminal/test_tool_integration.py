"""Cross-tool integration tests: CommandTool + ProcessTool + TerminalTool."""

from __future__ import annotations

import time

import pytest

from framework.tools.terminal.command_tool import CommandTool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.process_tool import ProcessTool
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.tool import TerminalTool
from framework.tools.terminal.types import TerminalCommandStatus

from tests.framework.tools.terminal.conftest import FakeBackend, make_manager_and_registry


def _config(**overrides) -> TerminalRuntimeConfig:
    defaults = dict(
        no_output_timeout_ms=30_000,
        long_running_threshold_ms=300_000,
        prompt_stabilize_ms=50,
        default_yield_ms=200,  # must be > prompt_stabilize_ms for prompt detection to win
        default_command_timeout_seconds=5,
        command_tool_outer_timeout_seconds=10,
    )
    defaults.update(overrides)
    return TerminalRuntimeConfig(**defaults)


class TestCommandToolGuardIntegration:
    """CommandTool rejects when terminal is busy."""

    @pytest.mark.asyncio
    async def test_executing_command_rejects_new_command(self) -> None:
        cfg = _config()
        manager, registry = make_manager_and_registry(config=cfg)
        tool = CommandTool(manager=manager, registry=registry, config=cfg)

        # Start first command (will yield because backend has no prompt)
        session = await manager.get_default()
        session._ever_received_bytes = True
        backend: FakeBackend = session._backend
        backend._segment = TerminalSegment(text="running...", cursor_line="running...", is_empty_prompt=False)
        session._command_started_at = time.monotonic()

        # Second command should be rejected
        result = await tool.execute(command="echo second")
        assert "<status>rejected</status>" in result
        assert "executing" in result.lower()

    @pytest.mark.asyncio
    async def test_idle_allows_command(self) -> None:
        cfg = _config()
        manager, registry = make_manager_and_registry(config=cfg)
        tool = CommandTool(manager=manager, registry=registry, config=cfg)

        session = await manager.get_default()
        session._ever_received_bytes = True
        session._backend_started = True
        session._needs_restart = False
        backend: FakeBackend = session._backend
        # Simulate prompt ready
        backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)
        # Queue two identical reads: one for guard check, one for poll loop
        backend._read_queue = [
            TerminalRead(stdout="hello\n", raw="hello\n"),
            TerminalRead(stdout="hello\n", raw="hello\n"),
        ]

        result = await tool.execute(command="echo hello")
        assert "<status>completed</status>" in result


class TestProcessToolGuardIntegration:
    """ProcessTool._do_write rejects when terminal is busy."""

    @pytest.mark.asyncio
    async def test_executing_rejects_process_write(self) -> None:
        cfg = _config()
        manager, registry = make_manager_and_registry(config=cfg)
        tool = ProcessTool(registry=registry, manager=manager, config=cfg)

        session = await manager.get_default()
        session._ever_received_bytes = True
        session._command_started_at = time.monotonic()
        backend: FakeBackend = session._backend
        backend._segment = TerminalSegment(text="running...", cursor_line="running...", is_empty_prompt=False)

        # Create a running process session
        proc = registry.create(command="longcmd", terminal=session.name, cwd=None, pid=None)

        result = await tool.execute(action="write", data="some input")
        assert "<status>rejected</status>" in result

    @pytest.mark.asyncio
    async def test_interrupt_bypasses_guard(self) -> None:
        """Interrupt always works, even when terminal is busy."""
        cfg = _config()
        manager, registry = make_manager_and_registry(config=cfg)
        tool = ProcessTool(registry=registry, manager=manager, config=cfg)

        session = await manager.get_default()
        session._ever_received_bytes = True
        session._command_started_at = time.monotonic()
        backend: FakeBackend = session._backend
        backend._segment = TerminalSegment(text="running...", cursor_line="running...", is_empty_prompt=False)

        proc = registry.create(command="longcmd", terminal=session.name, cwd=None, pid=None)

        result = await tool.execute(action="interrupt")
        assert "rejected" not in result.lower()
        assert "\x03" in backend.writes  # Ctrl+C was sent


class TestRecoveryFlow:
    """After interrupt, terminal should return to usable state."""

    @pytest.mark.asyncio
    async def test_interrupt_then_command_allowed(self) -> None:
        cfg = _config()
        manager, registry = make_manager_and_registry(config=cfg)
        cmd_tool = CommandTool(manager=manager, registry=registry, config=cfg)
        proc_tool = ProcessTool(registry=registry, manager=manager, config=cfg)

        session = await manager.get_default()
        session._ever_received_bytes = True
        session._backend_started = True
        session._needs_restart = False
        session._command_started_at = time.monotonic()
        backend: FakeBackend = session._backend
        backend._segment = TerminalSegment(text="running...", cursor_line="running...", is_empty_prompt=False)

        # 1. Command rejected (busy)
        result = await cmd_tool.execute(command="echo test")
        assert "<status>rejected</status>" in result

        # 2. Interrupt (bypasses guard)
        proc = registry.create(command="stuck", terminal=session.name, cwd=None, pid=None)
        await proc_tool.execute(action="interrupt")

        # 3. Reset session state (simulating prompt return after interrupt)
        backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)
        session._command_started_at = None
        # Queue two identical reads: one for guard check, one for poll loop
        backend._read_queue = [
            TerminalRead(stdout="done\n", raw="done\n"),
            TerminalRead(stdout="done\n", raw="done\n"),
        ]

        # 4. Command now allowed
        result = await cmd_tool.execute(command="echo done")
        assert "<status>completed</status>" in result


class TestAntiInterference:
    """Visible terminal interference detection."""

    @pytest.mark.asyncio
    async def test_interference_warning_in_terminal_current(self) -> None:
        cfg = _config()
        manager, registry = make_manager_and_registry(config=cfg)
        term_tool = TerminalTool(manager=manager, registry=registry)

        session = await manager.get_default()
        # Make visible
        session._backend.visibility = "visible"
        session._ever_received_bytes = True

        # Simulate: agent expected EXECUTING, but terminal is now IDLE
        session.set_expected_state(TerminalCommandStatus.EXECUTING)
        session._backend._segment = TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

        result = await term_tool.execute(action="current")
        assert "<interference_warning>" in result
        assert "executing" in result.lower()
        assert "idle" in result.lower()
