from __future__ import annotations

import pytest

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.agents.react.state import ReActSnapshotPolicy, ReActTurnState
from modex_agent.approval.constants import ApprovalDecision, ApprovalTier
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, Tool
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import (
    AgentKind,
    ApprovalDenyPolicy,
    SnapshotReason,
    ToolBatchStatus,
    TurnPhase,
)
from modex_agent.runtime.models import StateQueryScope, TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore
from modex_graph.exceptions import GraphInterrupt


class _AlwaysDangerousClassifier:
    def classify(self, tool_call, ctx):
        return ApprovalTier.DANGEROUS


class _RecordTool(Tool):
    def __init__(self, calls):
        super().__init__(
            name="record",
            description="record",
            parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
        )
        self._calls = calls

    async def execute(self, **kwargs):
        self._calls.append(kwargs["value"])
        return kwargs["value"]


class _Provider(CallbackStreamProvider):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def get_default_model(self) -> str:
        return "mock-model"

    async def chat_stream(self, messages, on_content_delta=None, on_reasoning_delta=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(tool_name="record", arguments={"value": "a"}, call_id="c1"),
                    ToolCall(tool_name="record", arguments={"value": "b"}, call_id="c2"),
                ],
            )
        return LLMResponse(content="done")


class _Emitter(ContentEmitter):
    event_enum = object

    async def emit(self, event, data=None):
        pass

    async def emit_delta(self, delta):
        pass

    async def emit_content(self, content):
        pass

    async def emit_stream_end(self, *, resuming=False):
        pass

    async def emit_complete(self, result):
        pass

    async def emit_error(self, error_msg):
        pass

    def wants_streaming(self):
        return False


def _make_ctx(store, executed, default_deny_policy=ApprovalDenyPolicy.TOOL_RESULT_ONLY):
    manager = InMemoryToolManager()
    manager.register(_RecordTool(executed))
    identity = TurnIdentity(agent_id="agent", session=SessionInfo.from_str("s1"), turn_id="t1")
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    from modex_agent.approval.runtime import ApprovalRuntime
    ctx = AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=manager,
        session=SessionInfo.from_str("test.agent"),
        max_iterations=5,
    )
    ctx.identity = identity
    ctx.runtime = AgentRuntime(
        services=AgentRuntimeServices(
            approval=ApprovalRuntime(classifier=_AlwaysDangerousClassifier(), default_deny_policy=default_deny_policy),
            turn_store=store,
        ),
        state=state,
    )
    return ctx


@pytest.mark.asyncio
async def test_resume_executes_allowed_tools():
    store = InMemoryTurnStateStore()
    executed = []
    agent = ReActAgent(_Provider())
    ctx = _make_ctx(store, executed)

    with pytest.raises(GraphInterrupt):
        await agent.run(ctx, _Emitter())

    snapshots = await store.list_active_turns(
        StateQueryScope(session_id="s1", phase=TurnPhase.SUSPENDED, reason=SnapshotReason.TOOL_APPROVAL_REQUIRED)
    )
    assert len(snapshots) == 1
    snapshot = snapshots[0]

    approval = ReActSnapshotPolicy.approval_from_snapshot(snapshot)
    assert approval is not None
    approval.apply_decision("c1", ApprovalDecision.ALLOWED)
    approval.apply_decision("c2", ApprovalDecision.ALLOWED)
    await store.save_turn(ReActSnapshotPolicy.replace_approval(snapshot, approval))

    resume_ctx = _make_ctx(store, executed)
    resume_ctx.identity = snapshot.identity
    resume_ctx.runtime.state = ReActSnapshotPolicy.state_from_snapshot(
        ReActSnapshotPolicy.replace_approval(snapshot, approval)
    )

    result = await agent.run(resume_ctx, _Emitter())

    assert result.content == "done"
    assert executed == ["a", "b"]


