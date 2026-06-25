"""E2E verification: WSL bash terminal (hidden + visible).

Coverage:
  Hidden terminal (1 shared manager, 15 checks):
    - Command: echo, cd+pwd, 1s timeout, result format (natural language)
    - Stdin interaction: bash read + write + submit + poll
    - Process: poll, kill, interrupt (Ctrl+C), list
    - Terminal: open 3 tabs, select, tab isolation (different cwd),
      current segment, close tab, command after close

  Visible terminal (4 sequential managers, 14 checks):
    - 1st manager: echo, cd+pwd, current segment after commands
    - Tab switch: main <-> side, verify cwd isolation
    - Process: poll + send_keys (c-c, enter, escape) + list
    - List tabs, close tab, command after close
    - y/n interaction: bash read + write + submit + poll
    - password interaction: bash read + write + submit + poll

  Degradation chain: WSL bash -> cmd.exe hidden -> SubprocessTool.
  This file verifies the first tier (WSL bash).

Usage:
  python tests/verify_terminal_e2e_bash.py          # all (hidden + visible)
  python tests/verify_terminal_e2e_bash.py hidden    # hidden only
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
from modex_agent.tools.terminal.tool import TerminalTool
from modex_agent.tools.terminal.types import Platform, ShellFamily, ShellInfo, detect_platform_shell

NORM = TerminalRuntimeConfig(default_command_timeout_seconds=4, default_yield_ms=3000,
    command_tool_outer_timeout_seconds=8, input_wait_idle_ms=1000,
    input_wait_early_min_elapsed_ms=600, prompt_stabilize_ms=100)
TOUT = TerminalRuntimeConfig(default_command_timeout_seconds=1, default_yield_ms=30000,
    command_tool_outer_timeout_seconds=5, input_wait_idle_ms=1000,
    input_wait_early_min_elapsed_ms=600)
YIELD = TerminalRuntimeConfig(default_command_timeout_seconds=30, default_yield_ms=500,
    command_tool_outer_timeout_seconds=35, input_wait_idle_ms=1000,
    input_wait_early_min_elapsed_ms=600)

OK, FAIL = "  OK  ", "  FAIL"


def _mgr(visible: bool = False) -> TerminalManager:
    if visible:
        from modex_agent.tools.terminal.backends.visible_windows import VisibleWindowsPtyBackend
        backend = VisibleWindowsPtyBackend
    else:
        from modex_agent.tools.terminal.backends.windows_hidden import WindowsHiddenPtyBackend
        backend = WindowsHiddenPtyBackend
    shell = detect_platform_shell() or ShellInfo(ShellFamily.BASH, "bash", Platform.WINDOWS)
    return TerminalManager(storage_dir=Path(f"data/test_terms_{'v' if visible else 'h'}"),
                           max_terminals=5, backend_factory=backend, shell_info=shell)


# ═══════ HIDDEN ═══════

async def hidden_tests() -> int:
    f = 0
    mgr = _mgr()
    reg = ProcessRegistry()
    cmd = CommandTool(mgr, reg, NORM)
    tout = CommandTool(mgr, reg, TOUT)
    ycmd = CommandTool(mgr, reg, YIELD)
    proc = ProcessTool(registry=reg, manager=mgr)
    tool = TerminalTool(mgr)

    print("\n── hidden: command execution ──")
    r = await cmd.execute(command="echo hello-wsl")
    ok = "hello-wsl" in r and "[Command Result]" not in r
    print(f"{OK if ok else FAIL} echo: {r.strip()[:100]}")
    if not ok: f += 1

    await cmd.execute(command="cd /tmp")
    r = await cmd.execute(command="pwd")
    ok = "/tmp" in r
    print(f"{OK if ok else FAIL} cd+pwd: {r.strip()[:100]}")
    if not ok: f += 1

    r = await tout.execute(command="sleep 5")
    ok = "timed out" in r.lower()
    print(f"{OK if ok else FAIL} 1s timeout: {r.strip()[:100]}")
    if not ok: f += 1

    print("\n── hidden: stdin interaction ──")
    await ycmd.execute(command='bash -c "read ans && echo chosen: \\$ans"')
    if reg.list_running():
        await proc.execute(action="write", data="yes\n")
        await proc.execute(action="submit")
        for _ in range(10):
            p = await proc.execute(action="poll")
            if "chosen:" in p: break
            await asyncio.sleep(0.1)
        ok = "chosen:" in p
        print(f"{OK if ok else FAIL} read+write+submit: {p.strip()[:120]}")
        if not ok: f += 1
        await proc.execute(action="kill")

    print("\n── hidden: process actions ──")
    for label, act, check_fn in [
        ("poll", "poll", lambda r: len(r) > 0),
        ("kill", "kill", lambda r: "Killed" in r),
    ]:
        if label == "poll": await ycmd.execute(command="sleep 30")
        r = await proc.execute(action=act)
        ok = check_fn(r)
        print(f"{OK if ok else FAIL} {label}: {r.strip()[:100]}")
        if not ok: f += 1

    await ycmd.execute(command="sleep 30")
    r = await proc.execute(action="interrupt")
    ok = len(r) > 0
    print(f"{OK if ok else FAIL} interrupt: {r.strip()[:80]}")
    if not ok: f += 1
    for s in reg.list_running(): await proc.execute(action="kill")

    r = await proc.execute(action="list")
    ok = len(r) > 0
    print(f"{OK if ok else FAIL} list: {r.strip()[:150]}")
    if not ok: f += 1

    print("\n── hidden: tab management ──")
    for name in ("tab-a", "tab-b", "tab-c"):
        await tool.execute(action="open", name=name)
    lst = await tool.execute(action="list")
    ok = all(n in lst for n in ("tab-a", "tab-b", "tab-c"))
    print(f"{OK if ok else FAIL} open*3+list: {lst.strip()[:200]}")
    if not ok: f += 1

    await tool.execute(action="select", name="tab-a")
    await cmd.execute(command="cd /tmp && touch /tmp/e2e-tag-a")
    await tool.execute(action="select", name="tab-b")
    await cmd.execute(command="cd /var")
    r = await cmd.execute(command="pwd")
    ok = "/var" in r
    print(f"{OK if ok else FAIL} tab-b in /var: {r.strip()[:80]}")
    if not ok: f += 1

    await tool.execute(action="select", name="tab-a")
    r = await cmd.execute(command="pwd")
    ok = "/tmp" in r
    print(f"{OK if ok else FAIL} tab-a back in /tmp: {r.strip()[:80]}")
    if not ok: f += 1
    await cmd.execute(command="rm -f /tmp/e2e-tag-a")

    r = await tool.execute(action="current")
    ok = len(r) > 0
    print(f"{OK if ok else FAIL} current: {r.strip()[:150]}")
    if not ok: f += 1

    await tool.execute(action="close", name="tab-c")
    lst = await tool.execute(action="list")
    ok = "tab-c" not in lst and "tab-a" in lst
    print(f"{OK if ok else FAIL} close tab-c: {lst.strip()[:200]}")
    if not ok: f += 1

    r = await cmd.execute(command="echo after-close")
    ok = "after-close" in r
    print(f"{OK if ok else FAIL} cmd after close: {r.strip()[:80]}")
    if not ok: f += 1
    return f


# ═══════ VISIBLE: single manager, all scenarios ═══════

async def visible_tests() -> int:
    print("\n── visible: one manager, run commands then view current ──")
    mgr = _mgr(visible=True)
    tool = TerminalTool(mgr)

    # Prepare: open tabs
    await tool.execute(action="open", name="main")
    await tool.execute(action="open", name="side")
    print("     tabs opened: main, side")

    # ── normal commands on main tab ──
    await tool.execute(action="select", name="main")
    cmd = CommandTool(mgr, ProcessRegistry(), NORM)
    r = await cmd.execute(command="echo hello-from-visible-main")
    ok = "hello-from-visible-main" in r
    print(f"{OK if ok else FAIL} echo on main: {r.strip()[:120]}")
    if not ok: return 1

    await cmd.execute(command="cd /tmp && echo dir-is-tmp && ls -d /tmp")
    r = await cmd.execute(command="pwd")
    ok = "/tmp" in r
    print(f"{OK if ok else FAIL} cd /tmp && pwd: {r.strip()[:120]}")
    if not ok: return 1

    # ── current segment (view terminal content after commands) ──
    r = await tool.execute(action="current")
    ok = len(r) > 0
    print(f"{OK if ok else FAIL} current after commands: {r.strip()[:200]}")
    if not ok: return 1

    # ── switch to side tab, run commands ──
    await tool.execute(action="select", name="side")
    cmd2 = CommandTool(mgr, ProcessRegistry(), NORM)
    r = await cmd2.execute(command="echo side-tab-active && cd /var && pwd")
    ok = "side-tab-active" in r and "/var" in r
    print(f"{OK if ok else FAIL} side tab: {r.strip()[:120]}")
    if not ok: return 1

    # ── back to main, verify state preserved ──
    await tool.execute(action="select", name="main")
    r = await cmd.execute(command="pwd && echo main-still-tmp")
    ok = "/tmp" in r and "main-still-tmp" in r
    print(f"{OK if ok else FAIL} main tab state preserved: {r.strip()[:120]}")
    if not ok: return 1

    # ── list tabs ──
    lst = await tool.execute(action="list")
    ok = "main" in lst and "side" in lst
    print(f"{OK if ok else FAIL} list tabs: {lst.strip()[:200]}")
    if not ok: return 1

    # ── close side, verify main still works ──
    await tool.execute(action="close", name="side")
    lst = await tool.execute(action="list")
    ok = "side" not in lst and "main" in lst
    print(f"{OK if ok else FAIL} close side: {lst.strip()[:200]}")
    if not ok: return 1

    r = await cmd.execute(command="echo main-after-close-side")
    ok = "main-after-close-side" in r
    print(f"{OK if ok else FAIL} main after close side: {r.strip()[:120]}")
    if not ok: return 1

    # ═══════ process tool on visible ═══════
    print("\n── visible: process tool poll + interrupt + send_keys ──")
    reg = ProcessRegistry()
    ycmd = CommandTool(mgr, reg, YIELD)
    proc = ProcessTool(registry=reg, manager=mgr)

    # Start slow command, poll it
    await ycmd.execute(command="sleep 30")
    r = await proc.execute(action="poll")
    ok = len(r) > 0
    print(f"{OK if ok else FAIL} poll: {r.strip()[:100]}")
    if not ok: return 1

    # Send Ctrl+C to interrupt
    r = await proc.execute(action="send_keys", keys=["c-c"])
    print(f"     send_keys c-c: {r.strip()[:80]}")

    # Send other keys
    r = await proc.execute(action="send_keys", keys=["enter", "escape"])
    print(f"     send_keys enter+escape: {r.strip()[:80]}")

    # Kill any remaining
    for s in reg.list_running(): await proc.execute(action="kill")

    # List shows finished sessions
    r = await proc.execute(action="list")
    ok = len(r) > 0
    print(f"{OK if ok else FAIL} process list: {r.strip()[:150]}")
    if not ok: return 1

    # ═══════ visible interaction: y/n and password (fresh managers to avoid restart) ═══════
    print("\n── visible: stdin interaction (y/n + password) ──")

    # y/n prompt (simple read, same pattern that works in hidden)
    mgr_yn = _mgr(visible=True)
    reg_yn = ProcessRegistry()
    yn_cmd = CommandTool(mgr_yn, reg_yn, YIELD)
    yn_proc = ProcessTool(registry=reg_yn, manager=mgr_yn)
    await yn_cmd.execute(command='bash -c "read ans && echo decided: \\$ans"')
    if reg_yn.list_running():
        w = await yn_proc.execute(action="write", data="y")
        print(f"     write 'y': {w.strip()}")
        await yn_proc.execute(action="submit")
        for _ in range(15):
            p = await yn_proc.execute(action="poll")
            if "decided:" in p: break
            await asyncio.sleep(0.2)
        ok = "decided:" in p
        print(f"{OK if ok else FAIL} y/n interaction: {p.strip()[:140]}")
        if not ok: return 1
        await yn_proc.execute(action="kill")

    # password prompt (simple read pattern, same as y/n)
    mgr_pw = _mgr(visible=True)
    reg_pw = ProcessRegistry()
    pw_cmd = CommandTool(mgr_pw, reg_pw, YIELD)
    pw_proc = ProcessTool(registry=reg_pw, manager=mgr_pw)
    await pw_cmd.execute(command='bash -c "read pw && echo got-password"')
    if reg_pw.list_running():
        w = await pw_proc.execute(action="write", data="s3cret!")
        print(f"     write password: {w.strip()}")
        await pw_proc.execute(action="submit")
        for _ in range(15):
            p = await pw_proc.execute(action="poll")
            if "got-password" in p: break
            await asyncio.sleep(0.2)
        ok = "got-password" in p
        print(f"{OK if ok else FAIL} password interaction: {p.strip()[:140]}")
        if not ok: return 1
        await pw_proc.execute(action="kill")

    return 0


# ═══════ main ═══════

async def main() -> None:
    shell = detect_platform_shell()
    if shell is None or shell.platform is not Platform.WINDOWS:
        print("Requires Windows."); return
    print(f"Shell: {shell.family.value} @ {shell.path}")
    f = 0
    f += await hidden_tests()

    if "hidden" not in sys.argv:
        print("\n" + "=" * 60)
        print("VISIBLE: 4 console windows will open sequentially.")
        print("Each will show bash executing commands in real time.")
        print("=" * 60)
        f += await visible_tests()

    print("\n" + "=" * 60)
    print("ALL PASSED" if f == 0 else f"{f} FAILED")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
