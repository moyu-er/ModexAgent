"""Tests for ReActTurnState — typed turn state, tool batch helpers, snapshot policy."""
from __future__ import annotations

from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.state import ReActSnapshotPolicy, ReActTurnState
from modex_agent.runtime.enums import AgentKind, OperationKind, OperationStatus, SnapshotReason, ToolBatchStatus, TurnPhase
from modex_agent.runtime.models import ToolArguments, ToolCallState, TurnIdentity
from modex_agent.core.session_id import SessionInfo


def test_react_turn_state_creates_operation_for_tool_batch() -> None:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )
    batch = state.create_tool_batch(
        iteration=1,
        calls=[
            ToolCallState(
                call_id="call-1",
                tool_name="read_file",
                arguments=ToolArguments(values={"path": "README.md"}),
            )
        ],
    )

    assert batch.status is ToolBatchStatus.CREATED
    assert batch.operation_id is not None
    assert state.tool_batches == [batch]
    assert state.operations[0].kind is OperationKind.TOOL_BATCH
    assert state.operations[0].subject_id == batch.batch_id


def test_react_snapshot_policy_captures_minimal_resume_point() -> None:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.SUSPENDED,
        current_node=ReActNode.TOOL,
        iteration=2,
    )
    snapshot = ReActSnapshotPolicy().capture(
        state,
        SnapshotReason.TOOL_APPROVAL_REQUIRED,
    )

    assert snapshot.resume_point.agent_kind is AgentKind.REACT
    assert snapshot.resume_point.phase is TurnPhase.SUSPENDED
    assert snapshot.state_payload["current_node"] == ReActNode.TOOL.value
    assert snapshot.state_payload["iteration"] == 2


def test_react_operation_update_marks_batch_completed() -> None:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )
    batch = state.create_tool_batch(iteration=1, calls=[])
    state.update_operation(batch.operation_id, OperationStatus.COMPLETED)

    assert state.operations[0].status is OperationStatus.COMPLETED


def test_react_turn_state_initializes_default_react_fields() -> None:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )

    assert state.current_node is ReActNode.START
    assert state.iteration == 0
    assert state.llm_response is None
    assert state.tool_batches == []
    assert state.approval is None


def test_react_turn_state_extends_turn_state_base() -> None:
    from modex_agent.runtime.models import TurnStateBase

    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session=SessionInfo.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )

    assert isinstance(state, TurnStateBase)
