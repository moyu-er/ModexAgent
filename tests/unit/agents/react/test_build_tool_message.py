"""Tests for ``build_tool_message`` result-message construction.

Migrated from tests/unit/media/test_multimedia_pipeline.py (2026-09) when the
dormant MediaProcessor family was removed — these tests cover the live
tool-result message builder, which merely lived in the same legacy file.
"""

from __future__ import annotations

from modex_agent.agents.react.message_builder import build_tool_message
from modex_agent.core.message import ContentFormat, TextPart
from modex_agent.core.tool_manager import ToolResult


class TestBuildToolMessage:
    def test_short_result_not_truncated(self):
        result = ToolResult.from_text("test", "short output")
        msg = build_tool_message(result)
        assert msg.content == [TextPart(text="short output")]

    def test_long_result_not_truncated(self):
        long_content = "x" * 30000
        result = ToolResult.from_text("test", long_content)
        msg = build_tool_message(result)
        assert msg.content == [TextPart(text=long_content)]
        assert len(msg.content[0].text) == 30000

    def test_error_not_truncated(self):
        result = ToolResult(tool_name="test", error="something failed")
        msg = build_tool_message(result)
        assert msg.content == "Error: something failed"

    def test_empty_result_gets_space(self):
        result = ToolResult(tool_name="test")
        msg = build_tool_message(result)
        assert msg.content == " "

    def test_terminal_xml_sets_metadata(self):
        xml_content = (
            "<command_result>"
            "<terminal>default</terminal>"
            "<output>hello</output>"
            "<status>completed</status>"
            "</command_result>"
        )
        result = ToolResult(
            tool_name="bash",
            content=[TextPart(text=xml_content)],
            content_format=ContentFormat.XML,
            truncatable_paths=["output", "tui_screen", "cursor_line"],
        )
        msg = build_tool_message(result)
        assert msg.content_format == ContentFormat.XML
        assert msg.truncatable_paths == ["output", "tui_screen", "cursor_line"]

    def test_plain_text_no_metadata(self):
        result = ToolResult.from_text("grep", "Found 3 matches")
        msg = build_tool_message(result)
        assert msg.content_format == ContentFormat.PLAIN
        assert msg.truncatable_paths is None
