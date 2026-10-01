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
from bot.webui.transcript_store import JSONLTranscriptStore, TranscriptRecord

from modex_agent.core.emitter import AgentResult, turn_finished_event
from modex_agent.core.turn_events import (
    IterationFinishedEvent,
    StopReason,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.presentation import ToolResult
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

    async def _append_with_pool(sid: str, event: TranscriptRecord, **kwargs) -> None:
        await store.append(sid, event, pool=kwargs.get("pool", pool), sessions_dir=kwargs.get("sessions_dir"))

    transcript_store.append = AsyncMock(side_effect=_append_with_pool)
    return WebBotEmitter(
        output_adapter=output,
        session_id=session_id,
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
                await emitter.emit(TurnTextEvent(text="Hello world"))
                await emitter.emit(IterationFinishedEvent(iteration=0, has_tool_calls=False))

            # Read back — the text segment persisted as one complete
            # TextDelta record (the only persisted record here).
            jstore = JSONLTranscriptStore(base / "main")
            records = await jstore.load("conv.main")
            assert len(records) == 1, f"Expected 1 record, got {len(records)}"
            assert records[0].kind == "text_delta"
            assert "Hello" in str(records[0].model_dump())

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
                await emitter.emit(TurnTextEvent(text="test"))
                await emitter.emit(IterationFinishedEvent(iteration=0, has_tool_calls=False))

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
                await emitter.emit(TurnTextEvent(text="in-A"))
                await emitter.emit(IterationFinishedEvent(iteration=0, has_tool_calls=False))

            emitter2 = _build_emitter("s2.main", store)
            with bind_workspace_root(ws_b_root):
                await emitter2.emit(TurnTextEvent(text="in-B"))
                await emitter2.emit(IterationFinishedEvent(iteration=0, has_tool_calls=False))

            # Verify A has s1
            assert (ws_a / "main" / "s1.main.jsonl").exists(), "A must still have s1"
            events_a = await JSONLTranscriptStore(ws_a / "main").load("s1.main")
            assert any("in-A" in str(e.model_dump()) for e in events_a)

            # Verify B has s2
            assert (ws_b / "main" / "s2.main.jsonl").exists(), "B must have s2"
            events_b = await JSONLTranscriptStore(ws_b / "main").load("s2.main")
            assert any("in-B" in str(e.model_dump()) for e in events_b)

            # Verify A does not have s2
            assert not (ws_a / "main" / "s2.main.jsonl").exists(), "A must not have s2"

    async def test_tool_events_persisted(self) -> None:
        """Tool call and result records are correctly persisted.

        The call+result pair is persisted TOGETHER at result time (a
        ``ToolCallStarted`` record ahead of the ``ToolResult``), and the
        text segments around it flush as complete ``TextDelta`` records.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / ".modex" / "sessions"
            store = _build_store()
            emitter = _build_emitter("conv.main", store)

            with bind_workspace_root(root):
                await emitter.emit(
                    TurnToolCallEvent(
                        tool_name="read_file",
                        call_id="c1",
                        arguments={"path": "/x"},
                    )
                )
                await emitter.emit(TurnTextEvent(text="Checking..."))
                await emitter.emit(
                    TurnToolResultEvent(
                        tool_name="read_file", call_id="c1", output="contents", seq=7
                    )
                )
                await emitter.emit(TurnTextEvent(text="Done!"))

                result = AgentResult(stop_reason=StopReason.COMPLETED, content="Done!")
                await emitter.emit(turn_finished_event(result))

            records = await JSONLTranscriptStore(base / "main").load("conv.main")
            kinds = [record.kind for record in records]

            # Content records must be present
            for expected in ["tool_call_started", "text_delta", "tool_result"]:
                assert expected in kinds, f"Missing {expected} in {kinds}"
            tool_result = next(
                record
                for record in records
                if isinstance(record, ToolResult)
            )
            assert tool_result.seq == 7

            # Metadata records must NOT be persisted
            for forbidden in ["turn_started", "tool_args_delta"]:
                assert forbidden not in kinds, (
                    f"{forbidden} should NOT be persisted — it is "
                    f"transient wire-only metadata"
                )

    async def test_files_are_valid_jsonl(self) -> None:
        """Each line is valid JSON and decodable as a transcript record."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _build_store()
            emitter = _build_emitter("s.main", store)

            with bind_workspace_root(root):
                for text in ["First", "Second", "Third"]:
                    await emitter.emit(TurnTextEvent(text=text))
                    await emitter.emit(IterationFinishedEvent(iteration=0, has_tool_calls=False))

            file_path = root / ".modex" / "sessions" / "main" / "s.main.jsonl"
            lines = file_path.read_text(encoding="utf-8").strip().split("\n")
            assert len(lines) >= 1
            for line in lines:
                data = json.loads(line)
                assert "session_id" in data
                assert "kind" in data
