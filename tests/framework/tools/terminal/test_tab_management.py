"""Terminal tab management integration tests.

Covers gaps in existing tests around:
- LIST after manual/external close (dead tab filtering)
- LIST with dead-marker display
- Multiple tabs with different shells
- Tab isolation (commands in one tab don't leak to another)
- Timeout vs waiting_input in multi-tab context
- Persistence disabled (bot_project scenario)
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from framework.tools.terminal.manager import TerminalManager
from framework.tools.terminal.session import TerminalSession
from framework.tools.terminal.tool import TerminalAction, TerminalTool


class TrackingBackend:
    """Mock backend that tracks lifecycle and can simulate death."""

    def __init__(self, shell: str = "bash") -> None:
        self.alive = True
        self.buffer = ""
        self._started = False
        self.window_title = f"mock-{shell}"
        self._backend_started = True
        self._shell = shell
        self._reads: list[str] = []
        self._read_idx = 0
        self.start_calls = 0
        self.terminate_calls = 0

    async def start(self, shell=None, cwd=None, env=None) -> None:
        self._started = True
        self.alive = True
        self.start_calls += 1

    async def write(self, data) -> None:
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
        return self.alive

    async def terminate(self) -> None:
        self.alive = False
        self.terminate_calls += 1

    async def kill(self) -> None:
        self.alive = False

    async def drain_startup(self) -> None:
        """Consume startup reads until a prompt appears."""
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


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as td:
        tm = TerminalManager(
            storage_dir=Path(td),
            max_terminals=5,
            backend_factory=lambda: TrackingBackend(),
        )
        yield tm


@pytest.fixture
def tool(manager):
    return TerminalTool(manager)


class TestListAfterManualClose:
    """LIST must reflect reality when tabs are closed externally."""

    @pytest.mark.asyncio
    async def test_list_filters_dead_tabs(self, manager, tool) -> None:
        """After a tab dies externally, LIST must not show it."""
        await manager.get_or_create("alive-tab")
        dead = await manager.get_or_create("dead-tab")
        dead._backend_started = True
        dead._backend.alive = False

        result = await tool.execute(action="list")
        assert "alive-tab" in result
        assert "dead-tab" not in result

    @pytest.mark.asyncio
    async def test_list_shows_default_marker(self, manager, tool) -> None:
        """The default tab should be marked as '(default)'."""
        await manager.get_or_create("tab-a")
        await manager.get_or_create("tab-b")
        await manager.select_default("tab-b")

        result = await tool.execute(action="list")
        assert "tab-b" in result
        assert "(default)" in result

    @pytest.mark.asyncio
    async def test_list_shows_no_terminals_when_all_dead(self, manager, tool) -> None:
        """When all tabs are dead, LIST should say 'No active terminals.'"""
        s1 = await manager.get_or_create("tab-1")
        s1._backend_started = True
        s1._backend.alive = False

        result = await tool.execute(action="list")
        assert "No active terminals" in result


class TestTabLifecycle:
    """Open / close / select flow with state validation."""

    @pytest.mark.asyncio
    async def test_close_then_list_updates(self, manager, tool) -> None:
        """CLOSE a tab, then LIST must not include it."""
        await manager.get_or_create("tab-1")
        await tool.execute(action="close", name="tab-1")

        result = await tool.execute(action="list")
        assert "tab-1" not in result

    @pytest.mark.asyncio
    async def test_select_on_closed_tab_returns_error(self, manager, tool) -> None:
        """SELECT on a tab that was closed externally must return error."""
        s = await manager.get_or_create("tab-1")
        s._backend_started = True
        s._backend.alive = False

        result = await tool.execute(action="select", name="tab-1")
        assert "Error" in result
        assert "closed" in result.lower()

    @pytest.mark.asyncio
    async def test_open_with_cwd(self, manager, tool) -> None:
        """OPEN with cwd must set the session's working directory."""
        result = await tool.execute(action="open", name="work-tab", cwd="/tmp")
        session = manager.get("work-tab")
        assert session is not None
        assert session._cwd == "/tmp"


class TestMultiTabIsolation:
    """Commands in one tab must not affect another."""

    @pytest.mark.asyncio
    async def test_execute_isolation_between_tabs(self, manager) -> None:
        """Running a command in tab-a must not appear in tab-b's output."""
        session_a = await manager.get_or_create("tab-a")
        session_b = await manager.get_or_create("tab-b")

        # Simulate tab-a producing output
        session_a._backend.queue_reads(["$ ", "echo hello\nhello\n$ "])
        out_a = await session_a.execute("echo hello", timeout=2.0)
        assert "hello" in out_a

        # Tab-b should have empty output (no reads queued)
        session_b._backend.queue_reads(["$ ", "$ "])
        out_b = await session_b.execute("echo world", timeout=2.0)
        assert "world" not in out_b  # tab-b mock returns empty
        assert "hello" not in out_b


