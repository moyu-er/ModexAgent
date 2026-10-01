"""Tests for WebBotEmitter streaming event sink and CompositeEmitter."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from bot.acp.emitter import AcpEmitterHub, AcpTurnEmitter
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.webui.emitter import CompositeEmitter, WebBotEmitter
from bot.webui.emitter import web_bot as web_bot_module
from bot.webui.events import (
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    ServerEvent,
    ToolArgsDeltaEvent,
    UsageSummaryEvent,
    WebUIEventType,
)
from bot.webui.transcript_store import JSONLTranscriptStore

from modex_agent.core.emitter import AgentResult, TurnEventSink, turn_finished_event
from modex_agent.core.llm_struct import TokenUsage
from modex_agent.core.turn_events import (
    ApprovalRequestedEvent as CoreApprovalRequestedEvent,
)
from modex_agent.core.turn_events import (
    ApprovalResolvedEvent as CoreApprovalResolvedEvent,
)
from modex_agent.core.turn_events import (
    IterationFinishedEvent,
    StopReason,
    TurnEvent,
    TurnFinishedEvent,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
    UsageEvent,
)
from modex_agent.core.turn_events import (
    ToolArgsDeltaEvent as CoreToolArgsDeltaEvent,
)
from modex_agent.presentation import ToolResult


def _text(text: str) -> TurnTextEvent:
    return TurnTextEvent(text=text)


def _segment_end() -> IterationFinishedEvent:
    """The segment flush boundary (one LLM iteration closed)."""
    return IterationFinishedEvent(iteration=0, has_tool_calls=False)


def _turn_finished(result: AgentResult | None = None) -> TurnFinishedEvent:
    if result is None:
        return TurnFinishedEvent(stop_reason=StopReason.COMPLETED)
    return turn_finished_event(result)


@pytest.mark.asyncio
async def test_emit_content_delta() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(output_adapter, "web:abc.main")
    input_adapter.register_connection("web:abc.main", None)
    await emitter.emit(_text("hello"))
    q = input_adapter.get_delta_queue("web:abc.main", None)
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.event_type == WebUIEventType.MODEL_CONTENT_DELTA.value
    assert envelope.payload["text"] == "hello"
    assert isinstance(envelope.payload["turn_id"], str)
    assert len(envelope.payload["turn_id"]) > 0
    assert envelope.session_id == "web:abc.main"
    assert envelope.agent_name == "main"


@pytest.mark.asyncio
async def test_turn_finished_sends_turn_end() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(output_adapter, "web:abc.main")
    input_adapter.register_connection("web:abc.main", None)
    await emitter.emit(_turn_finished(AgentResult(content="done")))
    q = input_adapter.get_delta_queue("web:abc.main", None)
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.event_type == WebUIEventType.TURN_END.value


@pytest.mark.asyncio
async def test_streaming_does_not_save_deltas() -> None:
    """Text deltas push WS events but do NOT persist content to transcript."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(
            output_adapter, "conv1.main",
            transcript_store=store,
        )
        input_adapter.register_connection("conv1.main", None)

        await emitter.emit(_text("hello"))
        await emitter.emit(_text(" world"))

        events = await store.load("conv1.main")
        assert all(e.event != WebUIEventType.MODEL_CONTENT_DELTA.value for e in events)
        assert all(e.event != WebUIEventType.ASSISTANT_TEXT.value for e in events)

        q = input_adapter.get_delta_queue("conv1.main", None)
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
            transcript_store=store,
        )
        input_adapter.register_connection(full_sid, None)

        await emitter.emit(_text("review done"))
        await emitter.emit(_turn_finished(AgentResult(content="review done")))

        # WebSocket delta events carry the FULL session id + correct agent.
        q = input_adapter.get_delta_queue(full_sid, None)
        assert q is not None
        delta_env = q.get_nowait()
        assert delta_env.event_type == WebUIEventType.MODEL_CONTENT_DELTA.value
        assert delta_env.session_id == full_sid
        assert delta_env.agent_name == "reviewer"
        envelope = q.get_nowait()
        assert envelope.event_type == WebUIEventType.TURN_END.value
        assert envelope.session_id == full_sid
        assert envelope.agent_name == "reviewer"

        # Transcript persisted under the FULL session id (not truncated).
        assert await store.load(full_sid)
        assert not await store.load("conv1.reviewer")


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
                transcript_store=store,
            )
            await em.emit(_text(sid))
            await em.emit(_turn_finished(AgentResult(content=sid)))

        assert len(await store.load("conv1.reviewer.aa11")) >= 1
        assert len(await store.load("conv1.reviewer.bb22")) >= 1
        assert await store.list_sessions() == {
            "conv1.reviewer.aa11",
            "conv1.reviewer.bb22",
        }


