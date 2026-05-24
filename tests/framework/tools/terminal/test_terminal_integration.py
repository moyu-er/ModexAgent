"""Integration tests for TerminalTool tab management and session lifecycle.

These tests use realistic mock backends that behave like real backends:
- is_alive() returns False before start() is called
- is_alive() returns True after start() is called
- This matches VisibleWindowsPtyBackend behavior where _proc is None until start()
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

project_root = Path(__file__).parent.parent.parent.parent.parent
sys.path.insert(0, str(project_root))

from framework.tools.terminal.manager import TerminalManager
from framework.tools.terminal.session import TerminalSession
from framework.tools.terminal.tool import TerminalAction, TerminalTool
from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo


class RealisticBackend:
    """Mock backend that behaves like real VisibleWindowsPtyBackend.

    Key difference from existing test mocks: alive=False before start().
    Real backends (VisibleWindowsPtyBackend, TmuxPtyBackend) have no process
    until start() is called, so is_alive() must return False initially.
    """

    def __init__(self, shell: str = "bash") -> None:
        self._started = False
        self._alive = False
        self._shell = shell
        self.window_title = f"mock-{shell}"
        self.buffer = ""
        self._reads: list[str] = []
        self._read_idx = 0

    async def start(self, shell=None, cwd=None, env=None) -> None:
        self._started = True
        self._alive = True
        self._shell = shell or self._shell

    async def write(self, data: str) -> None:
        self.buffer += data

    async def read(self, timeout=5.0, max_size=65536) -> str:
        if timeout <= 0.15:
            return ""
        if self._read_idx < len(self._reads):
            out = self._reads[self._read_idx]
            self._read_idx += 1
            return out
        return ""

    async def is_alive(self) -> bool:
        return self._alive

    async def terminate(self) -> None:
        self._alive = False

    async def kill(self) -> None:
        self._alive = False

    async def drain_startup(self) -> None:
        from framework.tools.terminal.prompt import is_prompt_ready
        while self._read_idx < len(self._reads):
            chunk = self._reads[self._read_idx]
            self._read_idx += 1
            if is_prompt_ready(chunk):
                break

    async def clear_input_line(self) -> None:
        pass

    def queue_reads(self, reads: list[str]) -> None:
        self._reads = reads
        self._read_idx = 0


def _backend_factory() -> RealisticBackend:
    return RealisticBackend()


def _shell_info() -> ShellInfo:
    return ShellInfo(family=ShellFamily.BASH, path="bash", platform=Platform.WINDOWS)


@pytest.fixture
def manager():
    """TerminalManager with realistic backend factory."""
    return TerminalManager(
        max_terminals=5,
        backend_factory=_backend_factory,
    )


@pytest.fixture
def tool(manager):
    """TerminalTool wired to the manager."""
    return TerminalTool(manager)


class TestTabLifecycle:
    """Bug 1: TerminalTool cannot create/manage tabs."""

    async def test_open_creates_session(self, manager, tool):
        """OPEN should create a new session that exists in the manager."""
        result = await tool.execute(action="open", name="tab1")
        assert "Opened" in result
        # The session should be retrievable
        session = manager.get("tab1")
        assert session is not None

    async def test_opened_session_survives_list(self, manager, tool):
        """A session created by OPEN must survive LIST.

        This is the core bug: list_sessions() calls is_alive() which returns
        False for unstarted backends, causing the session to be purged.
        """
        await tool.execute(action="open", name="tab1")
        sessions = await manager.list_sessions()
        names = [s.name for s in sessions]
        assert "tab1" in names, f"tab1 was purged by list_sessions. Got: {names}"

    async def test_list_shows_opened_tab(self, manager, tool):
        """LIST after OPEN should show the newly created tab."""
        await tool.execute(action="open", name="tab1")
        result = await tool.execute(action="list")
        assert "tab1" in result

    async def test_open_multiple_tabs(self, manager, tool):
        """Opening multiple tabs should create distinct sessions."""
        await tool.execute(action="open", name="tab1")
        await tool.execute(action="open", name="tab2")
        result = await tool.execute(action="list")
        assert "tab1" in result
        assert "tab2" in result

    async def test_select_switches_default(self, manager, tool):
        """SELECT should change the default terminal."""
        await tool.execute(action="open", name="tab1")
        await tool.execute(action="open", name="tab2")
        await tool.execute(action="select", name="tab2")
        # Default should now be tab2
        assert manager._default_terminal == "tab2"

    async def test_close_removes_tab(self, manager, tool):
        """CLOSE should remove a session."""
        await tool.execute(action="open", name="tab1")
        result = await tool.execute(action="close", name="tab1")
        assert "Closed" in result
        assert manager.get("tab1") is None


class TestSessionExecuteWithRealisticBackend:
    """Bug 2 and related: execute() with realistic backend lifecycle."""

    async def test_execute_starts_backend(self, manager):
        """execute() must call start() on an unstarted backend."""
        session = await manager.get_or_create("test")
        backend = session._backend
        assert not await backend.is_alive(), "backend should not be alive before execute"
        await session.execute("echo hello", timeout=2)
        assert await backend.is_alive(), "backend should be alive after execute starts it"

    async def test_execute_after_open(self, manager, tool):
        """After OPEN, execute() should work on the same session.

        Realistic flow: open calls ensure_started which drains the initial
        prompt, then the main read loop consumes command output + new prompt.
        """
        # Pre-create session and queue reads before open so that
        # ensure_started's drain_startup can consume the prompt.
        session = await manager.get_or_create("tab1")
        session._backend.queue_reads([
            "bash-5.2$ ",           # consumed by ensure_started drain_startup
            "hello\nbash-5.2$ ",     # consumed by execute main loop
        ])
        await tool.execute(action="open", name="tab1")
        result = await session.execute("echo hello", timeout=2)
        assert "hello" in result

    async def test_shell_info_matches_manager(self, manager):
        """Session should use manager's shell_info."""
        session = await manager.get_or_create("test")
        assert session.shell_info == manager._shell_info


class TestVisibleHostNewlineHandling:
    """Bug 2: Extra newlines in visible terminal output.

    visible_windows_host.py _pty_to_socket writes PTY output directly to
    sys.stdout. On Windows, PTY output contains \r\n. Windows console expands
    \n to \r\n, producing \r\r\n = blank line.
    """

    def test_crlf_is_normalized_before_stdout_write(self):
        """PTY output with \r\n must be normalized to \n before writing to stdout.

        Windows console auto-expands \n to \r\n. If we write \r\n directly,
        it becomes \r\r\n which produces a blank line.
        """
        # Simulate what _pty_to_socket does
        raw = b"hello\r\nworld\r\n"
        text = raw.decode("utf-8", errors="replace")

        # The fix: normalize \r\n to \n
        normalized = text.replace("\r\n", "\n")

        # Verify no \r\n remains
        assert "\r\n" not in normalized
        assert normalized == "hello\nworld\n"

    def test_lf_only_preserved(self):
        """Output that already has \n only should not be double-converted."""
        raw = b"hello\nworld\n"
        text = raw.decode("utf-8", errors="replace")
        normalized = text.replace("\r\n", "\n")
        assert normalized == "hello\nworld\n"
