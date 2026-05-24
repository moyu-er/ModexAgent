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
        self.window_title = None
        self._backend_started = True

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

    async def drain_startup(self):
        pass

    async def clear_input_line(self):
        pass


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
    async def test_history_action_shows_recent_output(self, tool):
        """history action should show the last command's recent output."""
        session = await tool._manager.get_or_create("tab-1")
        from framework.tools.terminal.session import CommandRecord
        session._history.append(
            CommandRecord(command="ls", output="file1\nfile2\nfile3\n")
        )
        result = await tool.execute(action="history", name="tab-1")
        assert "Recent output" in result
        assert "file3" in result

    @pytest.mark.asyncio
    async def test_interrupt_on_default_terminal(self, tool):
        """interrupt without name should target the current default terminal."""
        await tool._manager.get_or_create("tab-1")
        await tool._manager.select_default("tab-1")
        result = await tool.execute(action="interrupt")
        assert "Ctrl+C" in result
        assert "tab-1" in result

    @pytest.mark.asyncio
    async def test_interrupt_without_default_terminal(self, tool):
        """interrupt when no default terminal exists should return an error."""
        result = await tool.execute(action="interrupt")
        assert "Error" in result
        assert "No default terminal" in result

    @pytest.mark.asyncio
    async def test_invalid_action(self, tool):
        result = await tool.execute(action="invalid")
        assert "Error" in result
        assert "Unknown action" in result
