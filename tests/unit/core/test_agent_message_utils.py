from __future__ import annotations

from modex_agent.core.message_utils import (
    normalize_agent_messages_for_llm,
)
from modex_agent.core.types import MessageRole


def test_normalize_agent_messages_converts_role_to_xml_format() -> None:
    messages = [
        {
            "role": MessageRole.AGENT,
            "source_agent": "subagent-a",
            "content": "hello",
        }
    ]

    converted = normalize_agent_messages_for_llm(messages)

    assert converted[0]["role"] == MessageRole.USER
    assert converted[0]["content_format"] == "xml"
    assert converted[0]["truncatable_paths"] == ["content"]
    assert '<agent_message source="subagent-a">' in converted[0]["content"]
    assert "<content>hello</content>" in converted[0]["content"]
    assert "</agent_message>" in converted[0]["content"]


def test_normalize_agent_messages_replaces_system_reminder_role_with_user() -> None:
    """SYSTEM_REMINDER messages are passed through as USER with content
    untouched — the <system-reminder> envelope is already applied at storage
    time via wrap_system_reminder."""
    messages = [
        {
            "role": MessageRole.SYSTEM_REMINDER,
            "content": "<system-reminder>\nKeep going.\n</system-reminder>",
        }
    ]

    converted = normalize_agent_messages_for_llm(messages)

    assert converted[0]["role"] == MessageRole.USER
    assert "<system-reminder>" in converted[0]["content"]
    assert "Keep going." in converted[0]["content"]
