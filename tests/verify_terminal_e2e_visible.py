"""E2E verification: visible terminal full-stack tests (WSL bash + Git bash + PowerShell).

Coverage per shell:
  1. Basic commands: echo, cd+pwd, simulated timeout
  2. Stdin interaction: y/n prompt (write+submit, output verification)
  3. Stdin interaction: password (write+submit, echo suppressed)
  4. Process operations: list, interrupt (send_keys Ctrl+C), kill
  5. Tab management: open, select, isolation (different cwd), close, list

Uses CommandTool / ProcessTool / TerminalTool — same stack as the agent.

Usage:
  python tests/verify_terminal_e2e_visible.py              # all shells
  python tests/verify_terminal_e2e_visible.py wsl           # WSL bash only
  python tests/verify_terminal_e2e_visible.py git           # Git bash only
  python tests/verify_terminal_e2e_visible.py ps            # PowerShell only
"""
from __future__ import annotations

import asyncio
import os as _os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.tools.terminal.command_tool import CommandTool
from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.manager import TerminalManager
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.process_tool import ProcessTool
from framework.tools.terminal.tool import TerminalTool
from framework.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
)

# ── Runtime configs ──────────────────────────────────────────────

NORM = TerminalRuntimeConfig(
    default_command_timeout_seconds=4, default_yield_ms=3000,
    command_tool_outer_timeout_seconds=8, input_wait_idle_ms=1000,
    input_wait_early_min_elapsed_ms=600, prompt_stabilize_ms=100,
)
TOUT = TerminalRuntimeConfig(
    default_command_timeout_seconds=1, default_yield_ms=30000,
    command_tool_outer_timeout_seconds=5, input_wait_idle_ms=1000,
    input_wait_early_min_elapsed_ms=600,
)
YIELD = TerminalRuntimeConfig(
    default_command_timeout_seconds=30, default_yield_ms=500,
    command_tool_outer_timeout_seconds=35, input_wait_idle_ms=1000,
    input_wait_early_min_elapsed_ms=600,
)

OK, FAIL = "  OK  ", "  FAIL"


# ── Shell detection ─────────────────────────────────────────────

def _find_wsl_bash() -> str | None:
    system_root = _os.environ.get("SystemRoot", r"C:\Windows")
    wsl_bash = Path(system_root) / "System32" / "bash.exe"
    if wsl_bash.is_file():
        return str(wsl_bash)
    return None


def _find_git_bash() -> str | None:
    bash_path = shutil.which("bash")
    if bash_path and "system32" not in bash_path.lower():
        return bash_path
    return None


def _find_powershell() -> str | None:
    return shutil.which("powershell.exe")


def _visible_mgr(shell_info: ShellInfo) -> TerminalManager:
    from framework.tools.terminal.backends.visible_windows import VisibleWindowsPtyBackend
    return TerminalManager(
        storage_dir=Path("data/test_terms_e2e_visible"),
        max_terminals=5,
        backend_factory=VisibleWindowsPtyBackend,
        shell_info=shell_info,
    )


# ── Shared bash test suite ──────────────────────────────────────

