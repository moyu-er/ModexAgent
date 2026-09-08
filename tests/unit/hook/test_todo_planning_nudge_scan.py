"""Tests for the todo planning nudge's bounded backward in-turn scan."""

from __future__ import annotations

from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.hook.builtin.todo_planning_nudge import (
    ToolNudgeVerdict,
    scan_tool_usage_in_turn,
)

_TOOLS = frozenset({"task"})


def _assistant(content: str = "work") -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content=content)


def _user(content: str = "request") -> ChatMessage:
    return ChatMessage(role=MessageRole.USER, content=content)


def _agent(content: str = "external") -> ChatMessage:
    return ChatMessage(role=MessageRole.AGENT, content=content)


def _tool(name: str) -> ChatMessage:
    return ChatMessage(role=MessageRole.TOOL, name=name, content="result")


def _reminder(content: str = "reminder") -> ChatMessage:
    return ChatMessage(role=MessageRole.SYSTEM_REMINDER, content=content)


def test_empty_history_is_short_turn() -> None:
    assert scan_tool_usage_in_turn([], _TOOLS) is ToolNudgeVerdict.SHORT_TURN


def test_usage_inside_window_is_used() -> None:
    messages = [_user(), _assistant(), _tool("task"), _assistant(), _assistant()]
    assert scan_tool_usage_in_turn(messages, _TOOLS) is ToolNudgeVerdict.USED


def test_three_assistants_without_usage_is_due() -> None:
    messages = [_user(), _assistant("a"), _assistant("b"), _assistant("c")]
    assert scan_tool_usage_in_turn(messages, _TOOLS) is ToolNudgeVerdict.DUE


def test_user_boundary_before_threshold_is_short_turn() -> None:
    messages = [_user(), _assistant(), _tool("task"), _assistant(), _user()]
    assert scan_tool_usage_in_turn(messages, _TOOLS) is ToolNudgeVerdict.SHORT_TURN


def test_history_exhaustion_before_threshold_is_short_turn() -> None:
    messages = [_assistant(), _assistant()]
    assert scan_tool_usage_in_turn(messages, _TOOLS) is ToolNudgeVerdict.SHORT_TURN


def test_previous_turn_usage_does_not_suppress_current_turn() -> None:
    messages = [
        _user(),
        _assistant(),
        _tool("task"),
        _assistant(),
        _assistant(),
        _assistant(),
        _user(),
        _assistant("a"),
        _assistant("b"),
        _assistant("c"),
    ]
    assert scan_tool_usage_in_turn(messages, _TOOLS) is ToolNudgeVerdict.DUE


def test_previous_turn_tail_does_not_make_current_turn_due() -> None:
    messages = [
        _assistant("old-a"),
        _assistant("old-b"),
        _assistant("old-c"),
        _assistant("old-d"),
        _user(),
        _assistant("new"),
    ]
    assert scan_tool_usage_in_turn(messages, _TOOLS) is ToolNudgeVerdict.SHORT_TURN


def test_agent_role_is_a_boundary() -> None:
    messages = [_user(), _assistant(), _assistant(), _agent(), _assistant()]
    assert scan_tool_usage_in_turn(messages, _TOOLS) is ToolNudgeVerdict.SHORT_TURN


def test_system_reminder_is_transparent_to_the_scan() -> None:
    messages = [
        _user(),
        _assistant("a"),
        _reminder(),
        _assistant("b"),
        _reminder(),
        _assistant("c"),
    ]
    assert scan_tool_usage_in_turn(messages, _TOOLS) is ToolNudgeVerdict.DUE


def test_usage_found_through_transparent_reminders() -> None:
    messages = [
        _user(),
        _assistant("a"),
        _reminder(),
        _tool("task"),
        _assistant("b"),
    ]
    assert scan_tool_usage_in_turn(messages, _TOOLS) is ToolNudgeVerdict.USED


def test_boundary_call_between_third_and_fourth_assistant_is_used() -> None:
    messages = [
        _user(),
        _assistant(),
        _assistant(),
        _tool("task"),
        _assistant(),
    ]
    assert scan_tool_usage_in_turn(messages, _TOOLS) is ToolNudgeVerdict.USED


def test_other_tool_name_is_ignored() -> None:
    messages = [_user(), _assistant(), _tool("read"), _assistant(), _assistant()]
    assert scan_tool_usage_in_turn(messages, _TOOLS) is ToolNudgeVerdict.DUE


def test_multi_name_set_matches_any() -> None:
    messages = [_user(), _tool("todo_write"), _assistant(), _assistant()]
    assert scan_tool_usage_in_turn(
        messages, frozenset({"todo_write", "todo_read"})
    ) is ToolNudgeVerdict.USED


def test_min_steps_one_covers_only_latest_assistant() -> None:
    messages = [_user(), _assistant(), _tool("task")]
    assert (
        scan_tool_usage_in_turn(messages, _TOOLS, min_assistant_steps=1)
        is ToolNudgeVerdict.USED
    )
    messages = [_user(), _tool("task"), _assistant()]
    assert (
        scan_tool_usage_in_turn(messages, _TOOLS, min_assistant_steps=1)
        is ToolNudgeVerdict.DUE
    )
