"""Real VisibleWindowsPtyBackend integration tests.

These tests exercise the actual visible-terminal backend which spawns a
real OS window via CREATE_NEW_CONSOLE.  I/O is forwarded through a TCP
socket, so the test sees exactly what the visible window shows.
"""

from __future__ import annotations

import asyncio
import re as _re
import shutil
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
class TestVisibleWindowsPtyIntegration:
    """Integration tests using real visible terminal backend with bash."""

    async def _read_until(self, backend, marker: str, timeout: float = 15.0) -> str:
        """Read repeatedly until *marker* appears in output or timeout."""
        output = ""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            chunk = await backend.read(timeout=0.5, max_size=4096)
            if chunk:
                output += chunk
            if marker in output:
                break
            await asyncio.sleep(0.1)
        return output

    @pytest.fixture
    async def bash_backend(self):
        """Yield a started and drained visible bash backend."""
        bash_path = shutil.which("bash")
        if bash_path is None:
            pytest.skip("bash not available on Windows")

        from framework.tools.terminal.backends.visible_windows import (
            VisibleWindowsPtyBackend,
        )

        b = VisibleWindowsPtyBackend()
        await b.start(bash_path)
        try:
            await b.drain_startup()
            yield b
        finally:
            await b.terminate()

    # --- bash tests ---

    @pytest.mark.asyncio
    async def test_bash_visible_window_starts(self, bash_backend) -> None:
        """Visible bash window starts and drain completes without error."""
        assert bash_backend.window_title
        assert await bash_backend.is_alive()

    @pytest.mark.asyncio
    async def test_bash_echo_after_drain(self, bash_backend) -> None:
        """After drain, a simple echo produces the expected text."""
        await bash_backend.write("echo modex-visible-bash\n")
        output = await self._read_until(bash_backend, "modex-visible-bash")
        assert "modex-visible-bash" in output

    @pytest.mark.asyncio
    async def test_bash_multiple_commands(self, bash_backend) -> None:
        """Visible bash handles sequential commands."""
        await bash_backend.write("echo first-visible\n")
        out_a = await self._read_until(bash_backend, "first-visible")
        assert "first-visible" in out_a

        await bash_backend.write("echo second-visible\n")
        out_b = await self._read_until(bash_backend, "second-visible")
        assert "second-visible" in out_b

    @pytest.mark.asyncio
    async def test_bash_first_command_no_da1(self, bash_backend) -> None:
        """After drain_startup(), DA1 must not leak into command output."""
        await bash_backend.write("echo modex-da1-test\n")
        output = await self._read_until(bash_backend, "modex-da1-test")

        assert "modex-da1-test" in output
        da1 = _re.compile(r"\x1b\[\?[\d;]+c")
        assert not da1.search(output), (
            f"DA1 leaked into bash command output: {output[:400]!r}"
        )

    @pytest.mark.asyncio
    async def test_bash_window_survives_between_commands(self, bash_backend) -> None:
        """The visible window stays open across multiple commands."""
        for i in range(3):
            await bash_backend.write(f"echo survive-{i}\n")
            out = await self._read_until(bash_backend, f"survive-{i}")
            assert f"survive-{i}" in out
            assert await bash_backend.is_alive()

    @pytest.mark.asyncio
    async def test_terminal_session_visible_bash_submits_without_manual_enter(self) -> None:
        """BotProject's visible TerminalSession must submit commands by itself."""
        bash_path = shutil.which("bash")
        if bash_path is None:
            pytest.skip("bash not available on Windows")

        from framework.tools.terminal.session import TerminalSession
        from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo
        from framework.tools.terminal.backends.visible_windows import (
            VisibleWindowsPtyBackend,
        )

        backend = VisibleWindowsPtyBackend()
        session = TerminalSession(
            name="visible-integration",
            backend=backend,
            shell_info=ShellInfo(
                family=ShellFamily.BASH,
                path=bash_path,
                platform=Platform.WINDOWS,
            ),
        )
        try:
            output = await session.execute("echo modex-visible-session-submit", timeout=5.0)
        finally:
            await session.close()

        assert "modex-visible-session-submit" in output
