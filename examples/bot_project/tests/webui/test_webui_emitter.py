"""Tests for WebBotEmitter streaming event emitter and CompositeEmitter."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.webui.emitter import CompositeEmitter, WebBotEmitter, _merge_blocks
from bot.webui.events import WebUIEventType
from bot.webui.transcript_store import JSONLTranscriptStore
from framework.agents.react.agent import ReActEvent
from framework.core.emitter import AgentResult, ContentEmitter, EmitterConfig


@pytest.mark.asyncio
async def test_emit_content_delta() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(output_adapter, "web:abc.main", config=EmitterConfig())
    input_adapter.register_connection("web:abc.main", None)
    await emitter.emit_delta("hello")
    q = input_adapter._delta_queues.get("web:abc.main")
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.event_type == "model_content_delta"
    assert envelope.payload == {"text": "hello", "turn_id": "turn_1"}
    assert envelope.session_id == "web:abc.main"
    assert envelope.agent_name == "main"


@pytest.mark.asyncio
async def test_emit_complete_sends_turn_end() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(output_adapter, "web:abc.main", config=EmitterConfig())
    input_adapter.register_connection("web:abc.main", None)
    await emitter.emit_complete(AgentResult(content="done"))
    q = input_adapter._delta_queues.get("web:abc.main")
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.event_type == "turn_end"


@pytest.mark.asyncio
async def test_emit_complete_saves_to_transcript() -> None:
    """emitter_complete persists a complete AssistantTurnEvent to transcript."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(
            output_adapter, "conv1.main",
            config=EmitterConfig(),
            transcript_store=store,
        )
        input_adapter.register_connection("conv1.main", None)

        # Simulate streaming in order: reasoning → content → tool → content
        emitter._blocks.append({"kind": "reasoning", "text": "Let me think."})
        emitter._blocks.append({"kind": "text", "text": "Hello"})
        emitter._blocks.append({"kind": "tool", "tool": "read", "args": {"path": "doc.md"}, "result": "content"})
        emitter._blocks.append({"kind": "text", "text": " World"})

        await emitter.emit_complete(AgentResult(content="Hello World"))

        # Verify transcript save — blocks preserved in order
        events = list(store.load("conv1.main"))
        assert len(events) == 1
        saved = events[0]
        assert saved.event == WebUIEventType.ASSISTANT_TURN.value
        blocks = saved.blocks  # type: ignore[attr-defined]
        assert len(blocks) == 4
        assert blocks[0]["kind"] == "reasoning"
        assert blocks[1]["kind"] == "text"
        assert blocks[2]["kind"] == "tool"
        assert blocks[3]["kind"] == "text"


@pytest.mark.asyncio
async def test_streaming_does_not_save_deltas() -> None:
    """emit_delta pushes WS events but does NOT write to transcript."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(
            output_adapter, "conv1.main",
            config=EmitterConfig(),
            transcript_store=store,
        )
        input_adapter.register_connection("conv1.main", None)

        await emitter.emit_delta("hello")
        await emitter.emit_delta(" world")

        # No events saved yet — only at turn end
        events = list(store.load("conv1.main"))
        assert len(events) == 0

        # WS delta IS queued
        q = input_adapter._delta_queues.get("conv1.main")
        assert q is not None
        assert q.qsize() == 2


@pytest.mark.asyncio
async def test_subagent_emitter_preserves_full_session_id() -> None:
    """Regression: a subagent session id carries an invocation_id segment.

    The emitter must keep the FULL session id (with invocation_id) in every
    event it emits AND persist the transcript keyed by that full id — so two
    reviewer invocations do not collapse into one transcript.
    """
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        full_sid = "conv1.reviewer.aa11bb22"
        emitter = WebBotEmitter(
            output_adapter, full_sid,
            config=EmitterConfig(),
            transcript_store=store,
        )
        input_adapter.register_connection(full_sid, None)

        emitter._blocks.append({"kind": "text", "text": "review done"})
        await emitter.emit_complete(AgentResult(content="review done"))

        # WebSocket delta events carry the FULL session id + correct agent.
        q = input_adapter._delta_queues.get(full_sid)
        assert q is not None
        envelope = q.get_nowait()
        assert envelope.event_type == "turn_end"
        assert envelope.session_id == full_sid
        assert envelope.agent_name == "reviewer"

        # Transcript persisted under the FULL session id (not truncated).
        assert list(store.load(full_sid))
        assert not list(store.load("conv1.reviewer"))


@pytest.mark.asyncio
async def test_two_subagent_emitters_persist_to_separate_transcripts() -> None:
    """Two reviewer invocations with different invocation_ids stay separate."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))

        for sid in ("conv1.reviewer.aa11", "conv1.reviewer.bb22"):
            em = WebBotEmitter(
                output_adapter, sid,
                config=EmitterConfig(),
                transcript_store=store,
            )
            em._blocks.append({"kind": "text", "text": sid})
            await em.emit_complete(AgentResult(content=sid))

        assert len(list(store.load("conv1.reviewer.aa11"))) == 1
        assert len(list(store.load("conv1.reviewer.bb22"))) == 1
        assert store.list_sessions() == {"conv1.reviewer.aa11", "conv1.reviewer.bb22"}


