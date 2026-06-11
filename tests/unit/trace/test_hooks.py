"""Tests for TraceCollectorHook."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from framework.core.agent import AgentContext, AgentSessionMeta
from framework.core.emitter import AgentResult
from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig, ToolResult
from framework.core.types import LLMResponse, ToolCall
from framework.memory.history import ListMessageHistory
from framework.runtime.enums import AgentKind, OperationKind, OperationStatus, TurnCustomKey, TurnPhase
from framework.runtime.models import TurnIdentity
from framework.runtime.services import AgentRuntime, AgentRuntimeServices
from framework.agents.react.state import ReActTurnState
from framework.trace.hooks import TraceCollectorHook
from framework.trace.store import JsonFileTraceStore


# -- helpers ------------------------------------------------------------------


def _make_trace_context(session_id: str) -> AgentContext:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="test", session_id=session_id, turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session_id=session_id,
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
    assert rec.metadata["tool_call_names"] == ["search"]
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
    assert rec.metadata["content_length"] == 4


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
