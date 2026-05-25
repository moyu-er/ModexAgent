from __future__ import annotations

import pytest

from framework.tools.terminal.results import TerminalSegment
from framework.tools.terminal.tool import TerminalTool


class FakeSession:
    name = "default"

    async def current_segment(self) -> TerminalSegment:
        return TerminalSegment(text="$ ", cursor_line="$ ", is_empty_prompt=True)

    async def interrupt(self) -> None:
        pass


class FakeManager:
    async def get_default(self):
        return FakeSession()

    async def get_default_session(self):
        return FakeSession()

    async def get_or_create(self, name=None, workdir=None):
        return FakeSession()

    async def list_sessions(self):
        return []

    def list_names(self):
        return ["default"]


@pytest.mark.asyncio
async def test_terminal_current_returns_empty_prompt_as_current_segment() -> None:
    tool = TerminalTool(FakeManager())

    result = await tool.execute(action="current")

    assert "Current terminal segment" in result
    assert "$ " in result
    assert "empty_prompt=True" in result
