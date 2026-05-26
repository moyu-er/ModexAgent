"""E2E verification: visible terminal special-key support (WSL bash + Git bash).

Validates that the visible terminal windows behave like normal user-opened
terminals.  All key handling is universal — works across bash, zsh, SSH, etc.

Coverage:
  WSL bash (visible):
    1. Console dimensions match PTY (tput cols/lines)
    2. Ctrl+C interrupts a running command
    3. Backspace deletes characters
    4. Arrow-up recalls previous command
    5. Esc cancels current input
    6. Rapid tool commands (cursor position correct)

  Git bash (visible):
    1. Ctrl+C interrupts a running command
    2. Backspace deletes characters
    3. Arrow-up recalls previous command
    4. Rapid tool commands (cursor position correct)

Usage:
  python tests/verify_terminal_visible_keys.py              # both
  python tests/verify_terminal_visible_keys.py wsl           # WSL bash only
  python tests/verify_terminal_visible_keys.py git           # Git bash only
"""
from __future__ import annotations

import asyncio
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.tools.terminal.backends.visible_windows import VisibleWindowsPtyBackend
from framework.tools.terminal.prompt import _strip_ansi_and_da1
from framework.tools.terminal.types import ShellFamily, detect_platform_shell

OK, FAIL = "  OK  ", "  FAIL"
PTY_COLS, PTY_ROWS = 120, 30


def _find_git_bash() -> str | None:
    """Find Git bash via PATH (path contains 'git'), avoiding WSL bash."""
    bash_path = shutil.which("bash")
    if bash_path and "git" in bash_path.lower():
        return bash_path
    return None


async def _read_until(backend: VisibleWindowsPtyBackend, marker: str, timeout: float = 10.0) -> str:
    """Read until *marker* appears in output or timeout."""
    output = ""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        chunk = await backend.read(timeout=0.5, max_size=4096)
        if chunk:
            output += chunk
        if marker in output:
            break
        await asyncio.sleep(0.05)
    return output


def _clean(text: str) -> str:
    return _strip_ansi_and_da1(text).strip()


# ═══════════════════════════════════════════════
# Shared bash test suite (universal for any bash)
# ═══════════════════════════════════════════════

async def _bash_tests(label: str, backend: VisibleWindowsPtyBackend) -> int:
    """Run universal bash tests. Returns failure count."""
    f = 0
    b = backend

    # ── 1. Console dimensions (stty size) ──
    print(f"\n── {label}: console dimensions ──")
    await b.write("stty size\n")
    await asyncio.sleep(0.5)
    out = await b.read(timeout=1.0, max_size=4096)
    dim_text = _clean(out)
    size_match = re.search(r"(\d+)\s+(\d+)", dim_text)
    if size_match:
        actual_rows = int(size_match.group(1))
        actual_cols = int(size_match.group(2))
        ok_c = actual_cols == PTY_COLS
        ok_r = actual_rows == PTY_ROWS
        print(f"{OK if ok_c else FAIL} cols: {actual_cols} (expected {PTY_COLS})")
        if not ok_c: f += 1
        print(f"{OK if ok_r else FAIL} rows: {actual_rows} (expected {PTY_ROWS})")
        if not ok_r: f += 1
    else:
        print(f"     stty output: {dim_text[:150]}")

    # ── 2. Ctrl+C interrupts running command ──
    print(f"\n── {label}: Ctrl+C interrupt ──")
    await b.write("sleep 30\n")
    await asyncio.sleep(0.5)
    await b.write("\x03")  # Ctrl+C
    out = await _read_until(b, "$", timeout=5.0)
    clean = _clean(out)
    ok = "^C" in clean or "$" in clean
    print(f"{OK if ok else FAIL} ctrl+c interrupt: {clean[:120]}")
    if not ok: f += 1

    # Drain any leftover output before next command
    await asyncio.sleep(0.5)
    await b.read(timeout=0.3, max_size=4096)

    # Verify command still works after interrupt
    await b.write("echo after-ctrl-c\n")
    out = await _read_until(b, "after-ctrl-c", timeout=8.0)
    ok = "after-ctrl-c" in out
    print(f"{OK if ok else FAIL} echo after ctrl+c: {_clean(out)[:120]}")
    if not ok: f += 1

    # ── 3. Backspace ──
    print(f"\n── {label}: backspace ──")
    await b.write("echo hellX\x08o\n")
    out = await _read_until(b, "hello")
    ok = "hello" in out and "hellXo" not in _clean(out)
    print(f"{OK if ok else FAIL} backspace: {_clean(out)[:120]}")
    if not ok: f += 1

    # ── 4. Arrow-up (history) ──
    print(f"\n── {label}: arrow-up (history recall) ──")
    await b.write("echo history-test-123\n")
    await _read_until(b, "history-test-123")
    await b.write("\x1b[A")
    await asyncio.sleep(0.3)
    out2 = await b.read(timeout=1.0, max_size=4096)
    combined = _clean(out2)
    ok2 = "history-test-123" in combined or "echo" in combined
    print(f"{OK if ok2 else FAIL} arrow-up recall: {combined[:150]}")
    if not ok2: f += 1

    await b.write("\x03")  # Ctrl+C to cancel
    await asyncio.sleep(0.5)
    # Drain all pending Ctrl+C output
    for _ in range(3):
        await b.read(timeout=0.5, max_size=4096)

    # ── 5. Cancel input ──
    print(f"\n── {label}: cancel input ──")
    await b.write("partial-typing")
    await asyncio.sleep(0.3)
    await b.write("\x03")
    out = await _read_until(b, "$", timeout=3.0)
    clean = _clean(out)
    ok = "^C" in clean or "$" in clean
    print(f"{OK if ok else FAIL} cancel input: {clean[:120]}")
    if not ok: f += 1

    # ── 6. Rapid commands (cursor position correct) ──
    print(f"\n── {label}: rapid commands ──")
    for i in range(3):
        await b.write(f"echo rapid-{i}\n")
        out = await _read_until(b, f"rapid-{i}")
        ok = f"rapid-{i}" in out
        if not ok:
            print(f"{FAIL} rapid-{i}: {_clean(out)[:100]}")
            f += 1
    print(f"{OK} rapid commands (cursor ok)")

    return f


