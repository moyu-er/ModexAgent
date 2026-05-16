from __future__ import annotations

from framework.memory.archive_input import DefaultArchiveInputPolicy
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext


def test_tool_chain_is_grouped_with_assistant_tool_calls() -> None:
    policy = DefaultArchiveInputPolicy()
    messages = [
        {"role": "user", "content": "check tests"},
        {
            "role": "assistant",
            "content": "I will run tests.",
            "tool_calls": [
                {
                    "id": "call_123456",
                    "function": {
                        "name": "shell",
                        "arguments": "{\"command\":\"pytest tests/unit -q\",\"unused\":\"drop me\"}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_123456",
            "name": "shell",
            "content": "FAILED test_a\n" + ("x" * 1300) + "\nshort summary tail",
        },
    ]

    result = policy.build_inputs(
        messages,
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    assert "[tool-chain]" in result.context_transcript
    assert "name=shell" in result.context_transcript
    assert "command=pytest tests/unit -q" in result.context_transcript
    assert "unused" not in result.context_transcript
    assert "truncated" in result.context_transcript
    assert "short summary tail" in result.context_transcript
    assert "source: shell" in result.knowledge_transcript


def test_orphan_tool_result_is_dropped() -> None:
    policy = DefaultArchiveInputPolicy()
    result = policy.build_inputs(
        [{"role": "tool", "tool_call_id": "missing", "name": "shell", "content": "noise"}],
        MemoryContext(session_id="s1"),
        CompressionReason.MESSAGE_COUNT,
    )

    assert result.context_transcript == ""
    assert result.knowledge_transcript == ""
    assert result.stats.dropped_messages == 1


def test_system_and_developer_messages_are_excluded() -> None:
    policy = DefaultArchiveInputPolicy()
    result = policy.build_inputs(
        [
            {"role": "system", "content": "system rule"},
            {"role": "developer", "content": "developer rule"},
            {"role": "user", "content": "real request"},
        ],
        MemoryContext(session_id="s1"),
        CompressionReason.MANUAL,
    )

    assert "system rule" not in result.context_transcript
    assert "developer rule" not in result.context_transcript
    assert "[user]" in result.context_transcript
    assert "real request" in result.knowledge_transcript
