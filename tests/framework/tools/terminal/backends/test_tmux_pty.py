"""Tests for TmuxPtyBackend. Runs on Unix only with tmux installed."""

import importlib.util
import sys

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="Unix only")
class TestTmuxPtyBackend:
    @pytest.fixture(autouse=True)
    def _check_tmux(self) -> None:
        if importlib.util.find_spec("libtmux") is None:
            pytest.skip("libtmux not installed")
        import shutil
        if shutil.which("tmux") is None:
            pytest.skip("tmux binary not found")

    @pytest.mark.asyncio
    async def test_start_creates_session(self) -> None:
        from framework.tools.terminal.backends.tmux_pty import TmuxPtyBackend
        backend = TmuxPtyBackend()
        await backend.start()
        try:
            assert await backend.is_alive()
            assert backend._session_name is not None
        finally:
            await backend.terminate()

    @pytest.mark.asyncio
    async def test_drain_startup_detects_prompt(self) -> None:
        from framework.tools.terminal.backends.tmux_pty import TmuxPtyBackend
        backend = TmuxPtyBackend()
        await backend.start()
        try:
            await backend.drain_startup()
            assert backend._last_capture is not None
        finally:
            await backend.terminate()

    @pytest.mark.asyncio
    async def test_write_and_read(self) -> None:
        from framework.tools.terminal.backends.tmux_pty import TmuxPtyBackend
        backend = TmuxPtyBackend()
        await backend.start()
        await backend.drain_startup()
        try:
            await backend.write("echo tmux-test-ok\n")
            import asyncio
            await asyncio.sleep(1.0)
            output = await backend.read(timeout=2.0)
            assert "tmux-test-ok" in output
        finally:
            await backend.terminate()

    @pytest.mark.asyncio
    async def test_is_alive_after_terminate(self) -> None:
        from framework.tools.terminal.backends.tmux_pty import TmuxPtyBackend
        backend = TmuxPtyBackend()
        await backend.start()
        assert await backend.is_alive()
        await backend.terminate()
        assert not await backend.is_alive()

    @pytest.mark.asyncio
    async def test_diff_output_with_new_lines(self) -> None:
        from framework.tools.terminal.backends.tmux_pty import TmuxPtyBackend
        backend = TmuxPtyBackend()
        prev = "line1\nline2\n$ "
        curr = "line1\nline2\n$ echo hi\nhi\n$ "
        assert backend._diff_output(prev, curr) == "$ echo hi\nhi"

    @pytest.mark.asyncio
    async def test_diff_output_no_change(self) -> None:
        from framework.tools.terminal.backends.tmux_pty import TmuxPtyBackend
        backend = TmuxPtyBackend()
        text = "line1\nline2\n$ "
        assert backend._diff_output(text, text) == ""
