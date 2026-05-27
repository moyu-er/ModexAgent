"""Unit tests for framework/memory/sanitizer.py.

TDD: verify DefaultSessionToolChainSanitizer behaviors after
relocation from framework/memory/compression/tool_chain_sanitizer.py.
The Protocol class SessionToolChainSanitizer has been removed;
DefaultSessionToolChainSanitizer is now a standalone concrete class.
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


def test_sanitizer_preserves_complete_tool_chain() -> None:
    """A complete assistant(tool_calls) -> tool result pair is kept intact."""
    messages = [
        {"role": str(MessageRole.USER), "content": "do something"},
        _assistant_tool_call("a"),
        _tool("a", "done"),
        {"role": str(MessageRole.ASSISTANT), "content": "finished"},
    ]

    result = DefaultSessionToolChainSanitizer().sanitize(
        messages,
        mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
    )

    assert result.messages == messages
    assert result.removed_messages == []
    assert result.removed_indices == set()
    assert result.issues == []
    assert result.has_open_tail is False


def test_sanitizer_removes_orphan_tool_result() -> None:
    """A tool result with no matching assistant tool_call is removed."""
    messages = [
        {"role": str(MessageRole.USER), "content": "hi"},
        _tool("orphan_id"),
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
    assert result.removed_indices == {1}
    assert [issue.reason for issue in result.issues] == [
        ToolChainSanitizationReason.ORPHAN_TOOL_RESULT,
    ]
    assert result.has_open_tail is False


def test_sanitizer_preserves_active_open_tail() -> None:
    """An incomplete last assistant with tool_calls (no closing plain assistant)
    is preserved in PERSISTENT_SESSION mode as an active open tail."""
    messages = [
        {"role": str(MessageRole.USER), "content": "start"},
        _assistant_tool_call("a", "b"),
        _tool("a", "partial"),
    ]

    result = DefaultSessionToolChainSanitizer().sanitize(
        messages,
        mode=ToolChainSanitizationMode.PERSISTENT_SESSION,
    )

    assert result.messages == messages
    assert result.removed_messages == []
    assert result.removed_indices == set()
    assert result.has_open_tail is True
    assert result.open_tail_assistant_index == 1
