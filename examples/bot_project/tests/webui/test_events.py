"""Unit tests for WebUI event dataclasses."""

from bot.webui.events import ServerEvent, UserMessageEvent


def test_user_message_event_serializes() -> None:
    ev = UserMessageEvent(conversation_id="web:abc", agent_name="main", content="hello")
    data = ev.to_dict()
    assert data["event"] == "user_message"
    assert data["content"] == "hello"


def test_server_event_from_dict() -> None:
    data = {"event": "user_message", "conversation_id": "web:abc", "agent_name": "main", "content": "hello"}
    ev = ServerEvent.from_dict(data)
    assert ev.event == "user_message"
    assert ev.content == "hello"  # type: ignore[attr-defined]
