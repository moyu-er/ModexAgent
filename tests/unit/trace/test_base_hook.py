"""Tests for BaseTraceHook shared infrastructure."""

from __future__ import annotations

from pathlib import Path

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.base_hook import BaseTraceHook
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.scoring import compute_metrics
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanName
from modex_agent.trace.session_state import TraceSessionState
from modex_agent.trace.store import SpanStatus
from modex_agent.tools.manager import InMemoryToolManager

# -- helpers ------------------------------------------------------------------


def _make_ctx(
    session_id: str = "s1",
    *,
    trace_id: str | None = None,
    root_span_id: str | None = None,
) -> AgentContext:
    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test",
            session=SessionInfo.from_str(session_id),
            turn_id="t1",
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    if trace_id is not None:
        state.custom[TurnCustomKey.TRACE_ID] = trace_id
    if root_span_id is not None:
        state.custom[TurnCustomKey.ROOT_SPAN_ID] = root_span_id
    services = AgentRuntimeServices()
    runtime = AgentRuntime(services=services, state=state)
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(session_id),
        runtime=runtime,
    )


def _make_store(tmp_path: Path) -> OtelSpanTraceStore:
    return OtelSpanTraceStore(base_dir=tmp_path / "traces")


# -- tests --------------------------------------------------------------------


def test_base_hook_initialization(tmp_path: Path) -> None:
    session = TraceSessionState()
    store = _make_store(tmp_path)
    hook = BaseTraceHook(session=session, store=store)
    assert hook._enabled is True


def test_new_span_id_generates_16_chars() -> None:
    hook = BaseTraceHook(session=TraceSessionState(), store=None)
    span_id = hook._new_span_id()
    assert len(span_id) == 16
    assert all(c in "0123456789abcdef" for c in span_id)


def test_trace_id_reads_from_state() -> None:
    hook = BaseTraceHook(session=TraceSessionState(), store=None)
    ctx = _make_ctx(trace_id="abc123def456")
    assert hook._trace_id(ctx) == "abc123def456"


def test_root_span_id_reads_from_state() -> None:
    hook = BaseTraceHook(session=TraceSessionState(), store=None)
    ctx = _make_ctx(root_span_id="root-span-123")
    assert hook._root_span_id(ctx) == "root-span-123"


def test_build_base_attrs_has_required_fields() -> None:
    hook = BaseTraceHook(session=TraceSessionState(), store=None)
    ctx = _make_ctx()
    attrs = hook._build_base_attrs(ctx, "chat")
    assert GenAiAttr.AGENT_NAME in attrs
    assert GenAiAttr.OPERATION_NAME in attrs
    assert attrs[GenAiAttr.OPERATION_NAME] == "chat"
    assert GenAiAttr.CONVERSATION_ID in attrs
    assert GenAiAttr.LANGFUSE_SESSION_ID in attrs


def test_enabled_false_when_store_none() -> None:
    hook = BaseTraceHook(session=TraceSessionState(), store=None)
    assert hook._enabled is False


def test_build_base_attrs_sets_langfuse_environment(tmp_path: Path) -> None:
    """When environment is set, _build_base_attrs includes langfuse.environment."""
    session = TraceSessionState()
    store = _make_store(tmp_path)
    hook = BaseTraceHook(session=session, store=store, environment="staging")
    ctx = _make_ctx()
    attrs = hook._build_base_attrs(ctx, "chat")
    assert attrs[GenAiAttr.LANGFUSE_ENVIRONMENT] == "staging"


def test_build_base_attrs_sets_langfuse_version(tmp_path: Path) -> None:
    """When version is set, _build_base_attrs includes langfuse.version."""
    session = TraceSessionState()
    store = _make_store(tmp_path)
    hook = BaseTraceHook(session=session, store=store, version="2.1.0")
    ctx = _make_ctx()
    attrs = hook._build_base_attrs(ctx, "chat")
    assert attrs[GenAiAttr.LANGFUSE_VERSION] == "2.1.0"


def test_build_base_attrs_sets_langfuse_tags(tmp_path: Path) -> None:
    """When tags is non-empty, _build_base_attrs includes langfuse.trace.tags."""
    session = TraceSessionState()
    store = _make_store(tmp_path)
    hook = BaseTraceHook(session=session, store=store, tags=["eval", "math-qa"])
    ctx = _make_ctx()
    attrs = hook._build_base_attrs(ctx, "chat")
    assert attrs[GenAiAttr.LANGFUSE_TRACE_TAGS] == ["eval", "math-qa"]


