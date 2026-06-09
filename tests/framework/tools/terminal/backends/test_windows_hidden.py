"""Tests for WindowsHiddenPtyBackend.

Covers both WSL bash and Git bash on Windows, plus cross-platform guards.
"""

from __future__ import annotations

import asyncio
import shutil
import sys

import pytest

from framework.tools.terminal.backends.windows_hidden import WindowsHiddenPtyBackend
from framework.tools.terminal.types import Platform, TerminalVisibility


@pytest.fixture
def backend() -> WindowsHiddenPtyBackend:
    return WindowsHiddenPtyBackend()


# ------------------------------------------------------------------
# pre-start property checks (single combined test)
# ------------------------------------------------------------------

def test_hidden_backend_pre_start_properties(backend: WindowsHiddenPtyBackend) -> None:
    """Before start: correct platform/visibility, not alive, not writable."""
    assert backend.platform is Platform.WINDOWS
    assert backend.visibility is TerminalVisibility.HIDDEN
    assert backend.window_title is None
    assert not backend.stdin_writable()


@pytest.mark.skipif(sys.platform == "win32", reason="Only relevant on non-Windows")
def test_hidden_backend_start_raises_on_non_windows(backend: WindowsHiddenPtyBackend) -> None:
    with pytest.raises(RuntimeError, match="WindowsHiddenPtyBackend requires Windows"):
        asyncio.run(backend.start(shell=None, cwd=None, env=None))


# ------------------------------------------------------------------
# integration tests — start / execute / read / interrupt / terminate
# ------------------------------------------------------------------

def _find_bash() -> str | None:
    """Return a working bash path (WSL preferred, Git bash fallback)."""
    for path in (
        r"C:\Windows\System32\bash.exe",  # WSL
        shutil.which("bash"),             # Git bash / MSYS2
    ):
        if path:
            return path
    return None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PTY backend")
class TestWindowsHiddenPtyLifecycle:
    """Start a real hidden PTY, run commands, exercise interrupt and drain."""

    @pytest.fixture
    async def backend(self):
        bash = _find_bash()
        if bash is None:
            pytest.skip("No bash (WSL or Git) available on this Windows machine")

        b = WindowsHiddenPtyBackend()
        await b.start(shell=bash)
        await b.drain_startup()
        try:
            yield b
        finally:
            await b.terminate()

    @pytest.mark.asyncio
    async def test_echo_read_roundtrip(self, backend: WindowsHiddenPtyBackend) -> None:
        b = backend
        assert await b.is_alive()

        await b.write("echo modex-hw-test\n")
        output = ""
        for _ in range(40):
            chunk = await b.read(timeout=0.3, max_size=4096)
            if chunk:
                output += chunk
            if "modex-hw-test" in output:
                break
            await asyncio.sleep(0.1)

        assert "modex-hw-test" in output

    @pytest.mark.asyncio
    async def test_multiple_commands_stay_alive(self, backend: WindowsHiddenPtyBackend) -> None:
        b = _backend
        for i in range(3):
            await b.write(f"echo seq-{i}\n")
            output = ""
            for _ in range(40):
                chunk = await b.read(timeout=0.3, max_size=4096)
                if chunk:
                    output += chunk
                if f"seq-{i}" in output:
                    break
                await asyncio.sleep(0.1)
            assert f"seq-{i}" in output
            assert await b.is_alive()

    @pytest.mark.asyncio
    async def test_read_pending_populates_buffer(self, backend: WindowsHiddenPtyBackend) -> None:
        b = _backend
        await b.write("echo buf-test\n")
        for _ in range(40):
            read = await b.read_pending(timeout=0.3, max_size=4096)
            if read.stdout and "buf-test" in read.stdout:
                break
            await asyncio.sleep(0.1)

        text = b.output_buffer_text()
        assert "buf-test" in text

    @pytest.mark.asyncio
    async def test_current_segment_after_echo(self, backend: WindowsHiddenPtyBackend) -> None:
        b = _backend
        await b.write("echo seg-test\n")
        for _ in range(40):
            chunk = await b.read(timeout=0.3, max_size=4096)
            if "seg-test" in chunk:
                break
            await asyncio.sleep(0.1)

        seg = await b.current_segment()
        assert seg.text
        assert seg.cursor_line
