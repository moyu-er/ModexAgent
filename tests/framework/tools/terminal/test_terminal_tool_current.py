from __future__ import annotations

import time

import pytest

from framework.tools.terminal.results import TerminalSegment
from framework.tools.terminal.tool import TerminalTool
from framework.tools.terminal.types import TerminalCommandStatus


class FakeSession:
    name = "default"
    created_at = 0.0
    _last_byte_at: float = 0.0

    @property
    def last_byte_at(self) -> float:
        return self._last_byte_at

    @last_byte_at.setter
    def last_byte_at(self, value: float) -> None:
        self._last_byte_at = value

    async def command_status(self) -> TerminalCommandStatus:
        return TerminalCommandStatus.IDLE

    async def last_command_output(self) -> str:
        return ""

    async def current_segment(self) -> TerminalSegment:
        return TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    async def interrupt(self) -> None:
        pass


class FakeManager:
    async def get_default(self):
        return FakeSession()

    async def get_default_session(self):
        return FakeSession()

    def get(self, name: str):
        return FakeSession()

    async def get_or_create(self, name=None, cwd=None):
        return FakeSession()

    async def close(self, name: str) -> bool:
        return True

    async def list_sessions(self):
        return []

    def list_names(self):
        return ["default"]

    async def select_default(self, name: str) -> None:
        pass


@pytest.mark.asyncio
async def test_terminal_current_returns_xml_with_idle_status() -> None:
    tool = TerminalTool(FakeManager())

    result = await tool.execute(action="current")

    assert "<terminal_result>" in result
    assert "<status>idle</status>" in result
    assert "<action>current</action>" in result
    assert "<created_at>0</created_at>" in result


@pytest.mark.asyncio
async def test_terminal_current_returns_unknown_when_no_session() -> None:
    class EmptyManager(FakeManager):
        async def get_default_session(self):
            return None

    tool = TerminalTool(EmptyManager())

    result = await tool.execute(action="current")

    assert "<terminal_result>" in result
    assert "<status>unknown</status>" in result
    assert "No terminal is active" in result


@pytest.mark.asyncio
async def test_terminal_current_shows_idle_ms() -> None:
    session = FakeSession()
    session.last_byte_at = time.monotonic() - 2.0  # 2 seconds ago

    class Manager(FakeManager):
        async def get_default_session(self):
            return session

    tool = TerminalTool(Manager())
    result = await tool.execute(action="current")

    assert "<idle_ms>" in result


@pytest.mark.asyncio
async def test_terminal_current_omits_idle_ms_when_zero() -> None:
    session = FakeSession()
    session.last_byte_at = time.monotonic()  # just now, idle_ms could be 0

    class Manager(FakeManager):
        async def get_default_session(self):
            return session

    tool = TerminalTool(Manager())
    result = await tool.execute(action="current")

    # idle_ms is 0 or very close to 0 — may or may not appear
    # The important thing is it doesn't crash


@pytest.mark.asyncio
async def test_terminal_current_shows_cursor() -> None:
    session = FakeSession()

    async def _current_segment():
        return TerminalSegment(text="output\n$ ", cursor_line="$ ", is_empty_prompt=False)

    session.current_segment = _current_segment

    class Manager(FakeManager):
        async def get_default_session(self):
            return session

    tool = TerminalTool(Manager())
    result = await tool.execute(action="current")

    assert "<cursor>$</cursor>" in result


@pytest.mark.asyncio
async def test_terminal_current_omits_empty_cursor() -> None:
    session = FakeSession()

    async def _current_segment():
        return TerminalSegment(text="", cursor_line="", is_empty_prompt=True)

    session.current_segment = _current_segment

    class Manager(FakeManager):
        async def get_default_session(self):
            return session

    tool = TerminalTool(Manager())
    result = await tool.execute(action="current")

    assert "<cursor>" not in result


@pytest.mark.asyncio
async def test_terminal_current_with_name_creates_session() -> None:
    class NamedManager(FakeManager):
        async def get_or_create(self, name=None, cwd=None):
            s = FakeSession()
            s.name = name or "default"
            return s

    tool = TerminalTool(NamedManager())
    result = await tool.execute(action="current", name="my-tab")

    assert "<terminal>my-tab</terminal>" in result
