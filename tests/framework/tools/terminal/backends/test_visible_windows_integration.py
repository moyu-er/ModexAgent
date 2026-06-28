"""Real WinptyConsoleWindowBackend (legacy alias: VisibleWindowsPtyBackend) integration tests.

Exercises the visible-terminal backend on Windows with WSL bash or Git bash.
"""

from __future__ import annotations

import asyncio
import re as _re
import shutil
import sys

import pytest


def _find_bash() -> str | None:
    """Return WSL bash (preferred) or Git bash path."""
    from pathlib import Path

    wsl = r"C:\Windows\System32\bash.exe"
    if Path(wsl).is_file():
        return wsl
    return shutil.which("bash")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
class TestVisibleWindowsPtyIntegration:
    """Visible terminal: start bash, run commands, verify output is clean."""

    @pytest.fixture
    async def backend(self):
        bash = _find_bash()
        if bash is None:
            pytest.skip("No bash (WSL or Git) available on this Windows machine")

        from modex_agent.tools.terminal.backends.visible_windows import (
            WinptyConsoleWindowBackend,
        )

        b = WinptyConsoleWindowBackend()
        await b.start(bash)
        await b.drain_startup()
        try:
            yield b
        finally:
            # Give the window a moment before terminating.
            await asyncio.sleep(0.3)
            await b.terminate()

    async def _read_until(self, backend, marker: str, timeout: float = 15.0) -> str:
        output = ""
        t0 = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - t0 < timeout:
            chunk = await backend.read(timeout=0.5, max_size=4096)
            if chunk:
                output += chunk
            if marker in output:
                return output
            await asyncio.sleep(0.1)
        return output

    @pytest.mark.asyncio
    async def test_echo_multiple_commands_no_da1_leak(self, backend) -> None:
        """Visible bash: sequential commands work, DA1 never leaks, stays alive."""
        assert await backend.is_alive()
        assert backend.window_title

        for i in range(3):
            await backend.write(f"echo vw-seq-{i}\n")
            out = await self._read_until(backend, f"vw-seq-{i}")
            assert f"vw-seq-{i}" in out
            assert await backend.is_alive()

        # DA1 must never leak into command output.
        da1 = _re.compile(r"\x1b\[\?[\d;]+c")
        assert not da1.search(out), f"DA1 leaked: {out[:400]!r}"

    @pytest.mark.asyncio
    async def test_session_submits_without_manual_enter(self) -> None:
        """TerminalSession submits commands autonomously — no manual Enter required."""
        bash = _find_bash()
        if bash is None:
            pytest.skip("No bash available")

        from modex_agent.tools.terminal.backends.visible_windows import (
            WinptyConsoleWindowBackend,
        )
        from modex_agent.tools.terminal.session import TerminalSession
        from modex_agent.tools.terminal.types import Platform, ShellFamily, ShellInfo

        backend = WinptyConsoleWindowBackend()
        session = TerminalSession(
            name="vw-session",
            backend=backend,
            shell_info=ShellInfo(ShellFamily.BASH, bash, Platform.WINDOWS),
        )
        try:
            output = await session.execute(
                "echo modex-vw-session", timeout=5.0
            )
        finally:
            await session.close()

        assert "modex-vw-session" in output
