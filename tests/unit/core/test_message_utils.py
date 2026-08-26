"""Tests for `recent_tool_usage` — bounded backward tool-usage scan."""

from __future__ import annotations

from modex_agent.core.message import ChatMessage
from modex_agent.core.message_utils import recent_tool_usage
from modex_agent.core.types import MessageRole

_TOOLS = frozenset({"task"})


def _assistant(content: str = "work") -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content=content)


def _user(content: str = "request") -> ChatMessage:
    return ChatMessage(role=MessageRole.USER, content=content)


def _tool(name: str) -> ChatMessage:
    return ChatMessage(role=MessageRole.TOOL, name=name, content="result")


def test_empty_history_is_no_usage() -> None:
    assert recent_tool_usage([], _TOOLS) is False


def test_tool_call_inside_window_is_detected() -> None:
    messages = [_assistant(), _tool("task"), _assistant(), _user()]
    assert recent_tool_usage(messages, _TOOLS) is True


def test_tool_call_beyond_window_is_ignored() -> None:
    messages = [
        _assistant(),
        _tool("task"),
        _assistant(),
        _assistant(),
        _assistant(),
        _user(),
    ]
    assert recent_tool_usage(messages, _TOOLS) is False


def test_boundary_call_between_third_and_fourth_assistant_counts() -> None:
    messages = [
        _assistant(),
        _assistant(),
        _tool("task"),
        _assistant(),
        _user(),
    ]
    assert recent_tool_usage(messages, _TOOLS) is True


def test_other_tool_name_is_ignored() -> None:
    messages = [_assistant(), _tool("read"), _assistant(), _user()]
    assert recent_tool_usage(messages, _TOOLS) is False


def test_multi_name_set_matches_any() -> None:
    messages = [_assistant(), _tool("todo_write"), _user()]
    assert recent_tool_usage(messages, frozenset({"todo_write", "todo_read"})) is True


def test_window_one_covers_only_latest_assistant() -> None:
    messages = [_assistant(), _tool("task")]
    assert recent_tool_usage(messages, _TOOLS, window=1) is True
    messages = [_tool("task"), _assistant()]
    assert recent_tool_usage(messages, _TOOLS, window=1) is False


def test_history_without_assistant_messages_scans_to_end() -> None:
    messages = [_user(), _tool("task"), _user()]
    assert recent_tool_usage(messages, _TOOLS) is True
