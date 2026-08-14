"""Tests for immutable root span lifecycle emission."""

from __future__ import annotations

from pathlib import Path

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.root_span_hook import RootSpanHook
from modex_agent.trace.semconv import GenAiAttr, SpanName, SpanStatusCode
from modex_agent.trace.session_state import TraceSessionState


def _make_context(
    session_id: str,
    *,
    parent_session_id: str | None = None,
) -> AgentContext:
    session = SessionInfo(
        session_id=session_id,
        agent_name="worker",
        parent_session_id=parent_session_id,
    )
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="worker", session=session, turn_id="turn-1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([{"role": "user", "content": "hello"}]),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=session,
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
    )


def _make_hook(tmp_path: Path) -> tuple[RootSpanHook, OtelSpanTraceStore]:
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    return RootSpanHook(session=TraceSessionState(), store=store), store


async def test_root_span_not_emitted_in_start_node_turn(tmp_path: Path) -> None:
    hook, store = _make_hook(tmp_path)
    context = _make_context("session.worker")

    await hook.start_node_turn(context)

    assert await store.list_by_session("session.worker") == []


async def test_root_span_emitted_once_in_finally_graph(tmp_path: Path) -> None:
    hook, store = _make_hook(tmp_path)
    context = _make_context("session.worker")

    await hook.start_node_turn(context)
    assert context.runtime is not None
    trace_id = str(context.runtime.state.custom[TurnCustomKey.TRACE_ID])
    hook._session.turn_usage[trace_id] = {
        "input_tokens": 7,
        "output_tokens": 3,
    }
    await hook.finally_graph(context, result=AgentResult(content="world"))

    spans = await store.list_by_session("session.worker")
    assert len(spans) == 1
    span = spans[0]
    assert span.name == SpanName.INVOKE_AGENT.value
    assert span.attributes[GenAiAttr.LANGFUSE_OBSERVATION_INPUT] == "hello"
    assert span.attributes[GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT] == "world"
    assert span.attributes[GenAiAttr.USAGE_INPUT_TOKENS] == 7
    assert span.attributes[GenAiAttr.USAGE_OUTPUT_TOKENS] == 3


async def test_root_span_has_error_status_on_failure(tmp_path: Path) -> None:
    hook, store = _make_hook(tmp_path)
    context = _make_context("session.worker")

    await hook.start_node_turn(context)
    await hook.finally_graph(context, result=None)

    spans = await store.list_by_session("session.worker")
    assert len(spans) == 1
    assert spans[0].status.code == SpanStatusCode.ERROR


async def test_subagent_root_span_parent_is_handoff(tmp_path: Path) -> None:
    hook, store = _make_hook(tmp_path)
    context = _make_context(
        "child.worker",
        parent_session_id="parent.main",
    )
    assert context.runtime is not None
    context.runtime.state.custom[TurnCustomKey.PARENT_SPAN_ID] = "handoff-span"
    context.runtime.state.custom[TurnCustomKey.TRACE_ID] = "inherited-trace"

    await hook.start_node_turn(context)
    assert context.runtime.state.custom[TurnCustomKey.TRACE_ID] == "inherited-trace"
    await hook.finally_graph(context, result=AgentResult(content="complete"))

    spans = await store.list_by_session("child.worker")
    assert len(spans) == 1
    assert spans[0].parent_span_id == "handoff-span"
    assert spans[0].trace_id == "inherited-trace"
    assert spans[0].attributes[GenAiAttr.LANGFUSE_SESSION_ID] == "parent.main"


async def test_subagent_reuses_parent_trace_id(tmp_path: Path) -> None:
    """When TRACE_ID is pre-set in state.custom, start_node_turn must not overwrite it."""
    hook, store = _make_hook(tmp_path)
    context = _make_context("child.worker")
    assert context.runtime is not None
    context.runtime.state.custom[TurnCustomKey.TRACE_ID] = "parent-trace-id"

    await hook.start_node_turn(context)

    assert context.runtime.state.custom[TurnCustomKey.TRACE_ID] == "parent-trace-id"
    # root_span_id is always freshly generated even when trace_id is inherited
    root_span_id = context.runtime.state.custom[TurnCustomKey.ROOT_SPAN_ID]
    assert root_span_id is not None
    assert root_span_id != "parent-trace-id"

    await hook.finally_graph(context, result=AgentResult(content="done"))

    spans = await store.list_by_session("child.worker")
    assert len(spans) == 1
    assert spans[0].trace_id == "parent-trace-id"