def test_build_base_attrs_omits_environment_when_default(tmp_path: Path) -> None:
    """When environment is 'default', _build_base_attrs omits langfuse.environment."""
    session = TraceSessionState()
    store = _make_store(tmp_path)
    hook = BaseTraceHook(session=session, store=store)
    ctx = _make_ctx()
    attrs = hook._build_base_attrs(ctx, "chat")
    assert GenAiAttr.LANGFUSE_ENVIRONMENT not in attrs


def test_build_base_attrs_omits_version_when_none(tmp_path: Path) -> None:
    """When version is None, _build_base_attrs omits langfuse.version."""
    session = TraceSessionState()
    store = _make_store(tmp_path)
    hook = BaseTraceHook(session=session, store=store)
    ctx = _make_ctx()
    attrs = hook._build_base_attrs(ctx, "chat")
    assert GenAiAttr.LANGFUSE_VERSION not in attrs


def test_build_base_attrs_omits_tags_when_empty(tmp_path: Path) -> None:
    """When tags is empty, _build_base_attrs omits langfuse.trace.tags."""
    session = TraceSessionState()
    store = _make_store(tmp_path)
    hook = BaseTraceHook(session=session, store=store)
    ctx = _make_ctx()
    attrs = hook._build_base_attrs(ctx, "chat")
    assert GenAiAttr.LANGFUSE_TRACE_TAGS not in attrs


# -- _save_span metric accumulation (A1) --------------------------------------


async def _save_chat_span(
    hook: BaseTraceHook,
    ctx: AgentContext,
    *,
    trace_id: str,
    root_span_id: str,
) -> None:
    await hook._save_span(
        trace_id=trace_id,
        span_id="chat-1",
        parent_span_id=root_span_id,
        name=SpanName.CHAT.value,
        kind=SpanKind.CLIENT.value,
        start_time=1.0,
        end_time=2.5,
        attributes={
            GenAiAttr.USAGE_INPUT_TOKENS.value: 30,
            GenAiAttr.USAGE_OUTPUT_TOKENS.value: 12,
            GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS.value: 10,
        },
        status=SpanStatus(),
        ctx=ctx,
    )


async def test_save_span_accumulates_under_turn_root(tmp_path: Path) -> None:
    """_save_span folds the span into the counters keyed by the turn's root."""
    session = TraceSessionState()
    hook = BaseTraceHook(session=session, store=_make_store(tmp_path))
    ctx = _make_ctx(trace_id="trace-acc", root_span_id="root-acc")

    await _save_chat_span(hook, ctx, trace_id="trace-acc", root_span_id="root-acc")

    metrics = session.read_metrics("trace-acc", "root-acc")
    assert metrics.total_input_tokens == 30
    assert metrics.total_output_tokens == 12
    assert metrics.llm_call_count == 1
    assert metrics == compute_metrics([]).model_copy(
        update={
            "total_input_tokens": 30,
            "total_output_tokens": 12,
            "llm_call_count": 1,
            "cache_hit_rate": 10 / 40,
            "response_token_ratio": 12 / 42,
            "api_latency_avg_s": 1.5,
        }
    )


async def test_save_span_without_store_accumulates_nothing() -> None:
    """Off-mode (store=None): _save_span returns before accumulating (A1)."""
    session = TraceSessionState()
    hook = BaseTraceHook(session=session, store=None)
    ctx = _make_ctx(trace_id="trace-off", root_span_id="root-off")

    await _save_chat_span(hook, ctx, trace_id="trace-off", root_span_id="root-off")

    assert "trace-off" not in session._metric_counters
    assert session.read_metrics("trace-off", "root-off") == compute_metrics([])


async def test_save_span_without_registered_root_accumulates_nothing(
    tmp_path: Path,
) -> None:
    """A span saved before any root is registered (ROOT_SPAN_ID unset in the
    turn state) accumulates nowhere — no finally_graph could read it."""
    session = TraceSessionState()
    hook = BaseTraceHook(session=session, store=_make_store(tmp_path))
    ctx = _make_ctx(trace_id="trace-noroot")

    await _save_chat_span(hook, ctx, trace_id="trace-noroot", root_span_id="root-x")

    assert "trace-noroot" not in session._metric_counters
