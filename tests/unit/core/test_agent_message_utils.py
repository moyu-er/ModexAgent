from __future__ import annotations

from framework.core.message_utils import (
    normalize_agent_messages_for_llm,
)
from framework.core.types import MessageRole


def test_normalize_agent_messages_converts_role_to_xml_format() -> None:
    messages = [
        {
            "role": MessageRole.AGENT,
            "source_agent": "subagent-a",
            "content": "hello",
        }
    ]

    converted, has_agent = normalize_agent_messages_for_llm(messages)

    assert has_agent is True
    assert converted[0]["role"] == MessageRole.USER
    assert converted[0]["content_format"] == "xml"
    assert converted[0]["truncatable_paths"] == ["content"]
    assert '<agent_message source="subagent-a">' in converted[0]["content"]
    assert "<content>hello</content>" in converted[0]["content"]
    assert "</agent_message>" in converted[0]["content"]
