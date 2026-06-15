"""Tests verifying ToolNode resume path sets PRE_APPROVED_TOOL_IDS."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from framework.agents.react.agent import ReActAgent
from framework.agents.react.constants import ReActNode, ReActReason
from framework.agents.react.nodes.tool import ToolNode
from framework.agents.react.state import ReActSnapshotPolicy, ReActTurnState
from framework.approval.constants import ApprovalDecision, ApprovalTier
from framework.core.agent import AgentContext
from framework.core.emitter import AgentResult, ContentEmitter
from framework.core.graph.interrupt import GraphInterrupt
from framework.core.session_id import SessionInfo
from framework.core.tool_manager import InMemoryToolManager, Tool
from framework.core.types import LLMResponse, ToolCall
from framework.memory.history import ListMessageHistory
from framework.runtime.enums import AgentKind, ApprovalDenyPolicy, MessageDeltaSource, SnapshotReason, ToolBatchStatus, ToolCallStatus, TurnCustomKey, TurnPhase
from framework.runtime.models import ApprovalRequestState, ApprovalTransaction, StateQueryScope, ToolArguments, ToolBatchState, ToolCallState, TurnIdentity
from framework.runtime.services import AgentRuntime, AgentRuntimeServices
from framework.runtime.store import InMemoryTurnStateStore


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


class _Provider:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, **kwargs):
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
    from framework.agents.react.approval import ApprovalRuntime
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
async def test_resume_sets_pre_approved_tool_ids_for_allowed_tools():
    """When resuming after approval, ToolNode must set PRE_APPROVED_TOOL_IDS
    so that downstream tool wrappers do not re-request approval."""

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

    pre_approved = resume_ctx.runtime.state.custom.get(TurnCustomKey.PRE_APPROVED_TOOL_IDS, set())
    assert "c1" in pre_approved
    assert "c2" in pre_approved


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
