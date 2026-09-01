"""End-to-end transcript persistence tests.

Verifies the complete chain: WebUIService boot → emitter writes →
file on disk → read back via transcript store.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.emitter import WebBotEmitter
from bot.webui.events import ServerEvent, ToolResultEvent
from bot.webui.transcript_store import JSONLTranscriptStore

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.constants import ToolCallEndPayload
from modex_agent.core.emitter import AgentResult, EmitterConfig, StopReason
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import ToolCall
from modex_agent.workspace.runtime import bind_workspace_root


# Helpers — avoid full bot boot for simpler tests
def _build_store() -> WorkspaceScopedTranscriptStore:
    return WorkspaceScopedTranscriptStore(data_dir_name=".modex")


def _build_emitter(session_id: str, store: WorkspaceScopedTranscriptStore) -> WebBotEmitter:
    output = MagicMock()
    output.send_envelope = AsyncMock()
    from bot.webui.events import SessionMeta

    pool = "coding" if session_id.endswith(".coding") else "main"
    transcript_store = MagicMock(wraps=store)

    async def _append_with_pool(sid: str, event: ServerEvent, **kwargs) -> None:
        await store.append(sid, event, pool=kwargs.get("pool", pool), sessions_dir=kwargs.get("sessions_dir"))

    transcript_store.append = AsyncMock(side_effect=_append_with_pool)
    return WebBotEmitter(
        output_adapter=output,
        session_id=session_id,
        config=EmitterConfig(),
        pool=pool,
        transcript_store=transcript_store,
        session_meta_resolver=lambda: SessionMeta(parent_session_id=None),
    )


class TestTranscriptPersistence:
    """Complete round-trip: emit events → persist → read back."""

    async def test_all_event_types_persisted_and_readable(self) -> None:
        """All event types survive a write → read round-trip."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / ".modex" / "sessions"
            store = _build_store()
            emitter = _build_emitter("conv.main", store)

            # Simulate a turn (text is buffered and flushed at stream/turn end).
            with bind_workspace_root(root):
                await emitter.emit_content("Hello world")
                await emitter.emit_stream_end(resuming=False)

            # Read back — TurnStartEvent/TurnEndEvent are WebSocket-only,
            # not persisted. AssistantTextEvent is the only persisted event here.
            jstore = JSONLTranscriptStore(base / "main")
            events = await jstore.load("conv.main")
            assert len(events) == 1, f"Expected 1 event, got {len(events)}"
            assert "Hello" in str(events[0].to_dict())

    @pytest.mark.parametrize(
        "session_id,expected_pool",
        [
            ("abc.main", "main"),
            ("abc.coding", "coding"),
            ("abc.unknown", "main"),  # default pool
        ],
    )
    async def test_session_routed_to_correct_pool_directory(
        self, session_id: str, expected_pool: str
    ) -> None:
        """Sessions are written under the correct pool subdirectory."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _build_store()
            emitter = _build_emitter(session_id, store)

            with bind_workspace_root(root):
                await emitter.emit_content("test")
                await emitter.emit_stream_end(resuming=False)

            file = root / ".modex" / "sessions" / expected_pool / f"{session_id}.jsonl"
            assert file.exists(), (
                f"Expected {file} for session {session_id!r}, "
                f"pool={expected_pool!r}"
            )

    async def test_resolver_routes_writes_to_new_workspace(self) -> None:
        """Routing is by the bound workspace root (ctxvar), not a resolver:
        writes for each emitter land under that emitter's bound root.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ws_a_root = tmp / "ws_a"
            ws_b_root = tmp / "ws_b"
            ws_a = ws_a_root / ".modex" / "sessions"
            ws_b = ws_b_root / ".modex" / "sessions"
            ws_a.mkdir(parents=True)
            ws_b.mkdir(parents=True)

            store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
            emitter = _build_emitter("s1.main", store)
            with bind_workspace_root(ws_a_root):
                await emitter.emit_content("in-A")
                await emitter.emit_stream_end(resuming=False)

            emitter2 = _build_emitter("s2.main", store)
            with bind_workspace_root(ws_b_root):
                await emitter2.emit_content("in-B")
                await emitter2.emit_stream_end(resuming=False)

            # Verify A has s1
            assert (ws_a / "main" / "s1.main.jsonl").exists(), "A must still have s1"
            events_a = await JSONLTranscriptStore(ws_a / "main").load("s1.main")
            assert any("in-A" in str(e.to_dict()) for e in events_a)

            # Verify B has s2
            assert (ws_b / "main" / "s2.main.jsonl").exists(), "B must have s2"
            events_b = await JSONLTranscriptStore(ws_b / "main").load("s2.main")
            assert any("in-B" in str(e.to_dict()) for e in events_b)

            # Verify A does not have s2
            assert not (ws_a / "main" / "s2.main.jsonl").exists(), "A must not have s2"

    async def test_tool_events_persisted(self) -> None:
        """Tool call and result events are correctly persisted.

        ``TurnStartEvent`` and ``TurnEndEvent`` are intentionally NOT
        persisted — they are sent only via WebSocket for the frontend UI.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / ".modex" / "sessions"
            store = _build_store()
            emitter = _build_emitter("conv.main", store)

            tc = ToolCall(
                tool_name="read_file",
                arguments={"path": "/x"},
                call_id="c1",
            )
            tr = ToolResult.from_text("read_file", "contents", call_id="c1")

            with bind_workspace_root(root):
                await emitter.emit(ReActEvent.TOOL_CALL_START, tc)
                await emitter.emit_content("Checking...")
                await emitter.emit(
                    ReActEvent.TOOL_CALL_END,
                    ToolCallEndPayload(tool_call=tc, result=tr, seq=7),
                )
                await emitter.emit_content("Done!")

                result = AgentResult(stop_reason=StopReason.COMPLETED, content="Done!")
                await emitter.emit_complete(result)

            events = await JSONLTranscriptStore(base / "main").load("conv.main")
            event_types = [e.__class__.__name__ for e in events]

            # Content events must be present
            for expected in ["ToolCallEvent", "AssistantTextEvent", "ToolResultEvent"]:
                assert expected in event_types, f"Missing {expected} in {event_types}"
            tool_result = next(e for e in events if isinstance(e, ToolResultEvent))
            assert tool_result.seq == 7

            # Metadata events must NOT be persisted
            for forbidden in ["TurnStartEvent", "TurnEndEvent"]:
                assert forbidden not in event_types, (
                    f"{forbidden} should NOT be persisted — it is "
                    f"WebSocket-only metadata"
                )

    async def test_files_are_valid_jsonl(self) -> None:
        """Each line is valid JSON and readable as a ServerEvent."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _build_store()
            emitter = _build_emitter("s.main", store)

            with bind_workspace_root(root):
                for text in ["First", "Second", "Third"]:
                    await emitter.emit_content(text)
                    await emitter.emit_stream_end(resuming=False)

            file_path = root / ".modex" / "sessions" / "main" / "s.main.jsonl"
            lines = file_path.read_text(encoding="utf-8").strip().split("\n")
            assert len(lines) >= 1
            for line in lines:
                data = json.loads(line)
                assert "session_id" in data
                assert "event" in data
