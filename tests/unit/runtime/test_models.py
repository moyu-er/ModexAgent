"""Tests for runtime state models — enums, identities, turn state, operations."""
from __future__ import annotations

from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.runtime.enums import (
    AgentKind,
    MessageDeltaSource,
    OperationKind,
    OperationStatus,
    ToolBatchStatus,
    TurnPhase,
)
from modex_agent.runtime.models import (
    MessageDelta,
    OperationState,
    ToolArguments,
    TurnIdentity,
    TurnStateBase,
)


def test_turn_identity_is_explicit_and_stable() -> None:
    identity = TurnIdentity(
        agent_id="bot",
        session=SessionInfo.from_str("session-1"),
        turn_id="turn-1",
    )

    assert identity.agent_id == "bot"
    assert str(identity.session) == "session-1"
    assert identity.turn_id == "turn-1"


def test_turn_state_starts_without_full_session_history() -> None:
    identity = TurnIdentity(agent_id="bot", session=SessionInfo.from_str("s1"), turn_id="t1")
    state = TurnStateBase(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )

    assert state.message_delta == []
    assert state.operations == []
    assert state.cancellation is None


def test_message_delta_wraps_normalized_message_and_source() -> None:
    message = ChatMessage(role="assistant", content="hello")
    delta = MessageDelta(
        message=message,
        source=MessageDeltaSource.ASSISTANT,
        provider_payload={"tool_call_count": 0},
    )

    assert delta.message.content == "hello"
    assert delta.source is MessageDeltaSource.ASSISTANT
    assert delta.provider_payload == {"tool_call_count": 0}


def test_tool_arguments_are_a_typed_value_object() -> None:
    args = ToolArguments(values={"path": "notes.md", "limit": 3})

    assert args.values["path"] == "notes.md"
    assert args.values["limit"] == 3


def test_operation_index_tracks_lifecycle_without_payload_duplication() -> None:
    op = OperationState(
        operation_id="op-1",
        kind=OperationKind.TOOL_BATCH,
        status=OperationStatus.CREATED,
        subject_id="batch-1",
    )

    assert op.kind is OperationKind.TOOL_BATCH
    assert op.status is OperationStatus.CREATED
    assert op.subject_id == "batch-1"


def test_runtime_enums_use_typed_values() -> None:
    assert AgentKind.REACT.value == "react"
    assert TurnPhase.SUSPENDED.value == "suspended"
    assert ToolBatchStatus.SUSPENDED.value == "suspended"
