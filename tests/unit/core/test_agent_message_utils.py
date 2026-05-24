from __future__ import annotations

from framework.core.message_utils import (
    ensure_agent_source_prefix,
    normalize_agent_messages_for_llm,
)
from framework.core.types import MessageRole


def test_ensure_agent_source_prefix_for_string_is_idempotent() -> None:
    content = "hello"
    first = ensure_agent_source_prefix(content, "subagent-a")
    second = ensure_agent_source_prefix(first, "subagent-a")

    assert first == "[From Agent subagent-a]\nhello"
    assert second == first


def test_ensure_agent_source_prefix_for_multimodal_inserts_text_block() -> None:
    content = [{"type": "image_url", "image_url": {"url": "x"}}]

    result = ensure_agent_source_prefix(content, "subagent-a")

    assert isinstance(result, list)
    assert result[0] == {"type": "text", "text": "[From Agent subagent-a]\n"}
    assert result[1] == {"type": "image_url", "image_url": {"url": "x"}}


def test_normalize_agent_messages_converts_role_without_duplicate_prefix() -> None:
    messages = [
        {
            "role": MessageRole.AGENT,
            "source_agent": "subagent-a",
            "content": "[From Agent subagent-a]\nhello",
        }
    ]

    converted, has_agent = normalize_agent_messages_for_llm(messages)

    assert has_agent is True
    assert converted == [{"role": MessageRole.USER, "content": "[From Agent subagent-a]\nhello"}]