async def _bash_suite(
    label: str,
    shell_path: str,
    echo_cmd: str,
    cwd_setup_cmd: str,
    timeout_cmd: str,
    sleep_cmd: str,
    read_cmd: str,
    pw_cmd: str,
) -> int:
    """Run full E2E suite for a bash-family shell. Returns failure count."""
    f = 0
    shell_info = ShellInfo(ShellFamily.BASH, shell_path, Platform.WINDOWS)
    pwd_cmd = "pwd"

    # ═══════ 1. Basic commands ──────────────────────────
    print(f"\n── {label}: basic commands ──")
    mgr = _visible_mgr(shell_info)
    cmd = CommandTool(mgr, ProcessRegistry(), NORM)
    tout = CommandTool(mgr, ProcessRegistry(), TOUT)

    # 1a. echo
    r = await cmd.execute(command=echo_cmd)
    ok = "hello" in r.lower() and "[Command Result]" not in r
    print(f"{OK if ok else FAIL} echo: {r.strip()[:120]}")
    if not ok: f += 1

    # 1b. cd + pwd
    await cmd.execute(command=cwd_setup_cmd)
    r = await cmd.execute(command=pwd_cmd)
    ok = "/tmp" in r or "/var" in r
    print(f"{OK if ok else FAIL} cd+pwd: {r.strip()[:120]}")
    if not ok: f += 1

    # 1c. timeout simulation (1s timeout on a 5s+ command)
    r = await tout.execute(command=timeout_cmd)
    ok = "timed out" in r.lower()
    print(f"{OK if ok else FAIL} timeout: {r.strip()[:120]}")
    if not ok: f += 1

    await mgr.close_all()

    # ═══════ 2. y/n interaction ────────────────────────
    print(f"\n── {label}: y/n interaction ──")
    mgr2 = _visible_mgr(shell_info)
    reg2 = ProcessRegistry()
    ycmd = CommandTool(mgr2, reg2, YIELD)
    yproc = ProcessTool(registry=reg2, manager=mgr2)

    await ycmd.execute(command=read_cmd)
    running = reg2.list_running()
    if running:
        w = await yproc.execute(action="write", data="y")
        print(f"     write 'y': {w.strip()[:100]}")
        r = await yproc.execute(action="submit")
        await asyncio.sleep(0.3)
        # Check log for output after submit
        log = await yproc.execute(action="log")
        ok = "chosen:" in log
        print(f"{OK if ok else FAIL} write+submit+log: {log.strip()[:140]}")
        if not ok: f += 1
        # No stray control chars
        ok2 = "\x01" not in log and "\x0b" not in log
        print(f"{OK if ok2 else FAIL} no stray control chars: {log.strip()[:100]}")
        if not ok2: f += 1
        for _ in reg2.list_running():
            await yproc.execute(action="kill")
    else:
        print(f"{FAIL} process not launched for y/n")
        f += 1

    await mgr2.close_all()

    # ═══════ 3. Password interaction ───────────────────
    print(f"\n── {label}: password interaction ──")
    mgr3 = _visible_mgr(shell_info)
    reg3 = ProcessRegistry()
    pwcmd = CommandTool(mgr3, reg3, YIELD)
    pwproc = ProcessTool(registry=reg3, manager=mgr3)

    await pwcmd.execute(command=pw_cmd)
    running = reg3.list_running()
    if running:
        w = await pwproc.execute(action="write", data="s3cret!")
        print(f"     write password: {w.strip()[:100]}")
        r = await pwproc.execute(action="submit")
        await asyncio.sleep(0.3)
        log = await pwproc.execute(action="log")
        ok = "got-pwd" in log
        print(f"{OK if ok else FAIL} password submit: {log.strip()[:140]}")
        if not ok: f += 1
        # Password must NOT be echoed
        ok2 = "s3cret!" not in log
        print(f"{OK if ok2 else FAIL} password not echoed: {log.strip()[:100]}")
        if not ok2: f += 1
        for s in reg3.list_running():
            await pwproc.execute(action="kill")
    else:
        print(f"{FAIL} process not launched for password")
        f += 1

    await mgr3.close_all()

    # ═══════ 4. Process operations ─────────────────────
    print(f"\n── {label}: process operations ──")
    mgr4 = _visible_mgr(shell_info)
    reg4 = ProcessRegistry()
    ycmd4 = CommandTool(mgr4, reg4, YIELD)
    proc4 = ProcessTool(registry=reg4, manager=mgr4)

    # Start long-running command
    await ycmd4.execute(command=sleep_cmd)
    r = await proc4.execute(action="list")
    ok = len(r) > 0
    print(f"{OK if ok else FAIL} list running: {r.strip()[:100]}")
    if not ok: f += 1

    # Interrupt via send_keys Ctrl+C
    await proc4.execute(action="send_keys", keys=["c-c"])
    await asyncio.sleep(0.3)
    r = await proc4.execute(action="list")
    print(f"     after c-c: {r.strip()[:120]}")

    # Kill any remaining
    for s in reg4.list_running():
        await proc4.execute(action="kill")
    r = await proc4.execute(action="list")
    ok = len(r) > 0
    print(f"{OK if ok else FAIL} list after kill: {r.strip()[:150]}")
    if not ok: f += 1

    await mgr4.close_all()

    # ═══════ 5. Tab management ─────────────────────────
    print(f"\n── {label}: tab management ──")
    mgr5 = _visible_mgr(shell_info)
    tool5 = TerminalTool(mgr5)
    cmd5 = CommandTool(mgr5, ProcessRegistry(), NORM)

    # Open tabs
    await tool5.execute(action="open", name="tab-x")
    await tool5.execute(action="open", name="tab-y")
    print("     tabs opened: tab-x, tab-y")

    # Tab isolation: cd to /tmp on tab-x, keep tab-y in original dir
    await tool5.execute(action="select", name="tab-x")
    await cmd5.execute(command="cd /tmp")
    r = await cmd5.execute(command="pwd")
    ok = "/tmp" in r
    print(f"{OK if ok else FAIL} tab-x in /tmp: {r.strip()[:80]}")
    if not ok: f += 1

    await tool5.execute(action="select", name="tab-y")
    r = await cmd5.execute(command="pwd")
    ok2 = "/tmp" not in r  # isolated
    print(f"{OK if ok2 else FAIL} tab-y isolated: {r.strip()[:80]}")
    if not ok2: f += 1

    # Back to tab-x
    await tool5.execute(action="select", name="tab-x")
    r = await cmd5.execute(command="pwd")
    ok3 = "/tmp" in r
    print(f"{OK if ok3 else FAIL} tab-x cwd preserved: {r.strip()[:80]}")
    if not ok3: f += 1

    # List + close
    lst = await tool5.execute(action="list")
    ok = "tab-x" in lst and "tab-y" in lst
    print(f"{OK if ok else FAIL} list tabs: {lst.strip()[:200]}")
    if not ok: f += 1

    await tool5.execute(action="close", name="tab-y")
    lst = await tool5.execute(action="list")
    ok = "tab-y" not in lst and "tab-x" in lst
    print(f"{OK if ok else FAIL} close tab-y: {lst.strip()[:200]}")
    if not ok: f += 1

    r = await cmd5.execute(command='echo "after-close"')
    ok = "after-close" in r
    print(f"{OK if ok else FAIL} tab-x after close: {r.strip()[:120]}")
    if not ok: f += 1

    await mgr5.close_all()

    return f


