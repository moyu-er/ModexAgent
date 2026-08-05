"""Tests for LossyContentCompactionGovernance."""

from __future__ import annotations

from unittest.mock import MagicMock

from modex_agent.core.agent import AgentContext
from modex_agent.core.types import MessageRole
from modex_agent.memory.context_governance import (
    META_CONTEXT_LOSSY,
    META_CONTEXT_REDUCTION,
    ContextReductionType,
    LossyContentCompactionGovernance,
)

_CTX: MagicMock = MagicMock(spec=AgentContext)


def _with_fillers(target: dict[str, object], total: int = 70) -> list[dict[str, object]]:
    """Return a list starting with *target* and enough fillers to trigger
    the default compaction step (compact_range_count=50, compact_buffer=20).
    """
    return [target] + [
        {"role": str(MessageRole.USER), "content": "filler"} for _ in range(total - 1)
    ]


async def test_lossy_compaction_truncates_tool_result() -> None:
    messages = _with_fillers(
        {"role": MessageRole.TOOL, "tool_call_id": "t1", "name": "search", "content": "t" * 500}
    )
    messages.insert(
        0,
        {"role": MessageRole.AGENT, "source_agent": "subagent", "content": "[From Agent subagent]\n" + "a" * 200},
    )
    # Now index 0=agent, index 1=tool, both within first compaction step.
    gov = LossyContentCompactionGovernance(
        tool_result_head_chars=20,
        assistant_head_chars=20,
        agent_head_chars=80,
        user_head_chars=120,
    )

    result = await gov.apply(messages, _CTX)

    tool = result[1]
    agent = result[0]
    assert tool[META_CONTEXT_LOSSY] is True
    assert tool[META_CONTEXT_REDUCTION] == ContextReductionType.TOOL_RESULT_TRUNCATED
    assert len(str(tool["content"])) < len("t" * 500)
    assert agent["content"].startswith("[From Agent subagent]\n")


async def test_lossy_does_not_mutate_input() -> None:
    messages = _with_fillers(
        {"role": MessageRole.TOOL, "tool_call_id": "t1", "name": "search", "content": "t" * 500}
    )
    original_content = messages[0]["content"]
    gov = LossyContentCompactionGovernance(tool_result_head_chars=20)

    result = await gov.apply(messages, _CTX)

    assert messages[0]["content"] == original_content
    assert result[0]["content"] != original_content


async def test_lossy_truncates_tool_args_json_aware() -> None:
    """Oversized tool_calls JSON arguments: long values shortened, metadata fields added."""
    import json

    huge_value = "x" * 5000
    args = json.dumps({"content": huge_value, "path": "/tmp/out.md"})
    target = {
        "role": MessageRole.ASSISTANT,
        "content": "let me write",
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": args}},
        ],
    }
    messages = _with_fillers(target)

    gov = LossyContentCompactionGovernance(tool_args_head_chars=200)

    result = await gov.apply(messages, _CTX)

    truncated_args = result[0]["tool_calls"][0]["function"]["arguments"]
    assert len(truncated_args) < len(args)
    parsed = json.loads(truncated_args)
    assert isinstance(parsed, dict)
    assert parsed["path"] == "/tmp/out.md"  # short value untouched
    assert parsed["_gv_truncated"] is True
    assert "truncated" in parsed["_gv_truncation_info"]
    assert len(parsed["content"]) < len(huge_value)  # content shortened
    assert META_CONTEXT_LOSSY in result[0]


async def test_lossy_skips_invalid_json_tool_args() -> None:
    """Non-JSON tool call arguments are left untouched (don't make them worse)."""
    bad_args = "{not valid json at all"
    target = {
        "role": MessageRole.ASSISTANT,
        "content": "",
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "bad_tool", "arguments": bad_args}},
        ],
    }
    messages = _with_fillers(target)
    gov = LossyContentCompactionGovernance(tool_args_head_chars=10)

    result = await gov.apply(messages, _CTX)

    assert result[0]["tool_calls"][0]["function"]["arguments"] == bad_args
    assert META_CONTEXT_LOSSY not in result[0]


async def test_lossy_skips_small_tool_args() -> None:
    """Small tool_calls arguments below the limit are left untouched."""
    target = {
        "role": MessageRole.ASSISTANT,
        "content": "let me search",
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": '{"query": "ok"}'}},
        ],
    }
    messages = _with_fillers(target)
    gov = LossyContentCompactionGovernance(tool_args_head_chars=2048)

    result = await gov.apply(messages, _CTX)

    assert result[0]["tool_calls"][0]["function"]["arguments"] == '{"query": "ok"}'
    assert META_CONTEXT_LOSSY not in result[0]
