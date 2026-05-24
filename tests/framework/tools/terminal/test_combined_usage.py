"""Combined usage tests: ShellTool + TerminalTool interaction.

Covers:
- busy message guides agent to interrupt
- shell timeout -> terminal interrupt -> shell normal
- action sequence: open -> list -> select -> history -> interrupt -> close
- user closes tab externally -> agent auto-recovers
- waiting_input -> user input -> command continues
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from framework.tools.standard.shell_tool import (
    ShellTool,
    TerminalSessionExecutor,
)
from framework.tools.terminal.manager import TerminalManager
from framework.tools.terminal.session import CommandRecord
from framework.tools.terminal.tool import TerminalTool
from framework.tools.terminal.types import Platform, ShellFamily, ShellInfo


class SlowBackend:
    """Backend that simulates a slow command (times out)."""

    def __init__(self) -> None:
        self.alive = True
        self.buffer = ""
        self._started = False
        self._read_idx = 0
        self._reads: list[str] = []
        self.interrupted = False

    async def start(self, shell=None, cwd=None, env=None) -> None:
        self._started = True
        self.alive = True

    async def write(self, data: str) -> None:
        self.buffer += data
        if "\x03" in data:
            self.interrupted = True

    async def read(self, timeout=5.0, max_size=65536) -> str:
        if timeout <= 0.15:
            return ""
        if self.interrupted:
            return "^C\n$ "
        if self._read_idx < len(self._reads):
            out = self._reads[self._read_idx]
            self._read_idx += 1
            return out
        return ""

    async def is_alive(self) -> bool:
        return self.alive

    async def terminate(self) -> None:
        self.alive = False

    async def kill(self) -> None:
        self.alive = False

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


class BusyMessageBackend(SlowBackend):
    """Backend that always returns partial output (command never completes)."""

    async def read(self, timeout=5.0, max_size=65536) -> str:
        if timeout <= 0.15:
            return ""
        return "partial output\n"


@pytest.fixture
def manager():
    return TerminalManager(
        max_terminals=5,
        backend_factory=lambda: SlowBackend(),
    )


@pytest.fixture
def tool(manager):
    return TerminalTool(manager)


@pytest.fixture
def shell(manager):
    executor = TerminalSessionExecutor(terminal_manager=manager)
    return ShellTool(executor=executor)


class TestBusyMessage:
    """Busy status must tell the agent how to interrupt."""

    @pytest.mark.asyncio
    async def test_busy_message_suggests_interrupt(self, manager) -> None:
        """When a command is busy, the message should guide the agent
        to use ^C to interrupt."""
        session = await manager.get_or_create("tab-1")
        session._backend = BusyMessageBackend()
        session._needs_restart = False
        session._backend_started = True

        # First: timeout
        first = await session.execute("sleep 100", timeout=0.05)
        assert "<status>timeout</status>" in first

        # Second: busy message should suggest interrupt
        second = await session.execute("echo next", timeout=1.0)
        assert "<status>busy</status>" in second
        assert "interrupt" in second.lower() or "^c" in second.lower()


class TestShellAndTerminalToolInteraction:
    """ShellTool and TerminalTool must work together seamlessly."""

    @pytest.mark.asyncio
    async def test_shell_timeout_terminal_interrupt_shell_normal(self, manager, tool, shell) -> None:
        """Full flow: shell timeout -> terminal interrupt -> shell works again."""
        # Replace default session backend with a slow one
        session = await manager.get_or_create("default")
        slow_backend = BusyMessageBackend()
        session._backend = slow_backend
        session._needs_restart = False
        session._backend_started = True

        # 1. Shell executes slow command -> timeout
        first = await shell.execute("sleep 100")
        assert "<status>timeout</status>" in first

        # 2. Shell tries another command -> busy (blocked)
        second = await shell.execute("echo blocked")
        assert "<status>busy</status>" in second

        # 3. Terminal interrupts the default tab
        third = await tool.execute(action="interrupt")
        assert "Ctrl+C" in third
        assert session._busy_after_timeout is False
        assert "\x03" in slow_backend.buffer

        # 4. Shell works again
        session._backend = SlowBackend()
        session._backend.queue_reads(["$ ", "done\n$ "])
        fourth = await shell.execute("echo done")
        assert "<status>busy</status>" not in fourth
        assert "<status>timeout</status>" not in fourth


class TestActionSequence:
    """Multiple terminal actions in sequence."""

    @pytest.mark.asyncio
    async def test_open_list_select_history_interrupt_close(self, manager, tool) -> None:
        """Full lifecycle: open -> list -> select -> history -> interrupt -> close."""

        # 1. OPEN
        r1 = await tool.execute(action="open", name="tab-1")
        assert "Opened" in r1

        # 2. LIST
        r2 = await tool.execute(action="list")
        assert "tab-1" in r2

        # 3. SELECT
        r3 = await tool.execute(action="select", name="tab-1")
        assert "Selected" in r3
        assert manager._default_terminal == "tab-1"

        # 4. Add history and then HISTORY action
        session = manager.get("tab-1")
        session._history.append(CommandRecord(command="ls", output="a\nb\nc\n"))
        r4 = await tool.execute(action="history", name="tab-1")
        assert "Recent output" in r4
        assert "c" in r4

        # 5. INTERRUPT (on default)
        r5 = await tool.execute(action="interrupt")
        assert "Ctrl+C" in r5
        assert "tab-1" in r5

        # 6. CLOSE
        r6 = await tool.execute(action="close", name="tab-1")
        assert "Closed" in r6

        # 7. LIST should be empty
        r7 = await tool.execute(action="list")
        assert "No active terminals" in r7


class TestUserCloseRecovery:
    """When user closes a tab externally, agent must auto-recover."""

    @pytest.mark.asyncio
    async def test_user_close_agent_recreates(self, manager, shell) -> None:
        """User manually closes the visible terminal; next shell.execute
        should automatically create a new session."""
        session = await manager.get_or_create("tab-1")
        await manager.select_default("tab-1")

        # Simulate user closing the terminal (backend dies)
        session._backend_started = True
        session._backend.alive = False

        # Next shell.execute should auto-recreate
        # The new backend will be fresh (started on execute)
        fresh_backend = SlowBackend()
        fresh_backend.queue_reads(["$ ", "hello\n$ "])

        # We need to patch the factory so the next get_or_create uses our backend
        manager._backend_factory = lambda: fresh_backend

        result = await shell.execute("echo hello")
        assert "hello" in result
        assert "<status>busy</status>" not in result


class TestWaitingInputContinuation:
    """After waiting_input, user input should allow command to continue."""

    @pytest.mark.asyncio
    async def test_waiting_input_then_input_continues(self, manager, shell) -> None:
        """ssh password prompt: first execute returns waiting_input,
        second execute sends password, command continues."""
        session = await manager.get_or_create("default")
        await manager.select_default("default")

        class SshBackend(SlowBackend):
            def __init__(self) -> None:
                super().__init__()
                self.queue_reads([
                    "$ ",
                    "[sudo] password for user: ",
                    "mypassword\n",
                    "logged in\n$ ",
                ])

        backend = SshBackend()
        session._backend = backend
        session._needs_restart = True  # Let drain_startup consume the initial prompt
        session._backend_started = True

        # 1. SSH command triggers waiting_input
        first = await shell.execute("ssh root@host")
        assert "<status>waiting_input</status>" in first
        assert "password" in first.lower()

        # 2. Send password — should NOT clear input line
        second = await shell.execute("mypassword")
        assert "mypassword" in backend.buffer

        # 3. After password, command continues (prompt returns)
        assert "logged in" in second or "mypassword" in second