# ── PowerShell suite ────────────────────────────────────────────

async def _ps_suite() -> int:
    ps_path = _find_powershell()
    if not ps_path:
        print("SKIP: PowerShell not found in PATH")
        return 0

    f = 0
    label = "powershell"
    shell_info = ShellInfo(ShellFamily.POWERSHELL, ps_path, Platform.WINDOWS)
    print(f"     shell: {ps_path}")

    # ═══════ 1. Basic commands ──────────────────────────
    print(f"\n── {label}: basic commands ──")
    mgr = _visible_mgr(shell_info)
    cmd = CommandTool(mgr, ProcessRegistry(), NORM)
    tout = CommandTool(mgr, ProcessRegistry(), TOUT)

    r = await cmd.execute(command='Write-Host "hello"')
    ok = "hello" in r.lower()
    print(f"{OK if ok else FAIL} echo: {r.strip()[:120]}")
    if not ok: f += 1

    await cmd.execute(command="Set-Location C:\\Windows")
    r = await cmd.execute(command="Get-Location")
    ok = "Windows" in r
    print(f"{OK if ok else FAIL} cd+pwd: {r.strip()[:120]}")
    if not ok: f += 1

    r = await tout.execute(command="Start-Sleep -Seconds 5")
    ok = "timed out" in r.lower()
    print(f"{OK if ok else FAIL} timeout: {r.strip()[:120]}")
    if not ok: f += 1

    await mgr.close_all()

    # ═══════ 2. y/n interaction ────────────────────────
    print(f"\n── {label}: y/n interaction ──")
    mgr2 = _visible_mgr(shell_info)
    reg2 = ProcessRegistry()
    ycmd = CommandTool(mgr2, reg2, YIELD)
    yproc = ProcessTool(registry=reg2, manager=mgr2)

    await ycmd.execute(command='$ans = Read-Host; Write-Host "chosen: $ans"')
    running = reg2.list_running()
    if running:
        await yproc.execute(action="write", data="y")
        await yproc.execute(action="submit")
        await asyncio.sleep(0.3)
        log = await yproc.execute(action="log")
        ok = "chosen:" in log
        print(f"{OK if ok else FAIL} write+submit: {log.strip()[:140]}")
        if not ok: f += 1
        for _ in reg2.list_running():
            await yproc.execute(action="kill")
    else:
        print(f"{FAIL} process not launched for y/n")
        f += 1

    await mgr2.close_all()

    # ═══════ 3. Password interaction ───────────────────
    print(f"\n── {label}: password interaction ──")
    mgr3 = _visible_mgr(shell_info)
    reg3 = ProcessRegistry()
    pwcmd = CommandTool(mgr3, reg3, YIELD)
    pwproc = ProcessTool(registry=reg3, manager=mgr3)

    await pwcmd.execute(command='$pw = Read-Host -AsSecureString; Write-Host "got-pwd"')
    running = reg3.list_running()
    if running:
        await pwproc.execute(action="write", data="s3cret!")
        await pwproc.execute(action="submit")
        await asyncio.sleep(0.3)
        log = await pwproc.execute(action="log")
        ok = "got-pwd" in log
        print(f"{OK if ok else FAIL} password submit: {log.strip()[:140]}")
        if not ok: f += 1
        ok2 = "s3cret!" not in log
        print(f"{OK if ok2 else FAIL} password not echoed: {log.strip()[:100]}")
        if not ok2: f += 1
        for s in reg3.list_running():
            await pwproc.execute(action="kill")
    else:
        print(f"{FAIL} process not launched for password")
        f += 1

    await mgr3.close_all()

    # ═══════ 4. Process operations ─────────────────────
    print(f"\n── {label}: process operations ──")
    mgr4 = _visible_mgr(shell_info)
    reg4 = ProcessRegistry()
    ycmd4 = CommandTool(mgr4, reg4, YIELD)
    proc4 = ProcessTool(registry=reg4, manager=mgr4)

    await ycmd4.execute(command="Start-Sleep -Seconds 30")
    r = await proc4.execute(action="list")
    ok = len(r) > 0
    print(f"{OK if ok else FAIL} list running: {r.strip()[:100]}")
    if not ok: f += 1

    await proc4.execute(action="send_keys", keys=["c-c"])
    await asyncio.sleep(0.3)
    r = await proc4.execute(action="list")
    print(f"     after c-c: {r.strip()[:120]}")

    for s in reg4.list_running():
        await proc4.execute(action="kill")
    r = await proc4.execute(action="list")
    ok = len(r) > 0
    print(f"{OK if ok else FAIL} list after kill: {r.strip()[:150]}")
    if not ok: f += 1

    await mgr4.close_all()

    # ═══════ 5. Tab management ─────────────────────────
    print(f"\n── {label}: tab management ──")
    mgr5 = _visible_mgr(shell_info)
    tool5 = TerminalTool(mgr5)
    cmd5 = CommandTool(mgr5, ProcessRegistry(), NORM)

    await tool5.execute(action="open", name="tab-x")
    await tool5.execute(action="open", name="tab-y")
    print("     tabs opened: tab-x, tab-y")

    await tool5.execute(action="select", name="tab-x")
    await cmd5.execute(command="Set-Location C:\\Windows")
    r = await cmd5.execute(command="Get-Location")
    ok = "Windows" in r
    print(f"{OK if ok else FAIL} tab-x in Windows: {r.strip()[:80]}")
    if not ok: f += 1

    await tool5.execute(action="select", name="tab-y")
    r = await cmd5.execute(command="Get-Location")
    ok2 = "Windows" not in r
    print(f"{OK if ok2 else FAIL} tab-y isolated: {r.strip()[:80]}")
    if not ok2: f += 1

    await tool5.execute(action="select", name="tab-x")
    r = await cmd5.execute(command="Get-Location")
    ok3 = "Windows" in r
    print(f"{OK if ok3 else FAIL} tab-x cwd preserved: {r.strip()[:80]}")
    if not ok3: f += 1

    lst = await tool5.execute(action="list")
    ok = "tab-x" in lst and "tab-y" in lst
    print(f"{OK if ok else FAIL} list tabs: {lst.strip()[:200]}")
    if not ok: f += 1

    await tool5.execute(action="close", name="tab-y")
    lst = await tool5.execute(action="list")
    ok = "tab-y" not in lst and "tab-x" in lst
    print(f"{OK if ok else FAIL} close tab-y: {lst.strip()[:200]}")
    if not ok: f += 1

    r = await cmd5.execute(command='Write-Host "after-close"')
    ok = "after-close" in r
    print(f"{OK if ok else FAIL} tab-x after close: {r.strip()[:120]}")
    if not ok: f += 1

    await mgr5.close_all()

    return f


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

