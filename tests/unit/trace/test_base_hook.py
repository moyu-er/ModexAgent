"""Tests for BaseTraceHook shared infrastructure."""

from __future__ import annotations

from pathlib import Path

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.base_hook import BaseTraceHook
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import GenAiAttr
from modex_agent.trace.session_state import TraceSessionState


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
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
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
