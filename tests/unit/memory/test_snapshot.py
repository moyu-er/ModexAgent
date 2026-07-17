"""Tests for shared snapshot helpers (context-fork + experience-review convergence)."""

from __future__ import annotations

from modex_agent.core.message import ChatMessage
from modex_agent.memory.snapshot import (
    DEFAULT_SNAPSHOT_MAX_CONTENT_LEN,
    DEFAULT_SNAPSHOT_MAX_MESSAGES,
    format_snapshot_text,
    format_snapshot_xml,
)


def _dict_msg(role: str, content: str, *, name: str | None = None) -> dict[str, object]:
    m: dict[str, object] = {"role": role, "content": content}
    if name is not None:
        m["name"] = name
    return m


class TestFormatSnapshotText:
    def test_empty_returns_empty_string(self) -> None:
        assert format_snapshot_text([]) == ""

    def test_dict_messages_format(self) -> None:
        result = format_snapshot_text(
            [_dict_msg("user", "Hello"), _dict_msg("assistant", "Hi")]
        )
        assert result == "[user]: Hello\n[assistant]: Hi"

    def test_object_messages_format(self) -> None:
        result = format_snapshot_text(
            [ChatMessage(role="user", content="Hello"), ChatMessage(role="assistant", content="Hi")]
        )
        assert result == "[user]: Hello\n[assistant]: Hi"

    def test_window_truncation_keeps_last_n(self) -> None:
        msgs = [_dict_msg("user", f"m{i}") for i in range(10)]
        result = format_snapshot_text(msgs, max_messages=3)
        assert result == "[user]: m7\n[user]: m8\n[user]: m9"

    def test_content_truncation_appends_marker(self) -> None:
        long_content = "x" * 3000
        result = format_snapshot_text([_dict_msg("user", long_content)], max_content_len=100)
        assert result.startswith("[user]: " + "x" * 100 + " [truncated]")
        assert len(result) < len(long_content)

    def test_content_under_limit_not_truncated(self) -> None:
        result = format_snapshot_text([_dict_msg("user", "short")], max_content_len=100)
        assert result == "[user]: short"

    def test_empty_content_skipped(self) -> None:
        result = format_snapshot_text(
            [_dict_msg("user", ""), _dict_msg("assistant", "Hi"), _dict_msg("user", "   ")]
        )
        assert result == "[assistant]: Hi"

    def test_default_constants(self) -> None:
        assert DEFAULT_SNAPSHOT_MAX_MESSAGES == 100
        assert DEFAULT_SNAPSHOT_MAX_CONTENT_LEN == 2000


class TestFormatSnapshotXml:
    def test_empty_messages_placeholder_xml(self) -> None:
        result = format_snapshot_xml([], "main")
        assert '<forked_context source="main">' in result
        assert "Inherited 0 messages" in result
        assert "</forked_context>" in result

    def test_dict_messages_xml(self) -> None:
        result = format_snapshot_xml(
            [_dict_msg("user", "Hello"), _dict_msg("assistant", "Hi")], "parent"
        )
        assert '<forked_context source="parent">' in result
        assert "Inherited 2 messages" in result
        assert '<message index="0" role="user">' in result
        assert "<![CDATA[Hello]]>" in result
        assert '<message index="1" role="assistant">' in result
        assert "<![CDATA[Hi]]>" in result

    def test_object_messages_xml(self) -> None:
        result = format_snapshot_xml(
            [ChatMessage(role="user", content="Hello")], "main"
        )
        assert '<message index="0" role="user">' in result
        assert "<![CDATA[Hello]]>" in result

    def test_tool_message_includes_name_attr(self) -> None:
        result = format_snapshot_xml(
            [_dict_msg("tool", "result", name="search")], "main"
        )
        assert '<message index="0" role="tool" name="search">' in result

    def test_tool_message_without_name_no_attr(self) -> None:
        result = format_snapshot_xml([_dict_msg("tool", "result")], "main")
        assert '<message index="0" role="tool">' in result
        assert "name=" not in result

    def test_content_truncation_in_xml(self) -> None:
        long_content = "x" * 3000
        result = format_snapshot_xml(
            [_dict_msg("user", long_content)], "main", max_content_len=100
        )
        assert "x" * 100 + " [truncated]" in result
        assert "x" * 101 not in result.split("[truncated]")[0]

    def test_no_window_truncation_in_xml(self) -> None:
        msgs = [_dict_msg("user", f"m{i}") for i in range(200)]
        result = format_snapshot_xml(msgs, "main")
        assert "Inherited 200 messages" in result
        assert "m199" in result
        assert "m0" in result
