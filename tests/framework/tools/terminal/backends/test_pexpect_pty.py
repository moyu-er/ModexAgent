"""Tests for PexpectPtyBackend (mock pexpect, cross-platform)."""

from __future__ import annotations

import asyncio

import pytest

from framework.tools.terminal.backends.pexpect_pty import PexpectPtyBackend
from framework.tools.terminal.results import TerminalRead, TerminalSegment
from framework.tools.terminal.types import Platform, TerminalVisibility


class FakePexpectProcess:
    """Simulates a pexpect.spawn object for unit tests."""

    def __init__(self) -> None:
        self._alive = True
        self._sent: list[str] = []

    def send(self, data: str) -> None:
        self._sent.append(data)

    def sendintr(self) -> None:
        self._sent.append("<SIGINT>")

    def read_nonblocking(self, size: int, timeout: float = 0.5) -> str:
        # simulates no output available — use the fake's own TIMEOUT
        raise FakePexpectModule.exceptions.TIMEOUT("timeout")

    def isalive(self) -> bool:
        return self._alive

    def terminate(self, force: bool = False) -> None:
        self._alive = False


class FakePexpectModule:
    """Standalone fake pexpect (no MagicMock needed for methods)."""

    class exceptions:
        class TIMEOUT(Exception):
            pass

        class EOF(Exception):
            pass

    exceptions = exceptions  # module.exceptions accessible

    def spawn(self, shell, dimensions=None, cwd=None, env=None,
              encoding="utf-8", codec_errors="replace"):
        return FakePexpectProcess()


def _make_backend() -> PexpectPtyBackend:
    """Create a PexpectPtyBackend with FakePexpectModule pre-injected."""
    backend = PexpectPtyBackend()
    backend._pexpect = FakePexpectModule()
    return backend


class TestPexpectPtyBackendDeclarations:

    def test_platform_is_linux(self) -> None:
        backend = PexpectPtyBackend()
        assert backend.platform is Platform.LINUX
        assert backend.visibility is TerminalVisibility.HIDDEN

    def test_not_alive_before_start(self) -> None:
        backend = PexpectPtyBackend()
        assert not asyncio.get_event_loop().run_until_complete(backend.is_alive())

    def test_stdin_not_writable_before_start(self) -> None:
        backend = PexpectPtyBackend()
        assert not backend.stdin_writable()

    def test_window_title_is_none(self) -> None:
        backend = PexpectPtyBackend()
        assert backend.window_title is None


class TestPexpectPtyBackendLifecycle:

    @pytest.mark.asyncio
    async def test_start_creates_process(self) -> None:
        backend = _make_backend()
        await backend.start(shell="/bin/bash")
        assert backend._proc is not None
        assert await backend.is_alive()
        assert backend.stdin_writable()

    @pytest.mark.asyncio
    async def test_write_sends_data(self) -> None:
        backend = _make_backend()
        await backend.start(shell="/bin/bash")
        await backend.write("echo hello")
        assert "echo hello" in backend._proc._sent  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_read_returns_empty_on_no_output(self) -> None:
        backend = _make_backend()
        await backend.start(shell="/bin/bash")
        result = await backend.read(timeout=0.1, max_size=4096)
        assert result == ""

    @pytest.mark.asyncio
    async def test_read_pending_returns_terminal_read(self) -> None:
        backend = _make_backend()
        await backend.start(shell="/bin/bash")
        result = await backend.read_pending(timeout=0.1, max_size=4096)
        assert isinstance(result, TerminalRead)

    @pytest.mark.asyncio
    async def test_interrupt_calls_sendintr(self) -> None:
        backend = _make_backend()
        await backend.start(shell="/bin/bash")
        await backend.interrupt()
        assert "<SIGINT>" in backend._proc._sent  # type: ignore[union-attr]
        await backend.kill()

    @pytest.mark.asyncio
    async def test_terminate_stops_process(self) -> None:
        backend = _make_backend()
        await backend.start(shell="/bin/bash")
        assert await backend.is_alive()
        await backend.terminate()
        assert not await backend.is_alive()

    @pytest.mark.asyncio
    async def test_kill_stops_process(self) -> None:
        backend = _make_backend()
        await backend.start(shell="/bin/bash")
        assert await backend.is_alive()
        await backend.kill()
        assert not await backend.is_alive()

    @pytest.mark.asyncio
    async def test_current_segment_returns_segment(self) -> None:
        backend = _make_backend()
        await backend.start(shell="/bin/bash")
        seg = await backend.current_segment()
        assert isinstance(seg, TerminalSegment)

    @pytest.mark.asyncio
    async def test_drain_startup_completes(self) -> None:
        backend = _make_backend()
        await backend.start(shell="/bin/bash")
        await backend.drain_startup()

    @pytest.mark.asyncio
    async def test_clear_input_line_writes_readline_sequence(self) -> None:
        backend = _make_backend()
        await backend.start(shell="/bin/bash")
        await backend.clear_input_line()
        assert "\x01\x0b" in backend._proc._sent  # type: ignore[union-attr]
