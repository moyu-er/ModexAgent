"""Tool result_metadata hook — core declares no terminal knowledge (ADR-0006).

Tools that emit structured XML declare their own content metadata via
``Tool.result_metadata``; the ToolManager reads it and attaches it to the
ToolResult / tool message. Core never imports terminal-domain code.
"""

from __future__ import annotations

import asyncio
import inspect

from modex_agent.core.message import ContentFormat, TextPart
from modex_agent.core.tool_manager import Tool, ToolResult
from modex_agent.tools.manager import InMemoryToolManager


class _TerminalishTool(Tool):
    """Mirror of what real terminal tools do: declare XML truncation metadata."""

    def get_schema(self) -> dict:  # type: ignore[override]
        return {"name": "term", "parameters": {"type": "object"}}

    async def execute(self, **kwargs):  # type: ignore[override]
        return "<command_result><output>data</output></command_result>"

    def result_metadata(self, result) -> tuple[ContentFormat | None, list[str] | None]:  # type: ignore[override]
        from modex_agent.tools.terminal.types import terminal_result_metadata

        return terminal_result_metadata(result)


def test_tool_result_carries_declared_metadata() -> None:
    tool = _TerminalishTool(name="term")
    mgr = InMemoryToolManager()
    mgr.register(tool)
    out = asyncio.new_event_loop().run_until_complete(mgr.execute("term", {}))
    assert out.content_format is ContentFormat.XML
    assert out.truncatable_paths == ["output", "tui_screen", "cursor_line"]


def test_plain_tool_has_no_metadata() -> None:
    class _PlainTool(Tool):
        def get_schema(self) -> dict:  # type: ignore[override]
            return {"name": "plain", "parameters": {"type": "object"}}

        async def execute(self, **kwargs):  # type: ignore[override]
            return "just text"

    mgr = InMemoryToolManager()
    mgr.register(_PlainTool(name="plain"))
    out = asyncio.new_event_loop().run_until_complete(mgr.execute("plain", {}))
    assert out.content_format is None
    assert out.truncatable_paths is None
    # and to_message() must not add content_format keys for plain results
    msg = out.to_message()
    assert "content_format" not in msg
    assert "truncatable_paths" not in msg


def test_terminal_to_message_carries_metadata() -> None:
    res = ToolResult(
        tool_name="term",
        content=[TextPart(text="<command_result><output>x</output></command_result>")],
        content_format=ContentFormat.XML,
        truncatable_paths=["output", "tui_screen", "cursor_line"],
    )
    msg = res.to_message()
    assert msg["content_format"] == "xml"
    assert msg["truncatable_paths"] == ["output", "tui_screen", "cursor_line"]


def test_to_message_has_no_terminal_import() -> None:
    src = inspect.getsource(ToolResult.to_message)
    assert "modex_agent.tools.terminal" not in src
