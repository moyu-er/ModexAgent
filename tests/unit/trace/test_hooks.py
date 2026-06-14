"""Tests for TraceCollectorHook."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from framework.core.agent import AgentContext
from framework.core.emitter import AgentResult
from framework.core.session_id import SessionId
from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig, ToolResult
from framework.core.types import LLMResponse, ToolCall
from framework.memory.history import ListMessageHistory
from framework.runtime.enums import AgentKind, OperationKind, OperationStatus, TurnCustomKey, TurnPhase
from framework.runtime.models import TurnIdentity
from framework.runtime.services import AgentRuntime, AgentRuntimeServices
from framework.agents.react.state import ReActTurnState
from framework.trace.hooks import TraceCollectorHook
from framework.trace.store import JsonFileTraceStore, TraceStore


# -- helpers ------------------------------------------------------------------


def _make_trace_context(session_id: str) -> AgentContext:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="test", session=SessionId.from_str(session_id), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=SessionId.from_str(session_id),
        runtime=runtime,
    )


def _make_hook(tmp_path: Path, *, enabled: bool = True) -> TraceCollectorHook:
    store = JsonFileTraceStore(tmp_path / "traces")
    return TraceCollectorHook(store, enabled=enabled)


async def _collect_records(tmp_path: Path, session_id: str) -> list:
    store = JsonFileTraceStore(tmp_path / "traces")
    return await store.list_by_session(session_id)


# -- tests --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_turn_records_turn_start(tmp_path: Path) -> None:
    ctx = _make_trace_context("s1")
    hook = _make_hook(tmp_path)

    await hook.before_turn(ctx)

    records = await _collect_records(tmp_path, "s1")
    assert len(records) == 1
    rec = records[0]
    assert rec.kind == OperationKind.TURN_START
    assert rec.status == OperationStatus.COMPLETED
    # trace_id should be stored in turn state
    trace_id = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
    assert trace_id is not None
    assert rec.trace_id == trace_id


@pytest.mark.asyncio
async def test_after_llm_response_records_llm_call(tmp_path: Path) -> None:
    ctx = _make_trace_context("s2")
    hook = _make_hook(tmp_path)

    # Seed trace_id
    await hook.before_turn(ctx)

    response = LLMResponse(
        content="hello",
        tool_calls=[ToolCall(call_id="c1", tool_name="search", arguments={"q": "test"})],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )
    await hook.after_llm_response(ctx, response)

    records = await _collect_records(tmp_path, "s2")
    # before_turn + after_llm_response
    assert len(records) == 2
    rec = records[1]
    assert rec.kind == OperationKind.LLM_CALL
    assert rec.status == OperationStatus.COMPLETED
    assert rec.metadata["finish_reason"] == "tool_calls"
    assert rec.metadata["has_tool_calls"] is True
    assert rec.metadata["content"] == "hello"
    assert rec.metadata["tool_calls"] == [
        {"tool_name": "search", "arguments": '{"q": "test"}'},
    ]
    assert rec.metadata["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}


@pytest.mark.asyncio
async def test_before_tool_execution_records_tool_batch(tmp_path: Path) -> None:
    ctx = _make_trace_context("s3")
    hook = _make_hook(tmp_path)

    await hook.before_turn(ctx)

    tool_calls = [
        ToolCall(call_id="c1", tool_name="search", arguments={"q": "a"}),
        ToolCall(call_id="c2", tool_name="read", arguments={"path": "/tmp"}),
    ]
    await hook.before_tool_execution(ctx, tool_calls)

    records = await _collect_records(tmp_path, "s3")
    assert len(records) == 2
    rec = records[1]
    assert rec.kind == OperationKind.TOOL_BATCH
    assert rec.status == OperationStatus.RUNNING
    assert rec.metadata["tool_count"] == 2
    assert rec.metadata["tool_names"] == ["search", "read"]


@pytest.mark.asyncio
async def test_after_tool_execution_records_per_tool(tmp_path: Path) -> None:
    ctx = _make_trace_context("s4")
    hook = _make_hook(tmp_path)

    await hook.before_turn(ctx)

    results = [
        ToolResult(tool_name="search", result="found", execution_time=0.05),
        ToolResult(tool_name="read", error="file not found", execution_time=0.01),
    ]
    await hook.after_tool_execution(ctx, results)

    records = await _collect_records(tmp_path, "s4")
    # before_turn + 2 tool results
    assert len(records) == 3
    r0 = records[1]
    assert r0.kind == OperationKind.TOOL_CALL
    assert r0.status == OperationStatus.COMPLETED
    assert r0.metadata["tool_name"] == "search"
    assert r0.error is None

    r1 = records[2]
    assert r1.kind == OperationKind.TOOL_CALL
    assert r1.status == OperationStatus.FAILED
    assert r1.metadata["tool_name"] == "read"
    assert r1.error == "file not found"


@pytest.mark.asyncio
async def test_finally_turn_records_turn_end(tmp_path: Path) -> None:
    ctx = _make_trace_context("s5")
    hook = _make_hook(tmp_path)

    await hook.before_turn(ctx)
    await hook.finally_turn(ctx, AgentResult(content="done"))

    records = await _collect_records(tmp_path, "s5")
    assert len(records) == 2
    rec = records[1]
    assert rec.kind == OperationKind.TURN_END
    assert rec.status == OperationStatus.COMPLETED
    assert rec.error is None
    assert rec.metadata["stop_reason"] == "completed"
    assert rec.metadata["content"] == "done"


@pytest.mark.asyncio
async def test_finally_turn_records_error(tmp_path: Path) -> None:
    ctx = _make_trace_context("s6")
    hook = _make_hook(tmp_path)

    await hook.before_turn(ctx)
    await hook.finally_turn(ctx, AgentResult(error="boom"))

    records = await _collect_records(tmp_path, "s6")
    rec = records[1]
    assert rec.kind == OperationKind.TURN_END
    assert rec.status == OperationStatus.FAILED
    assert rec.error == "boom"


@pytest.mark.asyncio
async def test_disabled_hook_records_nothing(tmp_path: Path) -> None:
    ctx = _make_trace_context("s7")
    hook = _make_hook(tmp_path, enabled=False)

    await hook.before_turn(ctx)
    await hook.after_llm_response(ctx, LLMResponse(content="x"))
    await hook.before_tool_execution(ctx, [ToolCall(call_id="c1", tool_name="t", arguments={})])
    await hook.after_tool_execution(ctx, [ToolResult(tool_name="t", result="ok")])
    await hook.finally_turn(ctx, AgentResult(content="done"))

    records = await _collect_records(tmp_path, "s7")
    assert len(records) == 0


# -- trace content: tool calls, results, messages ------------------------------


@pytest.mark.asyncio
async def test_llm_response_captures_tool_call_arguments(tmp_path: Path) -> None:
    """after_llm_response records tool_call names + arguments for auditing."""
    ctx = _make_trace_context("s_tc")
    hook = _make_hook(tmp_path)

    await hook.before_turn(ctx)
    response = LLMResponse(
        content="Let me search and read.",
        tool_calls=[
            ToolCall(call_id="c1", tool_name="search", arguments={"q": "bug"}),
            ToolCall(call_id="c2", tool_name="read", arguments={"path": "/tmp/x"}),
        ],
        finish_reason="tool_calls",
    )
    await hook.after_llm_response(ctx, response)

    records = await _collect_records(tmp_path, "s_tc")
    rec = records[-1]
    assert rec.kind == OperationKind.LLM_CALL
    assert rec.metadata["content"] == "Let me search and read."
    assert rec.metadata["has_tool_calls"] is True
    tool_calls = rec.metadata["tool_calls"]
    assert len(tool_calls) == 2
    assert tool_calls[0]["tool_name"] == "search"
    assert "bug" in tool_calls[0]["arguments"]
    assert tool_calls[1]["tool_name"] == "read"
    assert "/tmp/x" in tool_calls[1]["arguments"]


@pytest.mark.asyncio
async def test_llm_response_captures_reasoning_content(tmp_path: Path) -> None:
    """Reasoning (thinking) content is preserved in trace."""
    ctx = _make_trace_context("s_reason")
    hook = _make_hook(tmp_path)

    await hook.before_turn(ctx)
    response = LLMResponse(content="Answer.", reasoning_content="Step 1: think\nStep 2: conclude")
    await hook.after_llm_response(ctx, response)

    records = await _collect_records(tmp_path, "s_reason")
    rec = records[-1]
    assert "Step 1: think" in rec.metadata["reasoning"]


@pytest.mark.asyncio
async def test_tool_execution_captures_result_content(tmp_path: Path) -> None:
    """after_tool_execution records the tool result text in trace."""
    ctx = _make_trace_context("s_result")
    hook = _make_hook(tmp_path)

    await hook.before_turn(ctx)
    results = [ToolResult(tool_name="read", result="file contents here", execution_time=0.02)]
    await hook.after_tool_execution(ctx, results)

    records = await _collect_records(tmp_path, "s_result")
    rec = records[-1]
    assert rec.metadata["tool_name"] == "read"
    assert rec.metadata["result"] == "file contents here"
    assert rec.metadata["duration_ms"] == 20


@pytest.mark.asyncio
async def test_tool_execution_result_truncated(tmp_path: Path) -> None:
    """Long tool results are truncated in file trace."""
    ctx = _make_trace_context("s_trunc")
    hook = _make_hook(tmp_path)

    await hook.before_turn(ctx)
    long_result = "x" * 5000
    results = [ToolResult(tool_name="read", result=long_result)]
    await hook.after_tool_execution(ctx, results)

    records = await _collect_records(tmp_path, "s_trunc")
    rec = records[-1]
    assert "truncated" in rec.metadata["result"]
    assert len(rec.metadata["result"]) < len(long_result)


@pytest.mark.asyncio
async def test_finally_turn_captures_content_text(tmp_path: Path) -> None:
    """finally_turn stores the actual content, not just content_length."""
    ctx = _make_trace_context("s_final")
    hook = _make_hook(tmp_path)

    await hook.before_turn(ctx)
    await hook.finally_turn(ctx, AgentResult(content="task completed successfully", stop_reason="completed"))

    records = await _collect_records(tmp_path, "s_final")
    rec = records[-1]
    assert rec.kind == OperationKind.TURN_END
    assert rec.metadata["content"] == "task completed successfully"
    assert rec.metadata["stop_reason"] == "completed"


@pytest.mark.asyncio
async def test_before_tool_execution_captures_full_arguments(tmp_path: Path) -> None:
    """before_tool_execution records tool_arguments alongside tool_names."""
    ctx = _make_trace_context("s_args")
    hook = _make_hook(tmp_path)

    await hook.before_turn(ctx)
    tool_calls = [
        ToolCall(call_id="c1", tool_name="write", arguments={"path": "/out/OUTPUT.md", "content": "done"}),
    ]
    await hook.before_tool_execution(ctx, tool_calls)

    records = await _collect_records(tmp_path, "s_args")
    rec = records[-1]
    assert rec.metadata["tool_names"] == ["write"]
    tool_args = rec.metadata["tool_arguments"]
    assert len(tool_args) == 1
    assert tool_args[0]["tool_name"] == "write"
    assert "OUTPUT.md" in tool_args[0]["arguments"]


# -- multi-store ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_stores_all_receive_records(tmp_path: Path) -> None:
    """When multiple stores are configured, every store receives the record."""
    store_a = JsonFileTraceStore(tmp_path / "a")
    store_b = JsonFileTraceStore(tmp_path / "b")
    hook = TraceCollectorHook(stores=[store_a, store_b])

    ctx = _make_trace_context("s_multi")
    await hook.before_turn(ctx)

    records_a = await store_a.list_by_session("s_multi")
    records_b = await store_b.list_by_session("s_multi")
    assert len(records_a) == 1
    assert len(records_b) == 1
    assert records_a[0].trace_id == records_b[0].trace_id


@pytest.mark.asyncio
async def test_single_store_failure_does_not_block_others(tmp_path: Path) -> None:
    """If one store fails, other stores still receive the record."""
    store_a = JsonFileTraceStore(tmp_path / "a")

    class _FailingStore(TraceStore):
        async def save(self, record):
            raise RuntimeError("simulated failure")
        async def list_by_session(self, session_id):
            return []
        async def list_by_trace_id(self, trace_id):
            return []

    hook = TraceCollectorHook(stores=[store_a, _FailingStore()])

    ctx = _make_trace_context("s_skip")
    await hook.before_turn(ctx)

    records_a = await store_a.list_by_session("s_skip")
    assert len(records_a) == 1, "Healthy store must still receive record"
