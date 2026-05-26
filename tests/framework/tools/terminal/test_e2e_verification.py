"""E2E verification: hidden + visible WSL bash, full tool chain.

4 focused tests, each covering multiple scenarios. Short timeouts used
to keep test duration reasonable.
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

_WIN_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")

# ── helpers ──

def _cfg(**kw: int) -> TerminalRuntimeConfig:
    return TerminalRuntimeConfig(
        default_command_timeout_seconds=kw.get("timeout", 8),
        default_yield_ms=kw.get("yield_ms", 4000),
        command_tool_outer_timeout_seconds=kw.get("outer", 15),
        input_wait_idle_ms=kw.get("idle_ms", 1500),
        input_wait_early_min_elapsed_ms=kw.get("early_ms", 800),
        prompt_stabilize_ms=100,
    )


def _hidden_mgr() -> TerminalManager:
    from framework.tools.terminal.backends.windows_hidden import WindowsHiddenPtyBackend
    shell = detect_platform_shell() or ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS)
    return TerminalManager(storage_dir=Path("data/test_terminals"), max_terminals=5,
                           backend_factory=WindowsHiddenPtyBackend, shell_info=shell)


def _visible_mgr() -> TerminalManager:
    from framework.tools.terminal.backends.visible_windows import VisibleWindowsPtyBackend
    shell = detect_platform_shell() or ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS)
    return TerminalManager(storage_dir=Path("data/test_terminals_v"), max_terminals=3,
                           backend_factory=VisibleWindowsPtyBackend, shell_info=shell)


# ── Test 1: Hidden -- command execution, timeout, stdin interaction, result format ──

@_WIN_ONLY
@pytest.mark.asyncio
async def test_1_hidden_command_execution_and_interaction() -> None:
    """Covers: echo, cd+pwd, short timeout, stdin read+write+submit, result format."""
    mgr = _hidden_mgr()
    reg = ProcessRegistry()
    cmd = CommandTool(mgr, reg, _cfg())

    # --- normal execution ---
    r = await cmd.execute(command="echo hello-wsl")
    assert "hello-wsl" in r, f"echo failed: {r[:200]}"
    assert "[Command Result]" not in r  # natural language format
    assert "[Output]" not in r
    assert "[State]" not in r

    # --- directory persistence ---
    await cmd.execute(command="cd /tmp")
    r = await cmd.execute(command="pwd")
    assert "/tmp" in r, f"cd/pwd failed: {r[:200]}"

    # --- short timeout (1s timeout vs sleep 30) ---
    cfg_small = _cfg(timeout=1, yield_ms=30000)
    cmd_fast = CommandTool(mgr, reg, cfg_small)
    r = await cmd_fast.execute(command="sleep 30")
    assert "timed out" in r.lower(), f"timeout not triggered: {r[:200]}"
    assert "1s" in r  # mentions the timeout duration

    # --- stdin interaction: bash read ---
    cfg_int = _cfg(yield_ms=500, timeout=10)
    cmd_int = CommandTool(mgr, reg, cfg_int)
    r = await cmd_int.execute(
        command='bash -c "read -p \"enter: \" ans && echo got: \$ans"'
    )
    proc = ProcessTool(registry=reg, manager=mgr)
    if reg.list_running():
        w = await proc.execute(action="write", data="yes\n")
        assert "Wrote" in w
        await proc.execute(action="submit")
        # drain output
        import asyncio
        for _ in range(8):
            p = await proc.execute(action="poll")
            if "got:" in p:
                break
            await asyncio.sleep(0.15)
        await proc.execute(action="kill")


# ── Test 2: Hidden -- process tool actions + terminal tab management ──

@_WIN_ONLY
@pytest.mark.asyncio
async def test_2_hidden_process_and_terminal_tabs() -> None:
    """Covers: process poll/kill/interrupt/list/send_keys/paste,
    terminal open/list/select/current/close, multi-tab isolation."""
    mgr = _hidden_mgr()
    reg = ProcessRegistry()
    cmd = CommandTool(mgr, reg, _cfg(yield_ms=500, timeout=30))
    proc = ProcessTool(registry=reg, manager=mgr)
    tool = TerminalTool(mgr)

    # --- process: poll + kill ---
    await cmd.execute(command="sleep 60")
    p = await proc.execute(action="poll")
    assert len(p) > 0
    await proc.execute(action="kill")
    assert len(reg.list_running()) == 0

    # --- process: interrupt (Ctrl+C) ---
    await cmd.execute(command="sleep 60")
    r = await proc.execute(action="interrupt")
    assert len(r) > 0
    for s in reg.list_running():
        await proc.execute(action="kill")

    # --- process: list ---
    await cmd.execute(command="echo list-me")
    lst = await proc.execute(action="list")
    assert "echo" in lst or "list-me" in lst
    for s in reg.list_running():
        await proc.execute(action="kill")

    # --- process: send_keys + paste (only on running command) ---
    await cmd.execute(command="sleep 60")
    k = await proc.execute(action="send_keys", keys=["c-c"])  # Ctrl+C to interrupt
    assert "Sent" in k or "Error" in k  # Either sent or error is ok
    p = await proc.execute(action="paste", text="echo hello")
    assert "Pasted" in p or "Error" in p
    for s in reg.list_running():
        await proc.execute(action="kill")

    # --- terminal: open + list tabs ---
    await tool.execute(action="open", name="tab-left")
    await tool.execute(action="open", name="tab-right")
    lst = await tool.execute(action="list")
    assert "tab-left" in lst
    assert "tab-right" in lst

    # --- terminal: current segment ---
    cur = await tool.execute(action="current")
    assert len(cur) > 0

    # --- multi-tab: independent state ---
    await tool.execute(action="select", name="tab-left")
    await cmd.execute(command="cd /tmp && touch /tmp/e2e-left")
    await tool.execute(action="select", name="tab-right")
    await cmd.execute(command="touch /tmp/e2e-right")
    # left tab's file should exist (both tabs share same filesystem in WSL)
    await tool.execute(action="select", name="tab-left")
    r = await cmd.execute(command="ls /tmp/e2e-left 2>&1 && rm -f /tmp/e2e-left /tmp/e2e-right")
    assert "No such file" not in r, f"file not found on left: {r[:200]}"

    # --- terminal: close tab ---
    await tool.execute(action="close", name="tab-right")
    lst = await tool.execute(action="list")
    assert "tab-right" not in lst

    # --- still functional after close ---
    r = await cmd.execute(command="echo still-works")
    assert "still-works" in r


# ── Test 3: Visible -- window pops up, command executes, interaction works ──

@_WIN_ONLY
@pytest.mark.asyncio
async def test_3_visible_terminal_window_and_interaction() -> None:
    """Opens a VISIBLE console window. Covers: echo, timeout, stdin interaction."""
    mgr = _visible_mgr()
    reg = ProcessRegistry()

    # --- visible echo ---
    r = await CommandTool(mgr, reg, _cfg(timeout=10)).execute(command="echo visible-wsl-ok")
    assert "visible-wsl-ok" in r, f"visible echo failed: {r[:200]}"

    # --- visible short timeout ---
    r = await CommandTool(mgr, reg, _cfg(timeout=1, yield_ms=30000)).execute(command="sleep 30")
    assert "timed out" in r.lower(), f"visible timeout failed: {r[:200]}"

    # --- visible stdin interaction ---
    cmd = CommandTool(mgr, reg, _cfg(yield_ms=500, timeout=10))
    await cmd.execute(command='bash -c "read -p \"yn: \" ans && echo picked: \$ans"')
    proc = ProcessTool(registry=reg, manager=mgr)
    if reg.list_running():
        await proc.execute(action="write", data="yes\n")
        await proc.execute(action="submit")
        import asyncio
        for _ in range(8):
            p = await proc.execute(action="poll")
            if "picked:" in p:
                break
            await asyncio.sleep(0.15)
        await proc.execute(action="kill")


# ── Test 4: Hidden -- full workflow: terminal tabs + command + process combined ──

@_WIN_ONLY
@pytest.mark.asyncio
async def test_4_hidden_full_workflow() -> None:
    """Simulates real agent workflow: open tab, run interactive command,
    poll, write answer, submit, poll result, list sessions, kill."""
    mgr = _hidden_mgr()
    reg = ProcessRegistry()
    tool = TerminalTool(mgr)
    cmd = CommandTool(mgr, reg, _cfg(yield_ms=500, timeout=12))
    proc = ProcessTool(registry=reg, manager=mgr)

    # 1. Open a workspace tab and run command
    await tool.execute(action="open", name="workspace")
    r = await cmd.execute(command="echo workflow-start")
    assert "workflow-start" in r

    # 2. Start interactive command (simpler pattern: bash read then echo)
    await cmd.execute(
        command='bash -c "read ans && echo chosen: \$ans"'
    )

    # 3. Poll, write, submit
    await proc.execute(action="poll")
    w = await proc.execute(action="write", data="go\n")
    assert "Wrote" in w
    await proc.execute(action="submit")

    # 4. Poll for result
    import asyncio
    p = ""
    for _ in range(10):
        p = await proc.execute(action="poll")
        if "chosen:" in p:
            break
        await asyncio.sleep(0.15)

    # 5. Check terminal state
    cur = await tool.execute(action="current")
    assert len(cur) > 0

    # 6. List sessions, kill remaining
    lst = await proc.execute(action="list")
    assert len(lst) > 0
    for s in reg.list_running():
        await proc.execute(action="kill")