@pytest.mark.asyncio
async def test_resume_after_deny_returns_error_results():
    store = InMemoryTurnStateStore()
    executed = []
    agent = ReActAgent(_Provider())
    ctx = _make_ctx(store, executed, default_deny_policy=ApprovalDenyPolicy.CANCEL_TURN)

    with pytest.raises(GraphInterrupt):
        await agent.run(ctx, _Emitter())

    snapshots = await store.list_active_turns(
        StateQueryScope(session_id="s1", phase=TurnPhase.SUSPENDED, reason=SnapshotReason.TOOL_APPROVAL_REQUIRED)
    )
    snapshot = snapshots[0]

    approval = ReActSnapshotPolicy.approval_from_snapshot(snapshot)
    approval.apply_decision("c1", ApprovalDecision.DENIED)

    resume_ctx = _make_ctx(store, executed, default_deny_policy=ApprovalDenyPolicy.CANCEL_TURN)
    resume_ctx.identity = snapshot.identity
    resume_ctx.runtime.state = ReActSnapshotPolicy.state_from_snapshot(
        ReActSnapshotPolicy.replace_approval(snapshot, approval)
    )

    result = await agent.run(resume_ctx, _Emitter())

    assert result.stop_reason == "turn_cancelled"
    assert executed == []

    batches = resume_ctx.runtime.state.tool_batches
    assert len(batches) == 1
    batch = batches[0]
    assert batch.status == ToolBatchStatus.FAILED
    for call in batch.calls:
        assert call.decision in (ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED)


class _ProviderNoCallId(CallbackStreamProvider):
    """Provider whose tool calls carry NO call_id (some providers omit it)."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def get_default_model(self) -> str:
        return "mock-model"

    async def chat_stream(self, messages, on_content_delta=None, on_reasoning_delta=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(tool_name="record", arguments={"value": "a"}),
                    ToolCall(tool_name="record", arguments={"value": "b"}),
                ],
            )
        return LLMResponse(content="done")


class _RecordingEmitter(_Emitter):
    def __init__(self):
        self.events = []

    async def emit(self, event, data=None):
        self.events.append((event, data))


@pytest.mark.asyncio
async def test_call_id_stable_across_approval_suspend_and_resume():
    """The id streamed with TOOL_CALL_START before suspension must equal the
    id carried by TOOL_CALL_END after resume — and both must equal the id in
    the approval request. This is what lets the WebUI pair a resumed tool's
    result with the block rendered before the interrupt."""
    from modex_agent.agents.react.agent import ReActEvent

    store = InMemoryTurnStateStore()
    executed = []
    agent = ReActAgent(_ProviderNoCallId())
    ctx = _make_ctx(store, executed)

    start_emitter = _RecordingEmitter()
    with pytest.raises(GraphInterrupt):
        await agent.run(ctx, start_emitter)

    start_ids = [
        data.call_id
        for event, data in start_emitter.events
        if event == ReActEvent.TOOL_CALL_START
    ]
    assert len(start_ids) == 2
    assert all(start_ids)  # canonicalized by the tool node, never empty

    snapshots = await store.list_active_turns(
        StateQueryScope(session_id="s1", phase=TurnPhase.SUSPENDED, reason=SnapshotReason.TOOL_APPROVAL_REQUIRED)
    )
    assert len(snapshots) == 1
    snapshot = snapshots[0]

    approval = ReActSnapshotPolicy.approval_from_snapshot(snapshot)
    assert approval is not None
    request_ids = [req.tool_call_id for req in approval.requests]
    # The approval flow references the SAME canonical ids the stream used.
    assert sorted(request_ids) == sorted(start_ids)
    for req_id in request_ids:
        approval.apply_decision(req_id, ApprovalDecision.ALLOWED)
    await store.save_turn(ReActSnapshotPolicy.replace_approval(snapshot, approval))

    resume_ctx = _make_ctx(store, executed)
    resume_ctx.identity = snapshot.identity
    resume_ctx.runtime.state = ReActSnapshotPolicy.state_from_snapshot(
        ReActSnapshotPolicy.replace_approval(snapshot, approval)
    )

    end_emitter = _RecordingEmitter()
    result = await agent.run(resume_ctx, end_emitter)

    assert result.content == "done"
    end_ids = [
        data[0].call_id
        for event, data in end_emitter.events
        if event == ReActEvent.TOOL_CALL_END
    ]
    assert sorted(end_ids) == sorted(start_ids)
