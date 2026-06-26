"""Tests for message_builder helpers — interrupted assistant message."""

from xml.etree import ElementTree as ET

from modex_agent.agents.react.message_builder import build_interrupted_assistant_message
from modex_agent.core.message import ContentFormat


def _parse(content: str) -> ET.Element:
    """Wrap multi-line content in a root and parse (content has single root here)."""
    return ET.fromstring(content)


class TestBuildInterruptedAssistantMessage:
    def test_marks_xml_format_and_truncatable_content(self):
        msg = build_interrupted_assistant_message("partial text", [], "user_stop")
        assert msg.role == "assistant"
        assert msg.content_format == ContentFormat.XML
        assert msg.truncatable_paths == ["content"]

    def test_full_structure_with_content_and_tools(self):
        msg = build_interrupted_assistant_message(
            "partial text", ["read_file", "write_file"], "user_stop"
        )
        root = _parse(msg.content)
        assert root.tag == "interrupted_response"
        assert root.get("reason") == "user_stop"
        assert root.findtext("content") == "partial text"
        tools = [t.get("name") for t in root.findall("./pending_tools/tool")]
        assert tools == ["read_file", "write_file"]

    def test_omits_content_when_empty(self):
        msg = build_interrupted_assistant_message("", ["read_file"], "error")
        root = _parse(msg.content)
        assert root.find("content") is None
        assert [t.get("name") for t in root.findall("./pending_tools/tool")] == ["read_file"]

    def test_omits_pending_tools_when_empty(self):
        msg = build_interrupted_assistant_message("only content", [], "timeout")
        root = _parse(msg.content)
        assert root.findtext("content") == "only content"
        assert root.find("pending_tools") is None

    def test_escapes_special_characters_via_cdata(self):
        special = "hello <world> & 'quote'"
        msg = build_interrupted_assistant_message(special, [], "user_stop")
        root = _parse(msg.content)
        # Parsed text must round-trip the original (CDATA unwrapped by parser).
        assert root.findtext("content").strip() == special

    def test_minimal_when_nothing_produced(self):
        msg = build_interrupted_assistant_message("", [], "error")
        root = _parse(msg.content)
        assert root.tag == "interrupted_response"
        assert root.get("reason") == "error"
        assert root.find("content") is None
        assert root.find("pending_tools") is None