# ── Incremental persistence tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_segment_flush_saves_assistant_text_to_transcript() -> None:
    """Buffered text is flushed to the transcript store at the segment boundary."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(output_adapter, "conv1.main", transcript_store=store)
        input_adapter.register_connection("conv1.main", None)
        await emitter.emit(_text("Hello World"))
        await emitter.emit(_segment_end())
        records = await store.load("conv1.main")
        # turn_started is wire-only metadata. The flushed text segment is the
        # only persisted record — one complete TextDelta.
        assert len(records) == 1
        assert records[0].kind == "text_delta"


@pytest.mark.asyncio
async def test_turn_finished_flushes_remaining_text_buffer() -> None:
    """If no segment boundary fired, turn_finished flushes the buffer."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(output_adapter, "conv1.main", transcript_store=store)
        input_adapter.register_connection("conv1.main", None)
        await emitter.emit(_text("Hello World"))
        await emitter.emit(_turn_finished(AgentResult(content="done")))
        records = await store.load("conv1.main")
        assert any(record.kind == "text_delta" for record in records)
        # The terminal record IS persisted now (turn-scoped stop
        # classification); the wire-only turn_end never was.
        assert any(record.kind == "turn_finished" for record in records)


@pytest.mark.asyncio
async def test_tool_call_events_persisted_incrementally() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(output_adapter, "conv1.main", transcript_store=store)
        input_adapter.register_connection("conv1.main", None)
        await emitter.emit(
            TurnToolCallEvent(
                tool_name="read_file", call_id="call_0", arguments={"path": "/x"}
            )
        )
        await emitter.emit(
            TurnToolResultEvent(
                tool_name="read_file", call_id="call_0", output="content", seq=7
            )
        )
        records = await store.load("conv1.main")
        assert any(record.kind == "tool_call_started" for record in records)
        assert any(record.kind == "tool_result" for record in records)
        tool_result = next(
            record for record in records if isinstance(record, ToolResult)
        )
        assert tool_result.seq == 7


@pytest.mark.asyncio
async def test_tool_call_events_stream_matching_call_id() -> None:
    """Streamed tool_call/tool_result carry the SAME call_id.

    The frontend pairs a result with exactly one tool block by call_id —
    matching by tool name breaks when a turn runs parallel same-name calls.
    """
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(output_adapter, "conv1.main")
    input_adapter.register_connection("conv1.main", None)
    await emitter.emit(
        TurnToolCallEvent(
            tool_name="read_file", call_id="call_0", arguments={"path": "/x"}
        )
    )
    await emitter.emit(
        TurnToolResultEvent(
            tool_name="read_file", call_id="call_0", output="content", seq=7
        )
    )
    q = input_adapter.get_delta_queue("conv1.main", None)
    assert q is not None
    start_env = q.get_nowait()
    end_env = q.get_nowait()
    assert start_env.event_type == WebUIEventType.TOOL_CALL_START.value
    assert start_env.payload["call_id"] == "call_0"
    assert end_env.event_type == WebUIEventType.TOOL_CALL_END.value
    assert end_env.payload["call_id"] == "call_0"
    assert end_env.payload["seq"] == 7


@pytest.mark.asyncio
async def test_tool_result_requires_call_id_from_the_union() -> None:
    """A tool result always carries a call_id (union constraint).

    The retired ``ToolCallEndPayload`` tolerated a tool call without a
    call_id (the wire frame omitted the field). The core
    ``TurnToolResultEvent`` union requires a non-empty ``call_id`` — the
    runtime stamps canonical ids before emitting — so the omission path is
    structurally impossible at the sink face now.
    """
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        TurnToolResultEvent(
            tool_name="read_file", call_id="", output="content", seq=0
        )


@pytest.mark.asyncio
async def test_reasoning_not_persisted_to_transcript() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(output_adapter, "conv1.main", transcript_store=store)
        input_adapter.register_connection("conv1.main", None)
        await emitter.emit(TurnReasoningEvent(text="thinking..."))
        await emitter.emit(_turn_finished(AgentResult(content="done")))
        records = await store.load("conv1.main")
        # Reasoning persists as a flushed ThinkingDelta record (the segment
        # record), never as per-delta writes.
        assert any(record.kind == "thinking_delta" for record in records)
        assert len(records) == 2  # thinking_delta + turn_finished


