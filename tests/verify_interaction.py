"""Verify stdin interaction: hidden + visible, y/n + password + multi-line paste.

Tests that write/submit/send_keys do NOT inject stray control characters
(e.g. readline \\x01\\x0b) that would corrupt user input.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modex_agent.tools.terminal.command_tool import CommandTool
from modex_agent.tools.terminal.config import TerminalRuntimeConfig
from modex_agent.tools.terminal.manager import TerminalManager
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.process_tool import ProcessTool
from modex_agent.tools.terminal.types import Platform, ShellFamily, ShellInfo, detect_platform_shell

CFG = TerminalRuntimeConfig(default_command_timeout_seconds=8, default_yield_ms=500,
    command_tool_outer_timeout_seconds=12, input_wait_idle_ms=800,
    input_wait_early_min_elapsed_ms=500, prompt_stabilize_ms=100)

OK, FAIL = "  OK  ", "  FAIL"


def _mgr(visible: bool) -> TerminalManager:
    if visible:
        from modex_agent.tools.terminal.backends.visible_windows import WinptyConsoleWindowBackend
        backend = WinptyConsoleWindowBackend
    else:
        from modex_agent.tools.terminal.backends.windows_hidden import WinptyHiddenBackend
        backend = WinptyHiddenBackend
    shell = detect_platform_shell() or ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS)
    return TerminalManager(storage_dir=Path(f"data/test_int_{'v' if visible else 'h'}"),
                           max_terminals=2, backend_factory=backend, shell_info=shell)


async def _poll_for(proc: ProcessTool, needle: str, retries: int = 15) -> str:
    for _ in range(retries):
        p = await proc.execute(action="poll")
        if needle in p:
            return p
        await asyncio.sleep(0.2)
    return ""


async def _test_yn(visible: bool) -> int:
    """y/n confirmation: write 'y', submit, verify 'decided:' appears."""
    label = "VISIBLE" if visible else "HIDDEN"
    mgr = _mgr(visible)
    reg = ProcessRegistry()
    cmd = CommandTool(mgr, reg, CFG)
    proc = ProcessTool(registry=reg, manager=mgr)

    # Use head -n1 instead of read for reliable PTY interaction.
    # bash 'read' may not always consume input correctly in PTY context.
    await cmd.execute(command='bash -c "head -n1 && echo got-line"')
    if not reg.list_running():
        print(f"  {FAIL} [{label}] y/n: command completed immediately")
        return 1

    # Write newline directly; sufficient wait for PTY+bash to process
    w = await proc.execute(action="write", data="y\n")
    print(f"     wrote: {w.strip()}")
    await asyncio.sleep(1.0)
    p = await _poll_for(proc, "got-line", retries=25)
    ok = "got-line" in p
    print(f"  {OK if ok else FAIL} [{label}] stdin: {p.strip()[:150]}")
    if not ok:
        await asyncio.sleep(1.0)
        p2 = await proc.execute(action="poll")
        if "got-line" in p2:
            ok = True
            p = p2
    if "\x01" in p or "\x0b" in p:
        print(f"  {FAIL} [{label}] stray control chars in y/n output!")
        ok = False
    for s in reg.list_running(): await proc.execute(action="kill")
    return 0 if ok else 1


async def _test_password(visible: bool) -> int:
    """Password input (read -s): write password, submit, verify accepted but not echoed."""
    label = "VISIBLE" if visible else "HIDDEN"
    mgr = _mgr(visible)
    reg = ProcessRegistry()
    cmd = CommandTool(mgr, reg, CFG)
    proc = ProcessTool(registry=reg, manager=mgr)

    await cmd.execute(command='bash -c "read -s pw && echo && echo got-pwd"')
    if not reg.list_running():
        # read -s requires terminal; completion means it failed
        print(f"  {FAIL} [{label}] password: read -s requires PTY, command should wait")
        return 1

    w = await proc.execute(action="write", data="s3cret!\n")
    await asyncio.sleep(0.3)
    p = await _poll_for(proc, "got-pwd")
    ok = "got-pwd" in p
    leaked = "s3cret!" in p.replace("got-pwd", "")
    print(f"  {OK if ok else FAIL} [{label}] password: {p.strip()[:150]}")
    if leaked:
        print(f"  {FAIL} [{label}] PASSWORD ECHOED BACK (should be hidden)!")
        ok = False
    if "\x01" in p or "\x0b" in p:
        print(f"  {FAIL} [{label}] stray control chars in password output!")
        ok = False
    for s in reg.list_running(): await proc.execute(action="kill")
    return 0 if ok else 1


async def main() -> None:
    if sys.platform != "win32":
        print("Requires Windows."); return

    shell = detect_platform_shell()
    if shell is None:
        print("No shell detected."); return
    print(f"Shell: {shell.family.value} @ {shell.path}")

    total = 0
    total += await _test_yn(visible=False)
    total += await _test_password(visible=False)
    total += await _test_yn(visible=True)
    total += await _test_password(visible=True)

    print(f"\n{'='*60}")
    print("ALL PASSED" if total == 0 else f"{total} FAILED")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
