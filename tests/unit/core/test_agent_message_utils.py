from __future__ import annotations

from modex_agent.core.message_utils import (
    normalize_agent_messages_for_llm,
)
from modex_agent.core.types import MessageRole


def test_normalize_agent_messages_converts_agent_role_to_user_pure() -> None:
    """AGENT messages become USER with content byte-identical to input —
    no <agent_message> envelope, no content_format/truncatable_paths added."""
    messages = [
        {
            "role": MessageRole.AGENT,
            "source_agent": "subagent-a",
            "content": "hello",
        }
    ]

    converted = normalize_agent_messages_for_llm(messages)

    assert converted[0]["role"] == MessageRole.USER
    assert converted[0]["content"] == "hello"
    assert converted[0]["source_agent"] == "subagent-a"
    assert "content_format" not in converted[0]
    assert "truncatable_paths" not in converted[0]
    assert "<agent_message" not in converted[0]["content"]


def test_normalize_agent_messages_agent_content_with_xml_chars_passes_through() -> None:
    """AGENT content containing </>& passes through unescaped and un-wrapped,
    proving the legacy XML envelope branch is gone."""
    raw = "a < b & c > d <tag>"
    messages = [
        {
            "role": MessageRole.AGENT,
            "source_agent": "planner",
            "content": raw,
        }
    ]

    converted = normalize_agent_messages_for_llm(messages)

    assert converted[0]["role"] == MessageRole.USER
    assert converted[0]["content"] == raw
    assert "<agent_message" not in converted[0]["content"]
    assert "&lt;" not in converted[0]["content"]
    assert "&gt;" not in converted[0]["content"]
    assert "&amp;" not in converted[0]["content"]


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