@pytest.mark.asyncio
async def test_emit_content_empty_skips_persist() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(output_adapter, "conv1.main", transcript_store=store)
        input_adapter.register_connection("conv1.main", None)
        await emitter.emit(_text("   "))
        records = await store.load("conv1.main")
        assert all(record.kind != "text_delta" for record in records)


@pytest.mark.asyncio
async def test_streaming_delta_flush_persists_content() -> None:
    """Regression: the control-interceptor stream path drives text deltas;
    the segment boundary flushes them. Assistant text must still reach the
    transcript store.
    """
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(
            output_adapter, "conv1.main", transcript_store=store
        )
        input_adapter.register_connection("conv1.main", None)
        await emitter.emit(_text("Hello "))
        await emitter.emit(_text("world"))
        await emitter.emit(_segment_end())
        records = await store.load("conv1.main")
        assert any(record.kind == "text_delta" for record in records), (
            f"Expected the flushed text record in transcript, got: {[record.kind for record in records]}"
        )


# ── tool_args_delta (transient pre-tool-call warm-up signal) tests ─────────


@pytest.mark.asyncio
async def test_tool_args_delta_first_fragment_sent_immediately() -> None:
    """tool_args_delta: 首 fragment 立即外发; 不落 store、不 flush 打开中的文本段。"""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(output_adapter, "conv1.main", transcript_store=store)
        input_adapter.register_connection("conv1.main", None)

        await emitter.emit(_text("partial "))  # 打开中的文本段, 不得被预热信号 flush
        fragment = '{"path": "/tmp'
        await emitter.emit(
            CoreToolArgsDeltaEvent(
                call_id="call_0", tool_name="read_file", args_fragment=fragment
            )
        )

        q = input_adapter.get_delta_queue("conv1.main", None)
        assert q is not None
        text_env = q.get_nowait()
        args_env = q.get_nowait()
        assert text_env.event_type == WebUIEventType.MODEL_CONTENT_DELTA.value
        assert args_env.event_type == WebUIEventType.TOOL_ARGS_DELTA.value
        assert args_env.payload["tool"] == "read_file"
        assert args_env.payload["call_id"] == "call_0"
        assert args_env.payload["turn_id"] == text_env.payload["turn_id"]
        assert len(args_env.payload["turn_id"]) > 0
        assert args_env.payload["chars"] == len(fragment)
        assert args_env.payload["preview"] == fragment
        assert q.empty()

        # 瞬态信号: store 无任何记录 —— 尤其无 AssistantTextEvent(文本段未 flush)。
        events = await store.load("conv1.main")
        assert events == []


@pytest.mark.asyncio
async def test_tool_args_delta_throttle_leading_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """窗口内吞 fragment; 过窗后一次外发携带累积 chars + 有界尾部 preview。"""
    clock = {"t": 1000.0}
    monkeypatch.setattr(web_bot_module.time, "monotonic", lambda: clock["t"])

    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(output_adapter, "conv1.main")
    input_adapter.register_connection("conv1.main", None)

    async def frag(fragment: str) -> None:
        await emitter.emit(
            CoreToolArgsDeltaEvent(
                call_id="call_0", tool_name="read_file", args_fragment=fragment
            )
        )

    await frag("a" * 5)  # t=1000.0: 首 fragment 立即外发(leading edge)
    clock["t"] = 1000.01
    await frag("b" * 10)  # 窗口内: 吞掉
    clock["t"] = 1000.02
    await frag("c" * 15)  # 窗口内: 吞掉

    q = input_adapter.get_delta_queue("conv1.main", None)
    assert q is not None
    first = q.get_nowait()
    assert first.event_type == WebUIEventType.TOOL_ARGS_DELTA.value
    assert first.payload["chars"] == 5
    assert first.payload["preview"] == "a" * 5
    assert q.empty()  # 窗口内无第二次外发

    clock["t"] = 1000.2  # 越过 100ms 节流窗口
    await frag("d" * 400)
    second = q.get_nowait()
    accumulated = "a" * 5 + "b" * 10 + "c" * 15 + "d" * 400
    assert second.event_type == WebUIEventType.TOOL_ARGS_DELTA.value
    assert second.payload["chars"] == len(accumulated)  # 累积计数含被吞 fragment
    assert second.payload["preview"] == accumulated[-200:]  # 有界尾部预览
    assert len(second.payload["preview"]) == 200
    assert q.empty()