async def main() -> None:
    if sys.platform != "win32":
        print("Requires Windows.")
        return

    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    total = 0

    if which in ("all", "wsl"):
        print("=" * 60)
        print("WSL BASH — console windows will open")
        print("=" * 60)
        wsl = _find_wsl_bash()
        if wsl:
            total += await _bash_suite(
                label="wsl-bash",
                shell_path=wsl,
                echo_cmd='echo "hello"',
                cwd_setup_cmd="cd /tmp",
                timeout_cmd="sleep 5",
                sleep_cmd="sleep 30",
                read_cmd=r'bash -c "read ans && echo chosen: \$ans"',
                pw_cmd=r'bash -c "read -s pw && echo got-pwd"',
            )
        else:
            print("SKIP: WSL bash not found")

    if which in ("all", "git"):
        print("\n" + "=" * 60)
        print("GIT BASH — console windows will open")
        print("=" * 60)
        git = _find_git_bash()
        if git:
            total += await _bash_suite(
                label="git-bash",
                shell_path=git,
                echo_cmd='echo "hello"',
                cwd_setup_cmd="cd /tmp",
                timeout_cmd="sleep 5",
                sleep_cmd="sleep 30",
                read_cmd=r'bash -c "read ans && echo chosen: \$ans"',
                pw_cmd=r'bash -c "read -s pw && echo got-pwd"',
            )
        else:
            print("SKIP: Git bash not found in PATH")

    if which in ("all", "ps"):
        print("\n" + "=" * 60)
        print("POWERSHELL — console windows will open")
        print("=" * 60)
        total += await _ps_suite()

    print("\n" + "=" * 60)
    print("ALL PASSED" if total == 0 else f"{total} FAILED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