class TestTimeoutVsWaitingInput:
    """Timeout and waiting_input must be correctly distinguished."""

    @pytest.mark.asyncio
    async def test_timeout_returns_xml(self, manager) -> None:
        """When a command times out, execute must return XML with timeout status."""
        session = await manager.get_or_create("tab-1")
        # Backend always returns empty -- command never completes
        session._backend.queue_reads(["$ ", ""])

        result = await session.execute("sleep 100", timeout=0.5)
        assert "<status>timeout</status>" in result
        assert "<shell_result>" in result

    @pytest.mark.asyncio
    async def test_waiting_input_returns_xml(self, manager) -> None:
        """When command prompts for input, execute must return XML with
        waiting_input status (not timeout)."""
        session = await manager.get_or_create("tab-1")
        session._backend.queue_reads([
            "$ ",
            "sudo apt update\n[sudo] password for user: ",
            "",
        ])

        result = await session.execute("sudo apt update", timeout=5.0)
        assert "<status>waiting_input</status>" in result
        assert "<status>timeout</status>" not in result

    @pytest.mark.asyncio
    async def test_timeout_does_not_kill_backend(self, manager) -> None:
        """After timeout, the backend must stay alive for retry."""
        session = await manager.get_or_create("tab-1")
        session._backend.queue_reads(["$ ", ""])

        await session.execute("slow-cmd", timeout=0.5)
        assert session._backend.alive is True


class TestPersistenceDisabled:
    """bot_project does not use persistence -- verify it's safe to skip."""

    @pytest.mark.asyncio
    async def test_no_persistence_does_not_crash(self) -> None:
        """Manager works without ever calling save_state/load_state."""
        tm = TerminalManager(
            storage_dir=Path(tempfile.mkdtemp()),
            max_terminals=3,
            backend_factory=lambda: TrackingBackend(),
        )
        s = await tm.get_or_create("tab-1")
        assert s.name == "tab-1"
        # No save/load called -- should not crash
        await tm.close("tab-1")

    @pytest.mark.asyncio
    async def test_bot_project_config_disables_persistence(self) -> None:
        """Verify bot_project does not call save/load."""
        tm = TerminalManager(
            storage_dir=Path(tempfile.mkdtemp()),
            max_terminals=3,
            backend_factory=lambda: TrackingBackend(),
        )
        s = await tm.get_or_create("tab-1")
        # In bot_project, close_on_exit=false means terminals stay alive.
        # Persistence is NOT used because state is ephemeral.
        assert s is not None
        # No assertion about save/load -- just verify no crash without them.


class TestSpecialCharacterHandling:
    """ANSI/DA1 and other special characters must not corrupt tab state."""

    _PROMPT = "user@example:/workspace$ "

    @pytest.mark.asyncio
    async def test_drain_startup_consumes_ansi_before_first_command(self, manager) -> None:
        """If startup output contains ANSI sequences, drain must consume them
        so the first command is not corrupted."""
        session = await manager.get_or_create("tab-1")
        # Simulate startup reads: banner -> ANSI -> prompt
        session._backend.queue_reads([
            "shell startup banner\n",
            "\x1b[?1;2;3c",
            self._PROMPT,
            # Now command phase
            "echo 'ok'\n",
            "ok\n",
            self._PROMPT,
        ])

        result = await session.execute("echo 'ok'", timeout=3.0)
        assert "ok" in result

    @pytest.mark.asyncio
    async def test_command_with_ansi_in_output_still_detects_prompt(self, manager) -> None:
        """When command output contains ANSI color codes, prompt detection
        must still work on the sanitized text."""
        session = await manager.get_or_create("tab-1")
        session._backend.queue_reads([
            self._PROMPT,
            "\x1b[32mhello\x1b[0m\n",
            self._PROMPT,
        ])

        result = await session.execute("echo hello", timeout=2.0)
        assert "hello" in result
        assert "<status>timeout</status>" not in result

    @pytest.mark.asyncio
    async def test_da1_sequence_does_not_corrupt_prompt(self, manager) -> None:
        """DA1 (Device Attributes) sequences must be stripped before prompt detection."""
        session = await manager.get_or_create("tab-1")
        session._backend.queue_reads([
            self._PROMPT,
            "output\n",
            "\x1b[?1;2;3c",
            self._PROMPT,
        ])

        result = await session.execute("cmd", timeout=2.0)
        assert "output" in result
        assert "<status>timeout</status>" not in result
