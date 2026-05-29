"""Windows integration tests -- hidden backend with WSL bash.

Uses session-scoped terminal to avoid per-test PTY startup cost.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from framework.tools.terminal.command_tool import CommandTool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.manager import TerminalManager
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.process_tool import ProcessTool
from framework.tools.terminal.tool import TerminalTool
from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo, detect_platform_shell

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")


@pytest.fixture(scope="session")
def manager() -> TerminalManager:
    from framework.tools.terminal.backends.windows_hidden import WindowsHiddenPtyBackend

    shell = detect_platform_shell() or ShellInfo(ShellFamily.CMD, "cmd.exe", Platform.WINDOWS)
    return TerminalManager(
        storage_dir=Path("data/test_terminals"),
        max_terminals=3,
        backend_factory=WindowsHiddenPtyBackend,
        shell_info=shell,
    )


def _cfg(**kw: int) -> TerminalRuntimeConfig:
    # Map short aliases to actual field names
    if "yield_ms" in kw:
        kw["default_yield_ms"] = kw.pop("yield_ms")
    defaults = dict(
        default_command_timeout_seconds=5,
        default_yield_ms=3000,
        command_tool_outer_timeout_seconds=10,
        input_wait_idle_ms=2000,
        initial_idle_threshold_ms=1000,
        prompt_stabilize_ms=100,
    )
    defaults.update(kw)
    return TerminalRuntimeConfig(**defaults)


@pytest.mark.asyncio
async def test_echo_completes(manager: TerminalManager) -> None:
    r = await CommandTool(manager, ProcessRegistry(), _cfg()).execute(command="echo ok")
    assert "ok" in r


@pytest.mark.asyncio
async def test_slow_yields_running(manager: TerminalManager) -> None:
    cfg = _cfg(yield_ms=500, default_command_timeout_seconds=30)
    r = await CommandTool(manager, ProcessRegistry(), cfg).execute(command="ping 127.0.0.1 -n 10")
    assert "Command still running" in r


@pytest.mark.asyncio
async def test_short_timeout(manager: TerminalManager) -> None:
    cfg = _cfg(default_command_timeout_seconds=2, default_yield_ms=30000)
    r = await CommandTool(manager, ProcessRegistry(), cfg).execute(command="ping 127.0.0.1 -n 30")
    assert "timed out" in r.lower()


@pytest.mark.asyncio
async def test_poll_and_kill(manager: TerminalManager) -> None:
    cfg = _cfg(yield_ms=500, default_command_timeout_seconds=30)
    reg = ProcessRegistry()
    await CommandTool(manager, reg, cfg).execute(command="ping 127.0.0.1 -n 60")
    proc = ProcessTool(registry=reg, manager=manager)
    poll = await proc.execute(action="poll")
    assert len(poll) > 0
    await proc.execute(action="kill")
    assert len(reg.list_running()) == 0


@pytest.mark.asyncio
async def test_write_and_interrupt(manager: TerminalManager) -> None:
    cfg = _cfg(yield_ms=500, default_command_timeout_seconds=15)
    reg = ProcessRegistry()
    r = await CommandTool(manager, reg, cfg).execute(
        command="bash -c \"read -r x && echo got:$x\""
    )
    proc = ProcessTool(registry=reg, manager=manager)
    if "Command still running" in r:
        await proc.execute(action="write", data="hello\n")
        await proc.execute(action="submit")
    for s in reg.list_running():
        await proc.execute(action="kill")


@pytest.mark.asyncio
async def test_terminal_list_current(manager: TerminalManager) -> None:
    tool = TerminalTool(manager)
    await tool.execute(action="open", name="test-1")
    listed = await tool.execute(action="list")
    assert "test-1" in listed or "default" in listed
    current = await tool.execute(action="current")
    assert len(current) > 0


@pytest.mark.asyncio
async def test_multi_command(manager: TerminalManager) -> None:
    cfg = _cfg()
    reg = ProcessRegistry()
    cmd = CommandTool(manager, reg, cfg)
    assert "first" in await cmd.execute(command="echo first")
    assert "second" in await cmd.execute(command="echo second")


@pytest.mark.skip(reason="Visible terminal requires interactive desktop — run manually")
@pytest.mark.asyncio
async def test_visible_terminal_end_to_end() -> None:
    """Visible terminal: opens a console window, runs a command, verifies output."""
    from framework.tools.terminal.managers import create_terminal_manager
    mgr = create_terminal_manager(manager_kind="windows_visible")
    reg = ProcessRegistry()
    cfg = _cfg()

    r = await CommandTool(mgr, reg, cfg).execute(command="echo visible-ok")
    assert "visible-ok" in r
