from __future__ import annotations

from framework.core.types import MessageRole
from framework.memory.context_governance import (
    META_CONTEXT_LOSSY,
    META_CONTEXT_REDUCTION,
    ContextReductionType,
    FinalContextLegalityGovernance,
    LossyContentCompactionGovernance,
    PriorityBudgetGovernance,
)
from framework.memory.retention import DefaultMessageRetentionPolicy


async def test_priority_budget_keeps_user_before_agent() -> None:
    messages = [
        {"role": MessageRole.AGENT, "source_agent": "peer", "content": "[From Agent peer]\n" + "agent" * 500},
        {"role": MessageRole.USER, "content": "human"},
    ]
    gov = PriorityBudgetGovernance(
        max_tokens=8,
        safety_buffer=0,
        retention_policy=DefaultMessageRetentionPolicy(),
    )

    result = await gov.apply(messages)

    assert any(msg.get("content") == "human" for msg in result)
    assert any(msg.get("role") == MessageRole.AGENT for msg in result)


async def test_lossy_compaction_reduces_tool_before_agent() -> None:
    messages = [
        {"role": MessageRole.AGENT, "source_agent": "peer", "content": "[From Agent peer]\n" + "a" * 200},
        {"role": MessageRole.TOOL, "tool_call_id": "t1", "name": "search", "content": "t" * 500},
    ]
    gov = LossyContentCompactionGovernance(
        tool_result_head_chars=20,
        assistant_head_chars=20,
        agent_head_chars=80,
        user_head_chars=120,
    )

    result = await gov.apply(messages)

    tool = result[1]
    agent = result[0]
    assert tool[META_CONTEXT_LOSSY] is True
    assert tool[META_CONTEXT_REDUCTION] == ContextReductionType.TOOL_RESULT_TRUNCATED
    assert len(str(tool["content"])) < len("t" * 500)
    assert agent["content"].startswith("[From Agent peer]\n")


async def test_lossy_governance_does_not_mutate_input_messages() -> None:
    messages = [{"role": MessageRole.TOOL, "tool_call_id": "t1", "name": "search", "content": "t" * 500}]
    original_content = messages[0]["content"]
    gov = LossyContentCompactionGovernance(tool_result_head_chars=20)

    result = await gov.apply(messages)

    assert messages[0]["content"] == original_content
    assert result[0]["content"] != original_content


async def test_final_legality_passes_messages_through_unchanged() -> None:
    """FinalContextLegality is a no-op — see test_context_governance.py."""
    messages = [
        {"role": MessageRole.TOOL, "tool_call_id": "missing", "content": "orphan"},
        {"role": MessageRole.USER, "content": "hello"},
    ]
    gov = FinalContextLegalityGovernance()

    result = await gov.apply(messages)

    assert result == messages


async def test_priority_budget_preserves_recent_user_and_agent_anchors() -> None:
    messages = [
        {"role": MessageRole.USER, "content": "old human " * 200},
        {"role": MessageRole.AGENT, "source_agent": "peer", "content": "[From Agent peer]\nold agent " * 200},
        {"role": MessageRole.USER, "content": "recent human"},
        {"role": MessageRole.AGENT, "source_agent": "peer", "content": "[From Agent peer]\nrecent agent"},
    ]
    gov = PriorityBudgetGovernance(
        max_tokens=8,
        safety_buffer=0,
        retention_policy=DefaultMessageRetentionPolicy(),
        min_recent_user_turns=1,
        min_recent_agent_turns=1,
    )

    result = await gov.apply(messages)

    assert any(msg.get("content") == "recent human" for msg in result)
    assert any(msg.get("content") == "[From Agent peer]\nrecent agent" for msg in result)


async def test_priority_budget_preserves_consecutive_recent_user_inputs_when_configured() -> None:
    messages = [
        {"role": MessageRole.USER, "content": "old human " * 200},
        {"role": MessageRole.ASSISTANT, "content": "old answer " * 200},
        {"role": MessageRole.USER, "content": "recent part 1"},
        {"role": MessageRole.USER, "content": "recent part 2"},
        {"role": MessageRole.ASSISTANT, "content": "working " * 200},
    ]
    gov = PriorityBudgetGovernance(
        max_tokens=8,
        safety_buffer=0,
        retention_policy=DefaultMessageRetentionPolicy(),
        min_recent_user_turns=2,
        min_recent_agent_turns=0,
    )

    result = await gov.apply(messages)

    assert [msg["content"] for msg in result if msg.get("role") == MessageRole.USER] == [
        "recent part 1",
        "recent part 2",
    ]


async def test_final_legality_returns_messages_unchanged_when_tool_result_missing() -> None:
    """FinalContextLegality is a no-op; incomplete groups are removed by
    ToolChainRepairGovernance earlier in the chain."""
    messages = [
        {
            "role": MessageRole.ASSISTANT,
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "search"}}],
        },
        {"role": MessageRole.USER, "content": "next"},
    ]
    gov = FinalContextLegalityGovernance()

    result = await gov.apply(messages)

    assert result == messages


async def test_final_legality_returns_messages_unchanged_for_multi_tool_call() -> None:
    """FinalContextLegality is a no-op regardless of call count."""
    messages = [
        {
            "role": MessageRole.ASSISTANT,
            "content": "",
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "search"}},
                {"id": "call-2", "type": "function", "function": {"name": "read_file"}},
            ],
        },
        {"role": MessageRole.USER, "content": "next"},
    ]
    gov = FinalContextLegalityGovernance()

    result = await gov.apply(messages)

    assert result == messages
