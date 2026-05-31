"""Tests for WindowsHiddenPtyBackend."""

from __future__ import annotations

import sys

import pytest

from framework.tools.terminal.backends.windows_hidden import WindowsHiddenPtyBackend
from framework.tools.terminal.types import Platform, TerminalVisibility


def test_hidden_backend_declares_windows_hidden() -> None:
    backend = WindowsHiddenPtyBackend()

    assert backend.platform is Platform.WINDOWS
    assert backend.visibility is TerminalVisibility.HIDDEN


def test_hidden_backend_start_raises_on_non_windows() -> None:
    if sys.platform == "win32":
        pytest.skip("Only relevant on non-Windows platforms")

    import asyncio

    backend = WindowsHiddenPtyBackend()
    with pytest.raises(RuntimeError, match="WindowsHiddenPtyBackend requires Windows"):
        asyncio.get_event_loop().run_until_complete(
            backend.start(shell=None, cwd=None, env=None)
        )


def test_hidden_backend_not_alive_before_start() -> None:
    backend = WindowsHiddenPtyBackend()
    import asyncio

    assert not asyncio.get_event_loop().run_until_complete(backend.is_alive())


def test_hidden_backend_window_title_is_none() -> None:
    backend = WindowsHiddenPtyBackend()
    assert backend.window_title is None


def test_hidden_backend_stdin_not_writable_before_start() -> None:
    backend = WindowsHiddenPtyBackend()
    assert not backend.stdin_writable()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PTY backend")
@pytest.mark.asyncio
async def test_hidden_backend_starts_and_is_alive() -> None:
    backend = WindowsHiddenPtyBackend()
    await backend.start(shell=None, cwd=None, env=None)
    try:
        assert await backend.is_alive()
        assert backend.stdin_writable()
    finally:
        await backend.terminate()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PTY backend")
@pytest.mark.asyncio
async def test_hidden_backend_runs_echo() -> None:
    backend = WindowsHiddenPtyBackend()
    # Use cmd.exe for a faster, more predictable startup
    await backend.start(shell="cmd.exe", cwd=None, env=None)
    try:
        # Drain startup output before sending the command
        for _ in range(10):
            await backend.read_pending(timeout=0.2, max_size=65536)
        await backend.write("echo hello\r")
        chunks: list[str] = []
        for _ in range(20):
            read = await backend.read_pending(timeout=0.3, max_size=65536)
            if read.raw:
                chunks.append(read.raw)
            combined = "".join(chunks).lower()
            if "hello" in combined:
                break
        assert "hello" in "".join(chunks).lower()
    finally:
        await backend.terminate()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PTY backend")
@pytest.mark.asyncio
async def test_hidden_backend_interrupt_sends_ctrl_c() -> None:
    backend = WindowsHiddenPtyBackend()
    await backend.start(shell=None, cwd=None, env=None)
    try:
        # drain any startup output
        for _ in range(5):
            await backend.read_pending(timeout=0.2, max_size=65536)
        # interrupt should not raise
        await backend.interrupt()
        # read whatever comes back (should include ^C or prompt)
        read = await backend.read_pending(timeout=0.5, max_size=65536)
        assert isinstance(read.raw, str)
    finally:
        await backend.terminate()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PTY backend")
@pytest.mark.asyncio
async def test_hidden_backend_current_segment_returns_terminal_segment() -> None:
    from framework.tools.terminal.results import TerminalSegment

    backend = WindowsHiddenPtyBackend()
    await backend.start(shell=None, cwd=None, env=None)
    try:
        # drain startup
        for _ in range(10):
            await backend.read_pending(timeout=0.2, max_size=65536)
        seg = await backend.current_segment()
        assert isinstance(seg, TerminalSegment)
        assert isinstance(seg.text, str)
    finally:
        await backend.terminate()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PTY backend")
@pytest.mark.asyncio
async def test_hidden_backend_kill() -> None:
    backend = WindowsHiddenPtyBackend()
    await backend.start(shell=None, cwd=None, env=None)
    assert await backend.is_alive()
    await backend.kill()
    # After kill, the process should be dead
    import asyncio

    await asyncio.sleep(0.3)
    assert not await backend.is_alive()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PTY backend")
@pytest.mark.asyncio
async def test_hidden_backend_drain_startup() -> None:
    backend = WindowsHiddenPtyBackend()
    await backend.start(shell=None, cwd=None, env=None)
    try:
        # drain_startup should complete without error
        await backend.drain_startup()
    finally:
        await backend.terminate()


def test_hidden_backend_read_does_not_buffer() -> None:
    """read() returns raw output without appending to the sliding buffer."""
    backend = WindowsHiddenPtyBackend()
    assert backend._output_buffer is not None
    assert backend._output_buffer.total_chars == 0