@pytest.mark.asyncio
async def test_tool_call_start_clears_args_stream_state() -> None:
    """tool_call 随后照常外发, 并清掉该 call_id 的预热账目。"""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(output_adapter, "conv1.main")
    input_adapter.register_connection("conv1.main", None)

    await emitter.emit(
        CoreToolArgsDeltaEvent(
            call_id="call_0", tool_name="read_file", args_fragment='{"path"'
        )
    )
    assert emitter._args_stream_state  # 预热账目已在位

    await emitter.emit(
        TurnToolCallEvent(
            tool_name="read_file", call_id="call_0", arguments={"path": "/x"}
        )
    )

    q = input_adapter.get_delta_queue("conv1.main", None)
    assert q is not None
    assert q.get_nowait().event_type == WebUIEventType.TOOL_ARGS_DELTA.value
    start_env = q.get_nowait()
    assert start_env.event_type == WebUIEventType.TOOL_CALL_START.value
    assert start_env.payload["call_id"] == "call_0"
    assert emitter._args_stream_state == {}


@pytest.mark.asyncio
async def test_turn_end_clears_args_stream_state() -> None:
    """LENGTH 截断 / 规范 id 不一致的孤儿账目由回合结束清场。"""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(output_adapter, "conv1.main")
    input_adapter.register_connection("conv1.main", None)

    await emitter.emit(
        CoreToolArgsDeltaEvent(
            call_id="orphan_1", tool_name="read_file", args_fragment="x"
        )
    )
    assert emitter._args_stream_state

    await emitter.emit(_turn_finished(AgentResult(content="done")))
    assert emitter._args_stream_state == {}


