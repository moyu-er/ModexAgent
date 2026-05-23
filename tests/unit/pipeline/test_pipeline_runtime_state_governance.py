"""Pipeline runtime-state governance regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from framework.agents.react.approval import ApprovalRuntime
from framework.agents.react.constants import ReActNode
from framework.agents.react.state import ReActSnapshotPolicy, ReActTurnState
from framework.approval.constants import ApprovalTier
from framework.core.context import ContextState, InMemoryContextManager
from framework.core.emitter import AgentResult
from framework.core.graph.interrupt import GraphInterrupt
from framework.core.tool_manager import InMemoryToolManager
from framework.core.types import InputMessage, ToolCall
from framework.pipeline.pipeline import AgentPipeline
from framework.runtime.enums import AgentKind, ApprovalSubjectType, SnapshotReason, TurnPhase
from framework.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    ToolArguments,
    TurnIdentity,
)
from framework.runtime.services import AgentRuntimeServices
from framework.runtime.store import InMemoryTurnStateStore


class _InputAdapter:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def receive(self):
        if False:
            yield None


class _OutputAdapter:
    async def send(self, message, session_id) -> None: ...
    async def send_delta(self, delta: str, session_id: str) -> None: ...
    async def flush_deltas(self, session_id: str) -> None: ...

    @property
    def supports_streaming(self) -> bool:
        return False


class _Agent:
    name = "agent"

    async def run(self, context, emitter):
        return AgentResult(content="done")


class _SuspendingAgent:
    name = "agent"

    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot

    async def run(self, context, emitter):
        await context.runtime.turn_store.save_turn(self._snapshot)
        raise GraphInterrupt(value=["approval"])


class _DangerousClassifier:
    def classify(self, tool_call: ToolCall, ctx) -> str:
        return ApprovalTier.DANGEROUS


def _pipeline(
    *,
    turn_store: InMemoryTurnStateStore | None = None,
    runtime_services: AgentRuntimeServices | None = None,
    context_manager: InMemoryContextManager | None = None,
    agent: object | None = None,
) -> AgentPipeline:
    return AgentPipeline(
        agent=agent or _Agent(),
        context_manager=context_manager or InMemoryContextManager(),
        tool_manager=InMemoryToolManager(),
        input_adapter=_InputAdapter(),
        output_adapter=_OutputAdapter(),
        sanitizer=None,
        approval_workspace=str(Path("/tmp/approval")),
        turn_store=turn_store,
        runtime_services=runtime_services,
    )


def _pending_snapshot(
    session_id: str = "s1",
    *,
    turn_id: str = "t1",
    approval_id: str = "ap1",
    request_id: str = "r1",
    tool_call_id: str = "c1",
) -> tuple[TurnIdentity, object]:
    identity = TurnIdentity(agent_id="agent", session_id=session_id, turn_id=turn_id)
    request = ApprovalRequestState(
        request_id=request_id,
        approval_id=approval_id,
        tool_call_id=tool_call_id,
        tool_name="write_file",
        arguments=ToolArguments(values={"path": "danger.txt"}),
        tier=ApprovalTier.DANGEROUS,
        iteration=1,
    )
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.SUSPENDED,
        current_node=ReActNode.TOOL,
        approval=ApprovalTransaction(
            approval_id=approval_id,
            turn_id=identity.turn_id,
            subject_type=ApprovalSubjectType.TOOL_BATCH,
            subject_ids=["batch1"],
            requests=[request],
        ),
    )
    return identity, ReActSnapshotPolicy().capture(
        state,
        SnapshotReason.TOOL_APPROVAL_REQUIRED,
    )


def test_pipeline_copies_runtime_services_template_into_each_turn() -> None:
    turn_store = InMemoryTurnStateStore()
    approval = ApprovalRuntime(classifier=_DangerousClassifier())
    runtime_services = AgentRuntimeServices(
        approval=approval,
        turn_store=turn_store,
    )
    pipeline = _pipeline(
        turn_store=turn_store,
        runtime_services=runtime_services,
    )
    context_state = ContextState()

    agent_context, _ = pipeline._build_runtime_and_context(
        "s1",
        context_state,
        InMemoryContextManager(),
    )

    assert agent_context.runtime is not None
    assert agent_context.runtime.approval is approval
    assert agent_context.runtime.turn_store is turn_store
    assert agent_context.runtime.state is not runtime_services


@pytest.mark.asyncio
async def test_source_agent_message_during_pending_approval_is_buffered_not_written() -> None:
    context_manager = InMemoryContextManager()
    turn_store = InMemoryTurnStateStore()
    _identity, snapshot = _pending_snapshot()
    await turn_store.save_turn(snapshot)
    pipeline = _pipeline(turn_store=turn_store, context_manager=context_manager)

    await pipeline._process_message_locked(
        InputMessage(
            content="subagent update",
            session_id="s1",
            metadata={"source_agent": "subagent"},
        ),
        "s1",
    )

    history = await (await context_manager.load("s1")).history.to_list()
    assert history == []


@pytest.mark.asyncio
async def test_unrelated_input_during_pending_approval_is_not_written_as_user_turn() -> None:
    context_manager = InMemoryContextManager()
    turn_store = InMemoryTurnStateStore()
    _identity, snapshot = _pending_snapshot()
    await turn_store.save_turn(snapshot)
    pipeline = _pipeline(turn_store=turn_store, context_manager=context_manager)

    await pipeline._process_message_locked(
        InputMessage(content="not an approval command", session_id="s1"),
        "s1",
    )

    history = await (await context_manager.load("s1")).history.to_list()
    assert [message.role for message in history] != ["user"]


@pytest.mark.asyncio
async def test_resume_that_suspends_again_keeps_new_snapshot() -> None:
    context_manager = InMemoryContextManager()
    turn_store = InMemoryTurnStateStore()
    _identity, first_snapshot = _pending_snapshot()
    _identity, second_snapshot = _pending_snapshot(
        turn_id="t1",
        approval_id="ap2",
        request_id="r2",
        tool_call_id="c2",
    )
    await turn_store.save_turn(first_snapshot)
    pipeline = _pipeline(
        turn_store=turn_store,
        context_manager=context_manager,
        agent=_SuspendingAgent(second_snapshot),
    )

    await pipeline._process_message_locked(
        InputMessage(content="/approve", session_id="s1"),
        "s1",
    )

    stored = await turn_store.load_turn(second_snapshot.identity)
    assert stored is not None
    approval = ReActSnapshotPolicy.approval_from_snapshot(stored)
    assert approval is not None
    assert approval.approval_id == "ap2"


@pytest.mark.asyncio
async def test_sequential_approval_groups_in_same_session_do_not_interfere() -> None:
    context_manager = InMemoryContextManager()
    turn_store = InMemoryTurnStateStore()
    _identity, first_snapshot = _pending_snapshot(turn_id="t1", approval_id="ap1")
    _identity, second_snapshot = _pending_snapshot(
        turn_id="t2",
        approval_id="ap2",
        request_id="r2",
        tool_call_id="c2",
    )
    pipeline = _pipeline(turn_store=turn_store, context_manager=context_manager)

    await turn_store.save_turn(first_snapshot)
    await pipeline._process_message_locked(
        InputMessage(content="/approve", session_id="s1"),
        "s1",
    )
    assert await turn_store.load_turn(first_snapshot.identity) is None

    await turn_store.save_turn(second_snapshot)
    await pipeline._process_message_locked(
        InputMessage(content="/approve", session_id="s1"),
        "s1",
    )

    assert await turn_store.load_turn(first_snapshot.identity) is None
    assert await turn_store.load_turn(second_snapshot.identity) is None
