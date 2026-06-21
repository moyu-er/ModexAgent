"""Unit tests for WebUI event dataclasses."""

from bot.webui.events import (
    AssistantTextEvent,
    ServerEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnStartEvent,
    UserMessageEvent,
)


def test_user_message_event_serializes() -> None:
    ev = UserMessageEvent(session_id="web:abc", agent_name="main", content="hello")
    data = ev.to_dict()
    assert data["event"] == "user_message"
    assert data["content"] == "hello"


def test_server_event_from_dict() -> None:
    data = {"event": "user_message", "session_id": "web:abc", "agent_name": "main", "content": "hello"}
    ev = ServerEvent.from_dict(data)
    assert ev.event == "user_message"
    assert ev.content == "hello"  # type: ignore[attr-defined]


def test_turn_start_event_roundtrip() -> None:
    ev = TurnStartEvent(
        session_id="abc.main", agent_name="main",
        turn_id="a1b2c3d4e5f6",
    )
    d = ev.to_dict()
    loaded = ServerEvent.from_dict(d)
    assert loaded.turn_id == "a1b2c3d4e5f6"  # type: ignore[attr-defined]
    assert loaded.event == "turn_start"


def test_assistant_text_event_roundtrip() -> None:
    ev = AssistantTextEvent(
        session_id="abc.main", agent_name="main",
        turn_id="a1b2c3d4e5f6", text="Hello World",
    )
    d = ev.to_dict()
    loaded = ServerEvent.from_dict(d)
    assert loaded.text == "Hello World"  # type: ignore[attr-defined]
    assert loaded.turn_id == "a1b2c3d4e5f6"  # type: ignore[attr-defined]


def test_tool_call_event_roundtrip() -> None:
    ev = ToolCallEvent(
        session_id="abc.main", agent_name="main",
        turn_id="a1b2c3d4e5f6", call_id="call_0",
        tool_name="read_file", args={"path": "/tmp/x"},
    )
    d = ev.to_dict()
    loaded = ServerEvent.from_dict(d)
    assert loaded.call_id == "call_0"  # type: ignore[attr-defined]
    assert loaded.tool_name == "read_file"  # type: ignore[attr-defined]
    assert loaded.args == {"path": "/tmp/x"}  # type: ignore[attr-defined]


def test_tool_result_event_roundtrip() -> None:
    ev = ToolResultEvent(
        session_id="abc.main", agent_name="main",
        turn_id="a1b2c3d4e5f6", call_id="call_0",
        tool_name="read_file", result="file content",
    )
    d = ev.to_dict()
    loaded = ServerEvent.from_dict(d)
    assert loaded.result == "file content"  # type: ignore[attr-defined]
    assert loaded.error is None  # type: ignore[attr-defined]


def test_tool_result_event_with_error_roundtrip() -> None:
    ev = ToolResultEvent(
        session_id="abc.main", agent_name="main",
        turn_id="a1b2c3d4e5f6", call_id="call_0",
        tool_name="read_file", result="", error="Permission denied",
    )
    d = ev.to_dict()
    loaded = ServerEvent.from_dict(d)
    assert loaded.error == "Permission denied"  # type: ignore[attr-defined]
