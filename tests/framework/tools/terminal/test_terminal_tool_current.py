from __future__ import annotations

import pytest

from framework.tools.terminal.results import TerminalSegment
from framework.tools.terminal.tool import TerminalTool


class FakeSession:
    name = "default"
    created_at = 0.0
    _busy_after_timeout = False
    _last_status = None

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
    assert "$" in result


@pytest.mark.asyncio
async def test_terminal_current_returns_none_when_no_session() -> None:
    class EmptyManager(FakeManager):
        async def get_default_session(self):
            return None

    tool = TerminalTool(EmptyManager())

    result = await tool.execute(action="current")

    assert "<terminal_result>" in result
    assert "<status>none</status>" in result
    assert "No terminal is active" in result