@pytest.mark.asyncio
async def test_tool_args_delta_is_noop_for_acp_projection() -> None:
    """ACP 投影忽略预热信号: 基类钩子默认 no-op —— 无外发、无持久化。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONLTranscriptStore(Path(tmp))
        hub = AcpEmitterHub()
        seen: list[TurnEvent] = []

        async def listener(event: TurnEvent) -> None:
            seen.append(event)

        hub.register("conv1.main", listener)
        emitter = AcpTurnEmitter(hub, "conv1.main", transcript_store=store)

        await emitter.emit(
            CoreToolArgsDeltaEvent(
                call_id="call_0", tool_name="read_file", args_fragment="x"
            )
        )
        assert seen == []
        # Transient warm-up: nothing ever reaches the store.
        assert await store.load("conv1.main") == []


def test_tool_args_delta_event_roundtrip() -> None:
    ev = ToolArgsDeltaEvent(
        session_id="abc.main", agent_name="main",
        tool="read_file", call_id="call_0", turn_id="a1b2c3d4e5f6",
        chars=42, preview='{"path": "/tmp/x"}',
    )
    loaded = ServerEvent.from_dict(ev.to_dict())
    assert isinstance(loaded, ToolArgsDeltaEvent)
    assert loaded.tool == "read_file"
    assert loaded.call_id == "call_0"
    assert loaded.turn_id == "a1b2c3d4e5f6"
    assert loaded.chars == 42
    assert loaded.preview == '{"path": "/tmp/x"}'
    assert loaded.event == WebUIEventType.TOOL_ARGS_DELTA.value


# ── approval / usage projections (streamed cards over the WS envelope) ─────


@pytest.mark.asyncio
async def test_approval_requested_streams_card_and_persists_record() -> None:
    """approval_requested: one WS envelope per suspension + one durable record.

    Since the W6 transcript cutover the approval lifecycle persists (the
    durable L2 record set); the folded replay ignores it, so history
    blocks are unchanged.
    """
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)
        store = JSONLTranscriptStore(Path(tmp))
        emitter = WebBotEmitter(output_adapter, "conv1.main", transcript_store=store)
        input_adapter.register_connection("conv1.main", None)

        await emitter.emit(_text("running it"))
        await emitter.emit(
            CoreApprovalRequestedEvent(
                tool_name="write_file",
                call_id="call_0",
                prompt="Approval Required [DANGEROUS]\nTool: write_file",
            )
        )

        q = input_adapter.get_delta_queue("conv1.main", None)
        assert q is not None
        text_env = q.get_nowait()
        approval_env = q.get_nowait()
        assert text_env.event_type == WebUIEventType.MODEL_CONTENT_DELTA.value
        assert approval_env.event_type == WebUIEventType.APPROVAL_REQUESTED.value
        assert approval_env.payload["tool_name"] == "write_file"
        assert approval_env.payload["call_id"] == "call_0"
        assert "write_file" in approval_env.payload["prompt"]
        assert approval_env.payload["turn_id"] == text_env.payload["turn_id"]
        assert q.empty()

        # The card persists as its durable record. The streamed text is
        # still segment-buffered here (approval is not a flush boundary);
        # it lands as its TextDelta record at the next boundary.
        records = await store.load("conv1.main")
        kinds = [record.kind for record in records]
        assert kinds == ["approval_requested"]


@pytest.mark.asyncio
async def test_approval_resolved_streams_card() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    # The resumer emits approval_resolved on the RESUMED binding (same turn id
    # as the suspended attempt) — mirror that wiring.
    emitter = WebBotEmitter(output_adapter, "conv1.main", turn_id="turn_same", resumed=True)
    input_adapter.register_connection("conv1.main", None)

    await emitter.emit(CoreApprovalResolvedEvent(call_id="call_0", approved=True))

    q = input_adapter.get_delta_queue("conv1.main", None)
    assert q is not None
    env = q.get_nowait()
    assert env.event_type == WebUIEventType.APPROVAL_RESOLVED.value
    assert env.payload["call_id"] == "call_0"
    assert env.payload["approved"] is True
    assert env.payload["turn_id"] == "turn_same"


@pytest.mark.asyncio
async def test_usage_summary_streams_token_snapshot() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(output_adapter, "conv1.main")
    input_adapter.register_connection("conv1.main", None)

    await emitter.emit(_text("answer"))
    await emitter.emit(
        UsageEvent(
            usage=TokenUsage(
                input_tokens=10,
                output_tokens=5,
                reasoning_tokens=2,
                cache_read_input_tokens=7,
            )
        )
    )

    q = input_adapter.get_delta_queue("conv1.main", None)
    assert q is not None
    text_env = q.get_nowait()
    usage_env = q.get_nowait()
    assert usage_env.event_type == WebUIEventType.USAGE_SUMMARY.value
    assert usage_env.payload["input_tokens"] == 10
    assert usage_env.payload["output_tokens"] == 5
    assert usage_env.payload["reasoning_tokens"] == 2
    assert usage_env.payload["cache_read_tokens"] == 7
    assert usage_env.payload["cache_creation_tokens"] == 0
    # total = input + cache_read + cache_creation + output (reasoning is a
    # subset of output, not added on top).
    assert usage_env.payload["total_tokens"] == 22
    assert usage_env.payload["turn_id"] == text_env.payload["turn_id"]


@pytest.mark.asyncio
async def test_resumed_binding_continues_same_turn_id() -> None:
    """A resumed emitter (TurnBinding.resumed) adopts the suspended turn's id:
    the first content event announces no second TurnStarted — the WebUI turn
    card continues, and approval_resolved rides the SAME turn id."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    emitter = WebBotEmitter(
        output_adapter, "conv1.main", turn_id="suspended_turn", resumed=True
    )
    input_adapter.register_connection("conv1.main", None)

    await emitter.emit(CoreApprovalResolvedEvent(call_id="call_0", approved=True))
    await emitter.emit(TurnToolResultEvent(tool_name="write_file", call_id="call_0", output="written"))

    q = input_adapter.get_delta_queue("conv1.main", None)
    assert q is not None
    resolved_env = q.get_nowait()
    tool_env = q.get_nowait()
    assert resolved_env.payload["turn_id"] == "suspended_turn"
    assert tool_env.payload["turn_id"] == "suspended_turn"
    # The resumed leg emits NO turn_start frame (the hub pre-activated the
    # projector — the wire keeps the original turn card).
    assert tool_env.event_type == WebUIEventType.TOOL_CALL_END.value


