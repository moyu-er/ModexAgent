"""End-to-end approval resume coverage using TurnStateStore."""

from __future__ import annotations

import pytest

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.agents.react.approval import ApprovalRuntime
from modex_agent.agents.react.state import ReActSnapshotPolicy, ReActTurnState
from modex_agent.approval.constants import ApprovalDecision, ApprovalTier
from modex_agent.approval.types import ApprovalAction
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.graph.interrupt import GraphInterrupt
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, Tool
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, ApprovalDenyPolicy, SnapshotReason, TurnPhase
from modex_agent.runtime.models import StateQueryScope, TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore


class _DangerousClassifier:
    def classify(self, tool_call: ToolCall, ctx: AgentContext) -> str:
        return ApprovalTier.DANGEROUS


class _Provider:
    def __init__(self) -> None:
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


class _RecordTool(Tool):
    def __init__(self, calls: list[str]) -> None:
        super().__init__(
            name="record",
            description="record value",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        )
        self._calls = calls

    async def execute(self, **kwargs):
        self._calls.append(kwargs["value"])
        return kwargs["value"]


class _Emitter(ContentEmitter):
    event_enum = object

    async def emit(self, event, data=None): ...
    async def emit_delta(self, delta: str): ...
    async def emit_content(self, content: str): ...
    async def emit_stream_end(self, *, resuming: bool = False): ...
    async def emit_complete(self, result: AgentResult): ...
    async def emit_error(self, error_msg: str): ...
    def wants_streaming(self) -> bool: return False


def _context(store: InMemoryTurnStateStore, tool_calls: list[str], default_deny_policy=ApprovalDenyPolicy.TOOL_RESULT_ONLY) -> AgentContext:
    manager = InMemoryToolManager()
    manager.register(_RecordTool(tool_calls))
    identity = TurnIdentity(
        agent_id="agent",
        session=SessionInfo.from_str("s1"),
        turn_id="t1",
    )
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
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
            approval=ApprovalRuntime(classifier=_DangerousClassifier(), default_deny_policy=default_deny_policy),
            turn_store=store,
        ),
        state=state,
    )
    return ctx


async def _load_snapshot(store: InMemoryTurnStateStore):
    snapshots = await store.list_active_turns(
        StateQueryScope(
            session_id="s1",
            phase=TurnPhase.SUSPENDED,
            reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
        )
    )
    assert len(snapshots) == 1
    return snapshots[0]


@pytest.mark.asyncio
async def test_multi_tool_approves_one_by_one_then_resumes_from_start() -> None:
    store = InMemoryTurnStateStore()
    executed: list[str] = []
    agent = ReActAgent(_Provider())
    ctx = _context(store, executed)

    with pytest.raises(GraphInterrupt):
        await agent.run(ctx, _Emitter())

    snapshot = await _load_snapshot(store)
    approval = ReActSnapshotPolicy.approval_from_snapshot(snapshot)
    assert approval is not None
    assert [req.tool_call_id for req in approval.requests] == ["c1", "c2"]
    assert executed == []

    approval.apply_decision("c1", ApprovalDecision.ALLOWED)
    await store.save_turn(ReActSnapshotPolicy.replace_approval(snapshot, approval))
    snapshot = await _load_snapshot(store)
    approval = ReActSnapshotPolicy.approval_from_snapshot(snapshot)
    assert approval is not None
    assert approval.every_tool_decided is False
    assert executed == []

    approval.apply_decision("c2", ApprovalDecision.ALLOWED)
    snapshot = ReActSnapshotPolicy.replace_approval(snapshot, approval)
    resume_ctx = _context(store, executed)
    resume_ctx.identity = snapshot.identity
    resume_ctx.runtime.state = ReActSnapshotPolicy.state_from_snapshot(snapshot)

    result = await agent.run(resume_ctx, _Emitter())

    assert result.content == "done"
    assert executed == ["a", "b"]


@pytest.mark.asyncio
async def test_partial_approval_then_deny_preempts_whole_batch_on_start_resume() -> None:
    store = InMemoryTurnStateStore()
    executed: list[str] = []
    agent = ReActAgent(_Provider())
    ctx = _context(store, executed, default_deny_policy=ApprovalDenyPolicy.CANCEL_TURN)

    with pytest.raises(GraphInterrupt):
        await agent.run(ctx, _Emitter())

    snapshot = await _load_snapshot(store)
    approval = ReActSnapshotPolicy.approval_from_snapshot(snapshot)
    assert approval is not None
    approval.apply_decision("c1", ApprovalDecision.ALLOWED)
    approval.apply_decision("c2", ApprovalDecision.DENIED)

    resume_ctx = _context(store, executed, default_deny_policy=ApprovalDenyPolicy.CANCEL_TURN)
    resume_ctx.identity = snapshot.identity
    resume_ctx.runtime.state = ReActSnapshotPolicy.state_from_snapshot(
        ReActSnapshotPolicy.replace_approval(snapshot, approval)
    )

    result = await agent.run(resume_ctx, _Emitter())

    assert result.stop_reason == "turn_cancelled"
    assert executed == []


def test_approval_action_enum_used_for_commands() -> None:
    assert ApprovalAction.ALLOW.value == "allow"