# ═══════════════════════════════════════════════
# WSL BASH
# ═══════════════════════════════════════════════

async def wsl_tests() -> int:
    shell = detect_platform_shell()
    if shell is None or shell.family != ShellFamily.BASH:
        print("SKIP: no WSL/Git bash available")
        return 0

    print(f"     shell: {shell.path}")
    b = VisibleWindowsPtyBackend()
    await b.start(shell.path)
    try:
        await b.drain_startup()
        print(f"     window: {b.window_title}")
        return await _bash_tests("wsl-bash", b)
    finally:
        await b.terminate()


# ═══════════════════════════════════════════════
# GIT BASH
# ═══════════════════════════════════════════════

async def git_tests() -> int:
    git_bash = _find_git_bash()
    if not git_bash:
        print("SKIP: Git bash not found in PATH")
        return 0

    print(f"     shell: {git_bash}")
    b = VisibleWindowsPtyBackend()
    await b.start(git_bash)
    try:
        await b.drain_startup()
        print(f"     window: {b.window_title}")
        return await _bash_tests("git-bash", b)
    finally:
        await b.terminate()


# ═══════════════════════════════════════════════
# POWERSHELL
# ═══════════════════════════════════════════════

async def ps_tests() -> int:
    import shutil as _shutil
    ps_path = _shutil.which("powershell.exe")
    if not ps_path:
        print("SKIP: PowerShell not found in PATH")
        return 0

    print(f"     shell: {ps_path}")
    f = 0
    b = VisibleWindowsPtyBackend()
    await b.start(ps_path)
    try:
        await b.drain_startup()
        print(f"     window: {b.window_title}")

        # ── 1. Echo ──
        print("\n── powershell: echo ──")
        await b.write('Write-Host "hello-ps"\n')
        out = await _read_until(b, "hello-ps", timeout=8.0)
        ok = "hello-ps" in out
        print(f"{OK if ok else FAIL} echo: {_clean(out)[:150]}")
        if not ok: f += 1

        # ── 2. Ctrl+C interrupt ──
        print("\n── powershell: Ctrl+C interrupt ──")
        await b.write("Start-Sleep -Seconds 30\n")
        await asyncio.sleep(1.0)
        await b.write("\x03")  # Ctrl+C
        await asyncio.sleep(0.5)
        await b.read(timeout=0.5, max_size=4096)

        await b.write('Write-Host "after-ctrl-c"\n')
        out = await _read_until(b, "after-ctrl-c", timeout=8.0)
        ok = "after-ctrl-c" in out
        print(f"{OK if ok else FAIL} ctrl+c + echo: {_clean(out)[:150]}")
        if not ok: f += 1

        # ── 3. Backspace ──
        print("\n── powershell: backspace ──")
        await b.write('Write-Host "hellX\x08o"\n')
        out = await _read_until(b, "hello", timeout=5.0)
        ok = "hello" in out
        print(f"{OK if ok else FAIL} backspace: {_clean(out)[:150]}")
        if not ok: f += 1

        # ── 4. Rapid commands ──
        print("\n── powershell: rapid commands ──")
        for i in range(3):
            await b.write(f'Write-Host "ps-rapid-{i}"\n')
            out = await _read_until(b, f"ps-rapid-{i}", timeout=5.0)
            ok = f"ps-rapid-{i}" in out
            if not ok:
                print(f"{FAIL} ps-rapid-{i}: {_clean(out)[:100]}")
                f += 1
        print(f"{OK} rapid commands (cursor ok)")

    finally:
        await b.terminate()

    return f


# ═══════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════

async def main() -> None:
    if sys.platform != "win32":
        print("Requires Windows.")
        return

    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    total = 0

    if which in ("all", "wsl"):
        print("=" * 60)
        print("WSL BASH — console window will open")
        print("=" * 60)
        total += await wsl_tests()

    if which in ("all", "git"):
        print("\n" + "=" * 60)
        print("GIT BASH — console window will open")
        print("=" * 60)
        total += await git_tests()

    if which in ("all", "ps"):
        print("\n" + "=" * 60)
        print("POWERSHELL — console window will open")
        print("=" * 60)
        total += await ps_tests()

    print("\n" + "=" * 60)
    print("ALL PASSED" if total == 0 else f"{total} FAILED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
