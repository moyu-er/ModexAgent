"""Integration tests for WebUI event pipeline."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

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


@pytest.mark.asyncio
async def test_full_conversation_roundtrip() -> None:
    """Write a complete conversation flow and read it back."""
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONLTranscriptStore(Path(tmp))
        session_id = "web:test-roundtrip.main"
        agent = "main"

        # Simulate a full conversation turn
        events = [
            UserMessageEvent(session_id=session_id, agent_name=agent, content="hello"),
            ModelReasoningDelta(session_id=session_id, agent_name=agent, text="thinking...", turn_id="turn_1"),
            ModelContentDelta(session_id=session_id, agent_name=agent, text="Hi", turn_id="turn_1"),
            ToolCallStartEvent(session_id=session_id, agent_name=agent, tool="read", args={"path": "doc.md"}, turn_id="turn_1"),
            ToolCallEndEvent(session_id=session_id, agent_name=agent, tool="read", result_summary="content here", turn_id="turn_1"),
            ModelContentDelta(session_id=session_id, agent_name=agent, text=" there!", turn_id="turn_1"),
            TurnEndEvent(session_id=session_id, agent_name=agent, turn_id="turn_1", latency_ms=1500),
        ]

        for evt in events:
            await store.append(session_id, evt)

        loaded = await store.load(session_id)
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
    original = UserMessageEvent(session_id="web:abc", agent_name="main", content="hello world")
    json_str = json.dumps(original.to_dict())
    data = json.loads(json_str)
    restored = UserMessageEvent.from_dict(data)
    assert restored.event == original.event
    assert restored.session_id == original.session_id
    assert restored.content == original.content  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_multi_agent_threads() -> None:
    """Verify multiple agents within one conversation are tracked separately."""
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONLTranscriptStore(Path(tmp))
        # Session ids are filesystem-safe by design (no ':' which is invalid
        # on Windows filenames); use the canonical {conv}.{agent} form.
        main_sid = "multiagent.main"
        sub_sid = "multiagent.office-expert"

        await store.append(main_sid, UserMessageEvent(session_id=main_sid, agent_name="main", content="hi"))
        await store.append(sub_sid, UserMessageEvent(session_id=sub_sid, agent_name="office-expert", content="analyzing..."))

        sessions = await store.list_sessions_by_prefix("multiagent")
        assert sessions == {main_sid, sub_sid}

        main_events = await store.load(main_sid)
        assert len(main_events) == 1
        assert main_events[0].event == WebUIEventType.USER_MESSAGE.value

        sub_events = await store.load(sub_sid)
        assert len(sub_events) == 1
