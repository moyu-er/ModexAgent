from __future__ import annotations

from pathlib import Path

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.approval.constants import ApprovalDecision, ApprovalStatus, ApprovalTier
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import (
    AgentKind,
    ApprovalSubjectType,
    TurnCustomKey,
    TurnPhase,
)
from modex_agent.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    ToolArguments,
    TurnIdentity,
)
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.trace.approval_span_hook import ApprovalSpanHook
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import (
    GenAiAttr,
    LangfuseObservationLevel,
    LangfuseObservationType,
    SpanKind,
    SpanName,
)
from modex_agent.trace.session_state import TraceSessionState


def _make_context(*, with_trace: bool = True) -> AgentContext:
    session = SessionInfo(session_id="session.worker", agent_name="worker")
    identity = TurnIdentity(agent_id="worker", session=session, turn_id="turn-1")
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    if with_trace:
        state.custom[TurnCustomKey.TRACE_ID] = "trace-1"
        state.custom[TurnCustomKey.ROOT_SPAN_ID] = "root-1"
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=session,
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
        identity=identity,
    )


def _make_hook(
    tmp_path: Path,
) -> tuple[ApprovalSpanHook, TraceSessionState, OtelSpanTraceStore]:
    session = TraceSessionState()
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    return ApprovalSpanHook(session=session, store=store), session, store


def _make_transaction(*, denied: bool) -> ApprovalTransaction:
    request = ApprovalRequestState(
        request_id="req-1",
        approval_id="approval-1",
        tool_call_id="call-1",
        tool_name="write_file",
        arguments=ToolArguments(values={"path": "secret.txt"}),
        tier=ApprovalTier.DANGEROUS,
        iteration=0,
    )
    transaction = ApprovalTransaction(
        approval_id="approval-1",
        turn_id="turn-1",
        subject_type=ApprovalSubjectType.TOOL_CALL,
        subject_ids=["call-1"],
        requests=[request],
    )
    if denied:
        transaction.apply_decision(
            "call-1", ApprovalDecision.DENIED, reason="too risky"
        )
    else:
        transaction.apply_decision("call-1", ApprovalDecision.ALLOWED)
    return transaction


async def test_approval_span_emitted_with_decision(tmp_path: Path) -> None:
    hook, _, store = _make_hook(tmp_path)
    context = _make_context()

    await hook.after_approval(context, _make_transaction(denied=True))

    spans = await store.list_by_session("session.worker")
    assert len(spans) == 1
    span = spans[0]
    assert span.name == SpanName.HUMAN_REVIEW.value
    assert span.parent_span_id == "root-1"
    assert span.kind == SpanKind.INTERNAL.value
    assert (
        span.attributes[GenAiAttr.LANGFUSE_OBSERVATION_TYPE]
        == LangfuseObservationType.EVENT.value
    )
    assert span.attributes[GenAiAttr.APPROVAL_DECISION] == str(ApprovalStatus.DENIED)
    assert span.attributes[GenAiAttr.APPROVAL_DENY_REASON] == "too risky"
    assert span.attributes[GenAiAttr.APPROVAL_TOOL_NAME] == "write_file"
    assert span.attributes[GenAiAttr.APPROVAL_TOOL_CALL_ID] == "call-1"
    assert (
        span.attributes[GenAiAttr.LANGFUSE_OBSERVATION_LEVEL]
        == LangfuseObservationLevel.WARNING.value
    )


async def test_approval_span_emitted_when_approved(tmp_path: Path) -> None:
    hook, _, store = _make_hook(tmp_path)
    context = _make_context()

    await hook.after_approval(context, _make_transaction(denied=False))

    spans = await store.list_by_session("session.worker")
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes[GenAiAttr.APPROVAL_DECISION] == str(ApprovalStatus.APPROVED)
    assert GenAiAttr.APPROVAL_DENY_REASON not in span.attributes
    assert (
        span.attributes[GenAiAttr.LANGFUSE_OBSERVATION_LEVEL]
        == LangfuseObservationLevel.DEFAULT.value
    )


async def test_no_approval_span_when_not_triggered(tmp_path: Path) -> None:
    hook, _, store = _make_hook(tmp_path)
    context = _make_context(with_trace=False)

    await hook.after_approval(context, _make_transaction(denied=True))

    assert await store.list_by_session("session.worker") == []
