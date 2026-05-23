"""Tests for TerminalSessionExecutor."""

import tempfile
from pathlib import Path

import pytest

from framework.tools.standard.shell_tool import TerminalSessionExecutor
from framework.tools.terminal.manager import TerminalManager


class MockBackend:
    def __init__(self):
        self.alive = True
        self.buffer = ""
        self._started = False

    async def start(self, shell=None, cwd=None, env=None):
        self._started = True

    async def write(self, data):
        self.buffer += data

    async def read(self, timeout=5.0, max_size=65536):
        return "mock-output\n$ "

    async def is_alive(self):
        return self.alive

    async def terminate(self):
        self.alive = False

    async def kill(self):
        self.alive = False


class TestTerminalSessionExecutor:
    @pytest.fixture
    def executor(self):
        with tempfile.TemporaryDirectory() as td:
            tm = TerminalManager(
                storage_dir=Path(td),
                max_terminals=3,
                backend_factory=MockBackend,
            )
            yield TerminalSessionExecutor(terminal_manager=tm)

    @pytest.mark.asyncio
    async def test_execute_creates_default_terminal(self, executor):
        """If no default terminal exists, execute should auto-create one."""
        result = await executor.execute("echo hello")
        assert isinstance(result, str)

    def test_shell_info_reflects_manager_default(self, executor):
        info = executor.shell_info()
        assert info.is_stateful is True
        assert info.name in ("bash", "powershell", "cmd", "zsh", "sh")
