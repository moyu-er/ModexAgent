"""Unit tests for session tool-chain sanitizer.

TDD: verify DefaultSessionToolChainSanitizer behaviors
including orphan removal, stale incomplete group removal,
open tail preservation, model-visible mode, duplicates,
and multi-agent agent-message interleaving.
"""

from __future__ import annotations

from framework.core.types import MessageRole
from framework.memory.sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
    ToolChainSanitizationReason,
)


def _assistant_tool_call(*call_ids: str) -> dict:
    return {
        "role": str(MessageRole.ASSISTANT),
        "content": "",
        "tool_calls": [
            {"id": call_id, "function": {"name": f"tool_{call_id}"}}
            for call_id in call_ids
        ],
    }


def _tool(call_id: str, content: str = "result") -> dict:
    return {
        "role": str(MessageRole.TOOL),
        "tool_call_id": call_id,
        "content": content,
    }


def _agent(sender: str, content: str = "message") -> dict:
    return {
        "role": str(MessageRole.AGENT),
        "content": content,
        "source_agent": sender,
    }


def test_persistent_mode_removes_orphan_tool_result() -> None:
    messages = [
        {"role": str(MessageRole.USER), "content": "hi"},
        _tool("missing"),
        {"role": str(MessageRole.ASSISTANT), "content": "done"},
    ]

    result = DefaultSessionToolChainSanitizer().sanitize(
        messages,
        mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
    )

    assert [msg["role"] for msg in result.messages] == [
        str(MessageRole.USER),
        str(MessageRole.ASSISTANT),
    ]
    assert result.removed_messages == [_tool("missing")]
    assert result.removed_indices == {1}
    assert [issue.reason for issue in result.issues] == [
        ToolChainSanitizationReason.ORPHAN_TOOL_RESULT,
    ]
    assert result.has_open_tail is False


def test_persistent_mode_removes_stale_incomplete_non_tail_assistant_and_partial_tool() -> None:
    messages = [
        {"role": str(MessageRole.USER), "content": "start"},
        _assistant_tool_call("a", "b"),
        _tool("a", "partial"),
        {"role": str(MessageRole.ASSISTANT), "content": "continued without b"},
        _assistant_tool_call("c"),
        _tool("c", "complete"),
    ]

    result = DefaultSessionToolChainSanitizer().sanitize(
        messages,
        mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
    )

    assert result.messages == [
        {"role": str(MessageRole.USER), "content": "start"},
        {"role": str(MessageRole.ASSISTANT), "content": "continued without b"},
        _assistant_tool_call("c"),
        _tool("c", "complete"),
    ]
    assert result.removed_indices == {1, 2}
    assert [issue.reason for issue in result.issues] == [
        ToolChainSanitizationReason.STALE_INCOMPLETE_ASSISTANT_TOOL_CALLS,
        ToolChainSanitizationReason.PARTIAL_TOOL_RESULTS_REMOVED,
    ]
    assert result.has_open_tail is False


def test_persistent_mode_preserves_last_incomplete_assistant_as_open_tail() -> None:
    messages = [
        {"role": str(MessageRole.USER), "content": "start"},
        _assistant_tool_call("a", "b"),
        _tool("a", "partial"),
        {"role": str(MessageRole.USER), "content": "new user while tool b is missing"},
    ]

    result = DefaultSessionToolChainSanitizer().sanitize(
        messages,
        mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
    )

    assert result.messages == messages
    assert result.removed_messages == []
    assert result.has_open_tail is True
    assert result.open_tail_assistant_index == 1


def test_model_visible_mode_removes_last_incomplete_assistant_and_partial_tool() -> None:
    messages = [
        {"role": str(MessageRole.USER), "content": "start"},
        _assistant_tool_call("a", "b"),
        _tool("a", "partial"),
        {"role": str(MessageRole.USER), "content": "new user"},
    ]

    result = DefaultSessionToolChainSanitizer().sanitize(
        messages,
        mode=ToolChainSanitizationMode.MODEL_VISIBLE_CONTEXT,
    )

    assert result.messages == [
        {"role": str(MessageRole.USER), "content": "start"},
        {"role": str(MessageRole.USER), "content": "new user"},
    ]
    assert result.removed_indices == {1, 2}
    assert result.has_open_tail is False


def test_duplicate_tool_result_keeps_first_and_removes_later_duplicate() -> None:
    messages = [
        _assistant_tool_call("a"),
        _tool("a", "first"),
        _tool("a", "duplicate"),
    ]

    result = DefaultSessionToolChainSanitizer().sanitize(
        messages,
        mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
    )

    assert result.messages == [_assistant_tool_call("a"), _tool("a", "first")]
    assert result.removed_messages == [_tool("a", "duplicate")]
    assert result.removed_indices == {2}
    assert [issue.reason for issue in result.issues] == [
        ToolChainSanitizationReason.DUPLICATE_TOOL_RESULT,
    ]


def test_persistent_mode_preserves_agent_messages_interleaved_with_tool_chain() -> None:
    """Multi-agent: role=agent messages must be preserved and not disturb
    assistant/tool group classification."""
    messages = [
        {"role": str(MessageRole.USER), "content": "start"},
        _assistant_tool_call("a", "b"),
        _agent("subagent1", "forwarded from subagent"),
        _tool("a", "result_a"),
        _tool("b", "result_b"),
        {"role": str(MessageRole.ASSISTANT), "content": "done"},
    ]

    result = DefaultSessionToolChainSanitizer().sanitize(
        messages,
        mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
    )

    assert result.messages == messages
    assert result.removed_messages == []
    assert result.removed_indices == set()
    assert result.has_open_tail is False