# ── CompositeEmitter tests ────────────────────────────────────────────────


class _StubEmitter(ContentEmitter[ReActEvent]):
    """Recording emitter that tracks which methods were called."""

    def __init__(self) -> None:
        super().__init__(EmitterConfig())
        self.calls: list[str] = []

    async def emit_delta(self, delta: str) -> None:
        self.calls.append(f"delta:{delta}")

    async def emit_complete(self, result: AgentResult) -> None:
        self.calls.append(f"complete:{result.content}")

    async def emit_error(self, error: str) -> None:
        self.calls.append(f"error:{error}")

    def wants_streaming(self) -> bool:
        return True


class _FailingEmitter(ContentEmitter[ReActEvent]):
    """Emitter that raises on every method."""

    async def emit_delta(self, delta: str) -> None:
        raise RuntimeError("boom")

    async def emit_complete(self, result: AgentResult) -> None:
        raise RuntimeError("boom")

    async def emit_error(self, error: str) -> None:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_composite_fans_out_to_all_children() -> None:
    """CompositeEmitter delegates to all children."""
    stub1 = _StubEmitter()
    stub2 = _StubEmitter()
    composite = CompositeEmitter[ReActEvent](emitters=[stub1, stub2])

    await composite.emit_delta("hello")
    await composite.emit_complete(AgentResult(content="done"))

    assert stub1.calls == ["delta:hello", "complete:done"]
    assert stub2.calls == ["delta:hello", "complete:done"]


@pytest.mark.asyncio
async def test_composite_error_isolation() -> None:
    """One failing child does not prevent others from receiving events."""
    stub = _StubEmitter()
    failing = _FailingEmitter()
    composite = CompositeEmitter[ReActEvent](emitters=[failing, stub])

    await composite.emit_delta("test")
    assert stub.calls == ["delta:test"]


@pytest.mark.asyncio
async def test_composite_wants_streaming_or_semantics() -> None:
    """wants_streaming returns True if ANY child wants streaming."""
    no_stream = _StubEmitter()
    no_stream.calls = []  # reset

    class _NoStreaming(ContentEmitter[ReActEvent]):
        async def emit_delta(self, delta: str) -> None:
            pass
        async def emit_complete(self, result: AgentResult) -> None:
            pass
        async def emit_error(self, error: str) -> None:
            pass

    composite = CompositeEmitter[ReActEvent](
        emitters=[_NoStreaming(), _StubEmitter()],
    )
    assert composite.wants_streaming() is True

    composite2 = CompositeEmitter[ReActEvent](
        emitters=[_NoStreaming(), _NoStreaming()],
    )
    assert composite2.wants_streaming() is False


# ── Block merge ──────────────────────────────────────────────────────────


def test_merge_adjacent_text_blocks() -> None:
    """Adjacent text blocks are merged into one."""
    blocks: list[dict[str, object]] = [
        {"kind": "text", "text": "Hello"},
        {"kind": "text", "text": " World"},
        {"kind": "text", "text": "!"},
    ]
    merged = _merge_blocks(blocks)
    assert len(merged) == 1
    assert merged[0] == {"kind": "text", "text": "Hello World!"}


def test_merge_adjacent_reasoning_blocks() -> None:
    """Adjacent reasoning blocks are merged into one."""
    blocks: list[dict[str, object]] = [
        {"kind": "reasoning", "text": "Let"},
        {"kind": "reasoning", "text": " me think."},
    ]
    merged = _merge_blocks(blocks)
    assert len(merged) == 1
    assert merged[0] == {"kind": "reasoning", "text": "Let me think."}


def test_merge_preserves_interleaving() -> None:
    """Interleaved text/reasoning/tool blocks stay separate."""
    blocks: list[dict[str, object]] = [
        {"kind": "reasoning", "text": "Hmm"},
        {"kind": "text", "text": "Hello"},
        {"kind": "tool", "tool": "read", "args": {"path": "x"}},
        {"kind": "text", "text": " World"},
    ]
    merged = _merge_blocks(blocks)
    assert len(merged) == 4
    assert merged[0] == {"kind": "reasoning", "text": "Hmm"}
    assert merged[1] == {"kind": "text", "text": "Hello"}
    assert merged[2]["kind"] == "tool"
    assert merged[3] == {"kind": "text", "text": " World"}


def test_merge_empty_blocks() -> None:
    """Empty list returns empty."""
    assert _merge_blocks([]) == []
