"""Integration tests for WebUI event pipeline."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from bot.webui.transcript_store import (
    JSONLTranscriptStore,
    UserMessageRecord,
    materialize_records,
)

from modex_agent.core.turn_events import StopReason
from modex_agent.presentation import (
    TextDelta,
    ThinkingDelta,
    ToolCallStarted,
    ToolResult,
    TurnFinished,
)


@pytest.mark.asyncio
async def test_full_conversation_roundtrip() -> None:
    """Write a complete conversation flow and read it back."""
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONLTranscriptStore(Path(tmp))
        session_id = "web:test-roundtrip.main"
        agent = "main"

        # Simulate the persisted record set of one full conversation turn
        # (exactly what the recording tap writes: user message, streamed
        # segments, paired tool cards, terminal record).
        records = [
            UserMessageRecord(session_id=session_id, agent_name=agent, content="hello"),
            ThinkingDelta(session_id=session_id, agent_name=agent, turn_id="turn_1",
                          text="thinking...", segment_id="_reasoning", timestamp_ms=101),
            TextDelta(session_id=session_id, agent_name=agent, turn_id="turn_1",
                      text="Hi", segment_id="_text", timestamp_ms=102),
            ToolCallStarted(session_id=session_id, agent_name=agent, turn_id="turn_1",
                            tool_name="read", call_id="c1",
                            arguments={"path": "doc.md"}, timestamp_ms=103),
            ToolResult(session_id=session_id, agent_name=agent, turn_id="turn_1",
                       tool_name="read", call_id="c1", output="content here",
                       timestamp_ms=104),
            TextDelta(session_id=session_id, agent_name=agent, turn_id="turn_1",
                      text=" there!", segment_id="_text", timestamp_ms=105),
            TurnFinished(session_id=session_id, agent_name=agent, turn_id="turn_1",
                         stop_reason=StopReason.COMPLETED, timestamp_ms=106),
        ]

        for record in records:
            await store.append(session_id, record)

        loaded = await store.load(session_id)
        assert len(loaded) == 7

        # Verify record kind order survives the round-trip.
        expected_kinds = [
            "user_message",
            "thinking_delta",
            "text_delta",
            "tool_call_started",
            "tool_result",
            "text_delta",
            "turn_finished",
        ]
        assert [record.kind for record in loaded] == expected_kinds

        turns = materialize_records(loaded)
        assert len(turns) == 1
        # The text segment's second run sits AFTER the tool card, so the
        # folder keeps it a separate block (contiguous-run merging).
        assert turns[0].blocks == [
            {"kind": "reasoning", "text": "thinking..."},
            {"kind": "text", "text": "Hi"},
            {"kind": "tool", "tool": "read", "args": {"path": "doc.md"},
             "result": "content here"},
            {"kind": "text", "text": " there!"},
        ]


def test_event_json_roundtrip() -> None:
    """Verify records survive model_dump_json → JSON → validate unchanged."""
    original = UserMessageRecord(session_id="web:abc", agent_name="main", content="hello world")
    json_str = original.model_dump_json()
    restored = UserMessageRecord.model_validate_json(json_str)
    assert restored == original


@pytest.mark.asyncio
async def test_multi_agent_threads() -> None:
    """Verify multiple agents within one conversation are tracked separately."""
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONLTranscriptStore(Path(tmp))
        # Session ids are filesystem-safe by design (no ':' which is invalid
        # on Windows filenames); use the canonical {conv}.{agent} form.
        main_sid = "multiagent.main"
        sub_sid = "multiagent.office-expert"

        await store.append(main_sid, UserMessageRecord(session_id=main_sid, agent_name="main", content="hi"))
        await store.append(sub_sid, UserMessageRecord(session_id=sub_sid, agent_name="office-expert", content="analyzing..."))

        sessions = await store.list_sessions_by_prefix("multiagent")
        assert sessions == {main_sid, sub_sid}

        main_records = await store.load(main_sid)
        assert len(main_records) == 1
        assert main_records[0].kind == "user_message"

        sub_records = await store.load(sub_sid)
        assert len(sub_records) == 1
