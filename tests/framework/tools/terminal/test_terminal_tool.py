"""Tests for TerminalTool."""

import tempfile
from pathlib import Path

import pytest

from framework.tools.terminal.manager import TerminalManager
from framework.tools.terminal.tool import TerminalAction, TerminalTool


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


@pytest.fixture
def tool():
    with tempfile.TemporaryDirectory() as td:
        tm = TerminalManager(
            storage_dir=Path(td),
            max_terminals=3,
            backend_factory=MockBackend,
        )
        yield TerminalTool(tm)


class TestTerminalTool:
    @pytest.mark.asyncio
    async def test_open_action(self, tool):
        result = await tool.execute(action="open", name="test-tab")
        assert "Opened terminal" in result
        assert "test-tab" in result

    @pytest.mark.asyncio
    async def test_list_action(self, tool):
        await tool._manager.get_or_create("tab-1")
        result = await tool.execute(action="list")
        assert "tab-1" in result

    @pytest.mark.asyncio
    async def test_close_action(self, tool):
        await tool._manager.get_or_create("tab-1")
        result = await tool.execute(action="close", name="tab-1")
        assert "Closed" in result

    @pytest.mark.asyncio
    async def test_select_action(self, tool):
        await tool._manager.get_or_create("tab-1")
        result = await tool.execute(action="select", name="tab-1")
        assert "Selected" in result

    @pytest.mark.asyncio
    async def test_history_action(self, tool):
        session = await tool._manager.get_or_create("tab-1")
        from framework.tools.terminal.session import CommandRecord
        session._history.append(CommandRecord(command="ls", output="file.txt"))
        result = await tool.execute(action="history", name="tab-1")
        assert "ls" in result

    @pytest.mark.asyncio
    async def test_invalid_action(self, tool):
        result = await tool.execute(action="invalid")
        assert "Error" in result
        assert "Unknown action" in result
