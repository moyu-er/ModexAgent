"""Tests for the structured DeltaEnvelope transport container."""

from __future__ import annotations

import pytest

from bot.webui.events import (
    DeltaEnvelope,
    ModelContentDelta,
    ServerEvent,
    ToolCallStartEvent,
    UserMessageEvent,
)


def test_from_event_splits_envelope_and_payload() -> None:
    """from_event moves session/agent/type/timestamp to the envelope and keeps
    event-specific fields (text, turn_id, ...) in payload."""
    event = ModelContentDelta(
        session_id="conv.reviewer.aa11",
        agent_name="reviewer",
        text="hello",
        turn_id="turn_1",
    )
    envelope = DeltaEnvelope.from_event(
        event, metadata={"k": "v"}, pool="coding", parent_session_id="conv.coding",
    )

    assert envelope.session_id == "conv.reviewer.aa11"
    assert envelope.agent_name == "reviewer"
    assert envelope.event_type == "model_content_delta"
    assert envelope.pool == "coding"
    assert envelope.parent_session_id == "conv.coding"
    assert envelope.metadata == {"k": "v"}
    # event-specific fields live in payload
    assert envelope.payload == {"text": "hello", "turn_id": "turn_1", "segment_id": ""}


def test_from_event_carries_tool_call_fields() -> None:
    event = ToolCallStartEvent(
        session_id="conv.main",
        agent_name="main",
        tool="read",
        args={"path": "x"},
        turn_id="turn_1",
        call_id="call_0",
    )
    envelope = DeltaEnvelope.from_event(event)
    assert envelope.payload == {
        "tool": "read",
        "args": {"path": "x"},
        "turn_id": "turn_1",
        "call_id": "call_0",
    }


def test_to_dict_round_trip_preserves_structure() -> None:
    event = UserMessageEvent(
        session_id="abc.main", agent_name="main", content="hi",
    )
    envelope = DeltaEnvelope.from_event(event, metadata={"k": "v"})
    wire = envelope.to_dict()

    assert wire["session_id"] == "abc.main"
    assert wire["agent_name"] == "main"
    assert wire["event_type"] == "user_message"
    assert wire["pool"] == ""
    assert wire["parent_session_id"] is None
    assert wire["metadata"] == {"k": "v"}
    assert wire["payload"] == {"content": "hi", "attachments": []}
    assert "timestamp" in wire


def test_pool_and_parent_default_when_unspecified() -> None:
    """Without pool/parent, defaults are empty string and None."""
    event = UserMessageEvent(session_id="abc.main", agent_name="main", content="hi")
    envelope = DeltaEnvelope.from_event(event)
    assert envelope.pool == ""
    assert envelope.parent_session_id is None


def test_to_event_reconstructs_server_event() -> None:
    """to_event reverses from_event, yielding the original ServerEvent shape."""
    original = UserMessageEvent(
        session_id="abc.main", agent_name="main", content="hi",
    )
    envelope = DeltaEnvelope.from_event(original)
    rebuilt: ServerEvent = envelope.to_event()

    assert rebuilt.event == "user_message"
    assert rebuilt.session_id == "abc.main"
    assert rebuilt.agent_name == "main"
    assert rebuilt.content == "hi"  # type: ignore[attr-defined]


def test_metadata_defaults_to_empty_dict() -> None:
    envelope = DeltaEnvelope.from_event(
        UserMessageEvent(session_id="a.main", agent_name="main", content="x"),
    )
    assert envelope.metadata == {}


def test_content_envelope_for_raw_string() -> None:
    """A plain content string (framework send_delta fallback) wraps into a
    content envelope carrying the text in payload."""
    envelope = DeltaEnvelope.content(
        session_id="conv.main",
        agent_name="main",
        text="chunk",
        metadata={"reasoning": True},
    )
    assert envelope.event_type == "content"
    assert envelope.payload == {"text": "chunk"}
    assert envelope.metadata == {"reasoning": True}
