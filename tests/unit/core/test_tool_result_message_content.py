"""Unit tests for ToolResult.message_content() — unified content rendering."""

from __future__ import annotations

from modex_agent.core.message import ContentFormat
from modex_agent.core.tool_manager import ToolResult


class TestMessageContent:
    def test_success_returns_result(self):
        r = ToolResult(tool_name="t", result="hello")
        assert r.message_content() == "hello"

    def test_success_none_result_returns_empty(self):
        r = ToolResult(tool_name="t", result=None)
        assert r.message_content() == ""

    def test_plain_error_returns_prefixed(self):
        r = ToolResult(tool_name="t", result=None, error="bad thing")
        assert r.message_content() == "Error: bad thing"

    def test_xml_result_with_error_returns_xml_verbatim(self):
        xml = "<tool_timeout><status>timed_out</status></tool_timeout>"
        r = ToolResult(
            tool_name="t",
            result=xml,
            error="Tool execution timed out after 120 seconds",
            content_format=ContentFormat.XML,
            truncatable_paths=[],
        )
        assert r.message_content() == xml

    def test_xml_result_without_error_returns_xml(self):
        xml = "<command_result><status>completed</status></command_result>"
        r = ToolResult(
            tool_name="t",
            result=xml,
            content_format=ContentFormat.XML,
            truncatable_paths=["output"],
        )
        assert r.message_content() == xml

    def test_plain_result_with_error_returns_error(self):
        r = ToolResult(tool_name="t", result="partial", error="failed")
        assert r.message_content() == "Error: failed"