@pytest.mark.asyncio
async def test_approval_and_usage_projections_are_noop_for_acp() -> None:
    """ACP projection keeps the base no-op hooks: no hub event.

    The shared recording lifecycle still persists the durable approval /
    usage records (they fold into the turn and never surface as blocks).
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = JSONLTranscriptStore(Path(tmp))
        hub = AcpEmitterHub()
        seen: list[TurnEvent] = []

        async def listener(event: TurnEvent) -> None:
            seen.append(event)

        hub.register("conv1.main", listener)
        emitter = AcpTurnEmitter(hub, "conv1.main", transcript_store=store)

        await emitter.emit(
            CoreApprovalRequestedEvent(tool_name="write_file", call_id="c0", prompt="p")
        )
        await emitter.emit(CoreApprovalResolvedEvent(call_id="c0", approved=False))
        await emitter.emit(UsageEvent(usage=TokenUsage(input_tokens=1)))

        assert seen == []
        records = await store.load("conv1.main")
        assert [record.kind for record in records] == [
            "approval_requested",
            "approval_resolved",
            "usage_summary",
        ]
        # Observation-only records replay to nothing (no empty turns).
        from bot.webui.transcript_store import materialize_records

        assert materialize_records(records) == []


def test_approval_and_usage_event_roundtrip() -> None:
    requested = ServerEvent.from_dict(
        ApprovalRequestedEvent(
            session_id="abc.main", agent_name="main", tool_name="write_file",
            call_id="call_0", turn_id="t1", prompt="Approval Required",
        ).to_dict()
    )
    assert isinstance(requested, ApprovalRequestedEvent)
    assert requested.call_id == "call_0"
    assert requested.event == WebUIEventType.APPROVAL_REQUESTED.value

    resolved = ServerEvent.from_dict(
        ApprovalResolvedEvent(
            session_id="abc.main", agent_name="main", call_id="call_0",
            approved=True, turn_id="t1",
        ).to_dict()
    )
    assert isinstance(resolved, ApprovalResolvedEvent)
    assert resolved.approved is True
    assert resolved.event == WebUIEventType.APPROVAL_RESOLVED.value

    usage = ServerEvent.from_dict(
        UsageSummaryEvent(
            session_id="abc.main", agent_name="main", input_tokens=3,
            output_tokens=4, total_tokens=7, turn_id="t1",
        ).to_dict()
    )
    assert isinstance(usage, UsageSummaryEvent)
    assert usage.total_tokens == 7
    assert usage.event == WebUIEventType.USAGE_SUMMARY.value


# ── CompositeEmitter tests ────────────────────────────────────────────────


class _StubSink(TurnEventSink):
    """Recording sink that tracks the events it received."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def _dispatch(self, event: TurnEvent) -> None:
        match event:
            case TurnTextEvent(text=text):
                self.calls.append(f"delta:{text}")
            case TurnFinishedEvent():
                self.calls.append("complete")

    def wants_streaming(self) -> bool:
        return True


class _FailingSink(TurnEventSink):
    """Sink that raises on every event."""

    async def _dispatch(self, event: TurnEvent) -> None:
        raise RuntimeError("boom")


class _EventRecordingSink(TurnEventSink):
    """Records every event kind (for fan-out assertions)."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    async def _dispatch(self, event: TurnEvent) -> None:
        self.events.append(event.kind)


@pytest.mark.asyncio
async def test_composite_fans_out_to_all_children() -> None:
    """CompositeEmitter delegates to all children."""
    stub1 = _StubSink()
    stub2 = _StubSink()
    composite = CompositeEmitter((stub1, stub2))

    await composite.emit(_text("hello"))
    await composite.emit(_turn_finished(AgentResult(content="done")))

    assert stub1.calls == ["delta:hello", "complete"]
    assert stub2.calls == ["delta:hello", "complete"]


@pytest.mark.asyncio
async def test_composite_fans_out_tool_args_delta() -> None:
    """tool_args_delta 经 CompositeEmitter 扇出到所有子 sink。"""
    stub1 = _EventRecordingSink()
    stub2 = _EventRecordingSink()
    composite = CompositeEmitter((stub1, stub2))

    await composite.emit(
        CoreToolArgsDeltaEvent(
            call_id="call_0", tool_name="read_file", args_fragment="x"
        )
    )

    assert stub1.events == ["tool_args_delta"]
    assert stub2.events == ["tool_args_delta"]


@pytest.mark.asyncio
async def test_composite_error_isolation() -> None:
    """One failing child does not prevent others from receiving events."""
    stub = _StubSink()
    failing = _FailingSink()
    composite = CompositeEmitter((failing, stub))

    await composite.emit(_text("test"))
    assert stub.calls == ["delta:test"]


@pytest.mark.asyncio
async def test_composite_wants_streaming_or_semantics() -> None:
    """wants_streaming returns True if ANY child wants streaming."""

    class _NoStreaming(TurnEventSink):
        async def _dispatch(self, event: TurnEvent) -> None:
            pass

    composite = CompositeEmitter((_NoStreaming(), _StubSink()))
    assert composite.wants_streaming() is True

    composite2 = CompositeEmitter((_NoStreaming(), _NoStreaming()))
    assert composite2.wants_streaming() is False
