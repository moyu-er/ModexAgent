"""Tests for TerminalSession."""

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from framework.tools.standard.shell_tool import ShellInfo
from framework.tools.terminal.session import CommandRecord, TerminalSession


class MockBackend:
    """Mock TerminalBackend for testing."""

    def __init__(self) -> None:
        self.alive = True
        self.buffer = ""
        self._started = False

    async def start(self, shell: str | None = None, cwd: str | None = None, env: dict[str, str] | None = None) -> None:
        self._started = True

    async def write(self, data: str) -> None:
        self.buffer += data

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        return "mock-output\n$ "

    async def is_alive(self) -> bool:
        return self.alive

    async def terminate(self) -> None:
        self.alive = False

    async def kill(self) -> None:
        self.alive = False


class TestTerminalSession:
    def test_session_creation(self) -> None:
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(name="bash", path="/bin/bash", platform="linux", is_stateful=True),
        )
        assert session.name == "test"
        assert session.shell_info.name == "bash"

    @pytest.mark.asyncio
    async def test_execute_restarts_dead_backend(self) -> None:
        backend = MockBackend()
        backend.alive = False
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(name="bash", path="/bin/bash", platform="linux", is_stateful=True),
        )
        result = await session.execute("echo hello")
        assert backend._started is True
        assert "mock-output" in result

    @pytest.mark.asyncio
    async def test_execute_records_history(self) -> None:
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(name="bash", path="/bin/bash", platform="linux", is_stateful=True),
            max_history=2,
        )
        await session.execute("cmd1")
        await session.execute("cmd2")
        history = session.get_history()
        assert len(history) == 2
        assert history[0].command == "cmd1"
        assert history[1].command == "cmd2"

    @pytest.mark.asyncio
    async def test_history_truncation(self) -> None:
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(name="bash", path="/bin/bash", platform="linux", is_stateful=True),
            max_history=2,
            history_truncate=10,
        )
        await session.execute("very_long_command_name")
        history = session.get_history()
        assert len(history[0].command) == 10

    def test_terminal_info(self) -> None:
        backend = MockBackend()
        session = TerminalSession(
            name="test",
            backend=backend,
            shell_info=ShellInfo(name="bash", path="/bin/bash", platform="linux", is_stateful=True),
        )
        info = session.to_info()
        assert info.name == "test"
        assert info.shell_type == "bash"
        assert info.command_count == 0
