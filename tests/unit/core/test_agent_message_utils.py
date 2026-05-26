from __future__ import annotations

from framework.core.message_utils import (
    normalize_agent_messages_for_llm,
)
from framework.core.types import MessageRole


def test_normalize_agent_messages_converts_role_without_duplicate_prefix() -> None:
    messages = [
        {
            "role": MessageRole.AGENT,
            "source_agent": "subagent-a",
            "content": '<agent_message source="subagent-a"><content>hello</content></agent_message>',
        }
    ]

    converted, has_agent = normalize_agent_messages_for_llm(messages)

    assert has_agent is True
    assert converted == [{
        "role": MessageRole.USER,
        "content": '<agent_message source="subagent-a"><content>hello</content></agent_message>',
    }]
