"""Integration tests for WebUI event pipeline."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from bot.webui.events import (
    ModelContentDelta,
    ModelReasoningDelta,
    ToolCallEndEvent,
    ToolCallStartEvent,
    TurnEndEvent,
    UserMessageEvent,
    WebUIEventType,
)
from bot.webui.transcript_store import JSONLTranscriptStore


def test_full_conversation_roundtrip() -> None:
    """Write a complete conversation flow and read it back."""
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONLTranscriptStore(Path(tmp))
        conv_id = "web:test-roundtrip"
        agent = "main"

        # Simulate a full conversation turn
        events = [
            UserMessageEvent(conversation_id=conv_id, agent_name=agent, content="hello"),
            ModelReasoningDelta(conversation_id=conv_id, agent_name=agent, text="thinking...", turn_id="turn_1"),
            ModelContentDelta(conversation_id=conv_id, agent_name=agent, text="Hi", turn_id="turn_1"),
            ToolCallStartEvent(conversation_id=conv_id, agent_name=agent, tool="read", args={"path": "doc.md"}, turn_id="turn_1"),
            ToolCallEndEvent(conversation_id=conv_id, agent_name=agent, tool="read", result_summary="content here", turn_id="turn_1"),
            ModelContentDelta(conversation_id=conv_id, agent_name=agent, text=" there!", turn_id="turn_1"),
            TurnEndEvent(conversation_id=conv_id, agent_name=agent, turn_id="turn_1", latency_ms=1500),
        ]

        for evt in events:
            store.append(conv_id, agent, evt)

        loaded = list(store.load(conv_id, agent))
        assert len(loaded) == 7

        # Verify event type order
        expected_types = [
            WebUIEventType.USER_MESSAGE.value,
            WebUIEventType.MODEL_REASONING_DELTA.value,
            WebUIEventType.MODEL_CONTENT_DELTA.value,
            WebUIEventType.TOOL_CALL_START.value,
            WebUIEventType.TOOL_CALL_END.value,
            WebUIEventType.MODEL_CONTENT_DELTA.value,
            WebUIEventType.TURN_END.value,
        ]
        for i, evt in enumerate(loaded):
            assert evt.event == expected_types[i], f"Event {i}: expected {expected_types[i]}, got {evt.event}"


def test_event_json_roundtrip() -> None:
    """Verify events survive to_dict → JSON → from_dict unchanged."""
    original = UserMessageEvent(conversation_id="web:abc", agent_name="main", content="hello world")
    json_str = json.dumps(original.to_dict())
    data = json.loads(json_str)
    restored = UserMessageEvent.from_dict(data)
    assert restored.event == original.event
    assert restored.conversation_id == original.conversation_id
    assert restored.content == original.content  # type: ignore[attr-defined]


def test_multi_agent_threads() -> None:
    """Verify multiple agents within one conversation are tracked separately."""
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONLTranscriptStore(Path(tmp))
        conv_id = "web:multi-agent"

        store.append(conv_id, "main", UserMessageEvent(conversation_id=conv_id, agent_name="main", content="hi"))
        store.append(conv_id, "office-expert", UserMessageEvent(conversation_id=conv_id, agent_name="office-expert", content="analyzing..."))

        agents = store.list_agents(conv_id)
        assert agents == {"main", "office-expert"}

        main_events = list(store.load(conv_id, "main"))
        assert len(main_events) == 1
        assert main_events[0].event == WebUIEventType.USER_MESSAGE.value

        sub_events = list(store.load(conv_id, "office-expert"))
        assert len(sub_events) == 1
