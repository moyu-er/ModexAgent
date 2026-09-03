"""Pipeline runtime-state governance regressions."""

from __future__ import annotations

import pytest

from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.state import ReActSnapshotPolicy, ReActTurnState
from modex_agent.approval.constants import ApprovalTier
from modex_agent.approval.runtime import ApprovalRuntime
from modex_agent.core.emitter import AgentResult
from modex_agent.core.message import ToolCall
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.context import ContextState, InMemoryContextManager
from modex_agent.messaging.models import InputMessage
from modex_agent.runtime.enums import AgentKind, ApprovalSubjectType, SnapshotReason, TurnPhase
from modex_agent.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    ToolArguments,
    TurnIdentity,
)
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore
from modex_agent.tools.manager import InMemoryToolManager
from modex_graph.exceptions import GraphInterrupt
from tests.unit.pipeline._helpers import _make_react_pipeline


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
):
    return _make_react_pipeline(
        agent=agent or _Agent(),
        context_manager=context_manager or InMemoryContextManager(),
        tool_manager=InMemoryToolManager(),
        input_adapter=_InputAdapter(),
        output_adapter=_OutputAdapter(),
        sanitizer=None,
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
    identity = TurnIdentity(agent_id="agent", session=SessionInfo.from_str(session_id), turn_id=turn_id)
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

    agent_context, _ = pipeline._turn_runner._builder.build_runtime_and_context(  # type: ignore[union-attr]
        SessionInfo.from_str("s1"),
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

    input_msg = InputMessage(
        content="subagent update",
        session=SessionInfo.from_str("s1"),
        metadata={"source_agent": "subagent"},
    )
    await pipeline._turn_runner.process_locked(
        input_msg,
        "s1",
        session=input_msg.session,
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

    input_msg = InputMessage(content="not an approval command", session=SessionInfo.from_str("s1"))
    await pipeline._turn_runner.process_locked(
        input_msg,
        "s1",
        session=input_msg.session,
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

    input_msg = InputMessage(content="/approve", session=SessionInfo.from_str("s1"))
    await pipeline._turn_runner.process_locked(
        input_msg,
        "s1",
        session=input_msg.session,
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
    input_msg1 = InputMessage(content="/approve", session=SessionInfo.from_str("s1"))
    await pipeline._turn_runner.process_locked(
        input_msg1,
        "s1",
        session=input_msg1.session,
    )
    assert await turn_store.load_turn(first_snapshot.identity) is None

    await turn_store.save_turn(second_snapshot)
    input_msg2 = InputMessage(content="/approve", session=SessionInfo.from_str("s1"))
    await pipeline._turn_runner.process_locked(
        input_msg2,
        "s1",
        session=input_msg2.session,
    )

    assert await turn_store.load_turn(first_snapshot.identity) is None
    assert await turn_store.load_turn(second_snapshot.identity) is None
