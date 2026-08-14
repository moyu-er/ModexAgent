"""Round-trip parity + full-cycle tests for ReActTurnState checkpoint migration.

Per ADR-0033 Stage 2 (ticket 03): verifies that the NEW ``state.checkpoint()``
/ ``state.from_checkpoint()`` path (via Pydantic ``model_dump`` / ``model_validate``)
preserves the same state data as the OLD hand-written ``_build_payload`` /
``state_from_snapshot`` path.

The OLD logic is captured here as baseline fixtures (the production code deletes
it). Both round-trips must preserve the same resume-critical fields:
``current_node``, ``iteration``, ``tool_batches`` (with nested
``ToolCallState`` decisions), ``approval`` (with ``ApprovalTransaction``),
and ``turn_uuid`` (from ``custom``).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from enum import StrEnum

from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.state import (
    ReActRuntimeStateCodec,
    ReActSnapshotPolicy,
    ReActTurnState,
)
from modex_agent.approval.constants import ApprovalDecision, ApprovalStatus, ApprovalTier
from modex_agent.core.session_id import SessionInfo
from modex_agent.runtime.codec import RuntimeStateCodecConfig
from modex_agent.runtime.enums import (
    AgentKind,
    ApprovalSubjectType,
    SnapshotReason,
    ToolBatchStatus,
    ToolCallStatus,
    TurnCustomKey,
    TurnPhase,
)
from modex_agent.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    JsonValue,
    OperationKind,
    OperationState,
    OperationStatus,
    ToolArguments,
    ToolBatchState,
    ToolCallState,
    TurnIdentity,
    TurnSnapshot,
)

# ---------------------------------------------------------------------------
# OLD payload key enums + _build_payload — captured pre-migration baseline
# ---------------------------------------------------------------------------


class _OldReActSnapshotPayloadKey(StrEnum):
    CURRENT_NODE = "current_node"
    ITERATION = "iteration"
    TOOL_BATCHES = "tool_batches"
    APPROVAL = "approval"
    TURN_UUID = TurnCustomKey.TURN_UUID.value


class _OldToolBatchSnapshotKey(StrEnum):
    BATCH_ID = "batch_id"
    ITERATION = "iteration"
    STATUS = "status"
    APPROVAL_ID = "approval_id"
    OPERATION_ID = "operation_id"
    CALLS = "calls"


class _OldToolCallSnapshotKey(StrEnum):
    CALL_ID = "call_id"
    TOOL_NAME = "tool_name"
    ARGUMENTS = "arguments"
    APPROVAL_ID = "approval_id"
    DECISION = "decision"
    STATUS = "status"


class _OldApprovalSnapshotKey(StrEnum):
    APPROVAL_ID = "approval_id"
    TURN_ID = "turn_id"
    SUBJECT_TYPE = "subject_type"
    SUBJECT_IDS = "subject_ids"
    REQUESTS = "requests"
    DECISIONS = "decisions"
    STATUS = "status"
    DENY_REASON = "deny_reason"


class _OldApprovalRequestSnapshotKey(StrEnum):
    REQUEST_ID = "request_id"
    APPROVAL_ID = "approval_id"
    TOOL_CALL_ID = "tool_call_id"
    TOOL_NAME = "tool_name"
    ARGUMENTS = "arguments"
    TIER = "tier"
    ITERATION = "iteration"
    CREATED_AT = "created_at"


def _old_serialize_approval(tx: ApprovalTransaction) -> dict[str, JsonValue]:
    """OLD approval serialization — captured from pre-migration state.py."""
    return {
        _OldApprovalSnapshotKey.APPROVAL_ID.value: tx.approval_id,
        _OldApprovalSnapshotKey.TURN_ID.value: tx.turn_id,
        _OldApprovalSnapshotKey.SUBJECT_TYPE.value: tx.subject_type.value,
        _OldApprovalSnapshotKey.SUBJECT_IDS.value: list(tx.subject_ids),
        _OldApprovalSnapshotKey.REQUESTS.value: [
            {
                _OldApprovalRequestSnapshotKey.REQUEST_ID.value: r.request_id,
                _OldApprovalRequestSnapshotKey.APPROVAL_ID.value: r.approval_id,
                _OldApprovalRequestSnapshotKey.TOOL_CALL_ID.value: r.tool_call_id,
                _OldApprovalRequestSnapshotKey.TOOL_NAME.value: r.tool_name,
                _OldApprovalRequestSnapshotKey.ARGUMENTS.value: dict(r.arguments.values),
                _OldApprovalRequestSnapshotKey.TIER.value: r.tier.value
                if hasattr(r.tier, "value")
                else str(r.tier),
                _OldApprovalRequestSnapshotKey.ITERATION.value: r.iteration,
                _OldApprovalRequestSnapshotKey.CREATED_AT.value: r.created_at,
            }
            for r in tx.requests
        ],
        _OldApprovalSnapshotKey.DECISIONS.value: {
            k: v.value if hasattr(v, "value") else str(v) for k, v in tx.decisions.items()
        },
        _OldApprovalSnapshotKey.STATUS.value: tx.status.value
        if hasattr(tx.status, "value")
        else str(tx.status),
        _OldApprovalSnapshotKey.DENY_REASON.value: tx.deny_reason,
    }


def _old_build_payload(state: ReActTurnState) -> dict[str, JsonValue]:
    """OLD _build_payload — captured from pre-migration ReActSnapshotPolicy."""
    payload: dict[str, JsonValue] = {
        _OldReActSnapshotPayloadKey.CURRENT_NODE.value: state.current_node.value,
        _OldReActSnapshotPayloadKey.ITERATION.value: state.iteration,
        _OldReActSnapshotPayloadKey.TOOL_BATCHES.value: [
            {
                _OldToolBatchSnapshotKey.BATCH_ID.value: b.batch_id,
                _OldToolBatchSnapshotKey.ITERATION.value: b.iteration,
                _OldToolBatchSnapshotKey.STATUS.value: b.status.value,
                _OldToolBatchSnapshotKey.APPROVAL_ID.value: b.approval_id,
                _OldToolBatchSnapshotKey.OPERATION_ID.value: b.operation_id,
                _OldToolBatchSnapshotKey.CALLS.value: [
                    {
                        _OldToolCallSnapshotKey.CALL_ID.value: tc.call_id,
                        _OldToolCallSnapshotKey.TOOL_NAME.value: tc.tool_name,
                        _OldToolCallSnapshotKey.ARGUMENTS.value: dict(tc.arguments.values),
                        _OldToolCallSnapshotKey.APPROVAL_ID.value: tc.approval_id,
                        _OldToolCallSnapshotKey.DECISION.value: tc.decision.value
                        if tc.decision
                        else None,
                        _OldToolCallSnapshotKey.STATUS.value: tc.status.value,
                    }
                    for tc in b.calls
                ],
            }
            for b in state.tool_batches
        ],
    }
    turn_uuid = state.custom.get(TurnCustomKey.TURN_UUID)
    if turn_uuid is not None:
        payload[_OldReActSnapshotPayloadKey.TURN_UUID.value] = turn_uuid
    if state.approval is not None:
        payload[_OldReActSnapshotPayloadKey.APPROVAL.value] = _old_serialize_approval(
            state.approval
        )
    return payload


def _old_approval_from_snapshot(
    snapshot: TurnSnapshot,
) -> ApprovalTransaction | None:
    """OLD approval deserialization — captured from pre-migration state.py."""
    approval_data = snapshot.state_payload.get(
        _OldReActSnapshotPayloadKey.APPROVAL.value
    )
    if not isinstance(approval_data, Mapping):
        return None

    requests: list[ApprovalRequestState] = []
    raw_requests = approval_data.get(_OldApprovalSnapshotKey.REQUESTS.value, [])
    if isinstance(raw_requests, list):
        for request_data in raw_requests:
            if not isinstance(request_data, Mapping):
                continue
            requests.append(
                ApprovalRequestState(
                    request_id=str(request_data[_OldApprovalRequestSnapshotKey.REQUEST_ID.value]),
                    approval_id=str(request_data[_OldApprovalRequestSnapshotKey.APPROVAL_ID.value]),
                    tool_call_id=str(
                        request_data[_OldApprovalRequestSnapshotKey.TOOL_CALL_ID.value]
                    ),
                    tool_name=str(request_data[_OldApprovalRequestSnapshotKey.TOOL_NAME.value]),
                    arguments=ToolArguments(
                        values=dict(
                            request_data.get(
                                _OldApprovalRequestSnapshotKey.ARGUMENTS.value, {}
                            )
                            or {}
                        ),
                    ),
                    tier=ApprovalTier(
                        str(request_data[_OldApprovalRequestSnapshotKey.TIER.value])
                    ),
                    iteration=int(request_data[_OldApprovalRequestSnapshotKey.ITERATION.value]),
                    created_at=float(
                        request_data.get(
                            _OldApprovalRequestSnapshotKey.CREATED_AT.value,
                            snapshot.created_at,
                        )
                    ),
                )
            )

    raw_decisions = approval_data.get(_OldApprovalSnapshotKey.DECISIONS.value, {})
    decisions = {
        str(key): ApprovalDecision(str(value))
        for key, value in dict(
            raw_decisions if isinstance(raw_decisions, Mapping) else {}
        ).items()
    }
    return ApprovalTransaction(
        approval_id=str(approval_data[_OldApprovalSnapshotKey.APPROVAL_ID.value]),
        turn_id=str(approval_data[_OldApprovalSnapshotKey.TURN_ID.value]),
        subject_type=ApprovalSubjectType(
            str(approval_data[_OldApprovalSnapshotKey.SUBJECT_TYPE.value])
        ),
        subject_ids=[
            str(item)
            for item in approval_data.get(_OldApprovalSnapshotKey.SUBJECT_IDS.value, [])
        ],
        requests=requests,
        decisions=decisions,
        status=ApprovalStatus(
            str(approval_data[_OldApprovalSnapshotKey.STATUS.value])
        ),
        deny_reason=approval_data.get(_OldApprovalSnapshotKey.DENY_REASON.value),  # type: ignore[arg-type]
    )


def _old_state_from_snapshot(snapshot: TurnSnapshot) -> ReActTurnState:
    """OLD state_from_snapshot — captured from pre-migration ReActSnapshotPolicy."""
    payload = snapshot.state_payload
    state = ReActTurnState(
        identity=snapshot.identity,
        agent_kind=AgentKind.REACT,
        phase=snapshot.phase,
        current_node=ReActNode(str(payload[_OldReActSnapshotPayloadKey.CURRENT_NODE.value])),
        iteration=int(payload[_OldReActSnapshotPayloadKey.ITERATION.value]),
        message_delta=list(snapshot.message_delta),
        approval=_old_approval_from_snapshot(snapshot),
    )
    turn_uuid = payload.get(_OldReActSnapshotPayloadKey.TURN_UUID.value)
    if turn_uuid is not None:
        state.custom[TurnCustomKey.TURN_UUID] = str(turn_uuid)

    raw_batches = payload.get(_OldReActSnapshotPayloadKey.TOOL_BATCHES.value, [])
    if isinstance(raw_batches, list):
        for batch_data in raw_batches:
            if not isinstance(batch_data, Mapping):
                continue
            calls: list[ToolCallState] = []
            raw_calls = batch_data.get(_OldToolBatchSnapshotKey.CALLS.value, [])
            if isinstance(raw_calls, list):
                for call_data in raw_calls:
                    if not isinstance(call_data, Mapping):
                        continue
                    raw_decision = call_data.get(_OldToolCallSnapshotKey.DECISION.value)
                    calls.append(
                        ToolCallState(
                            call_id=str(call_data[_OldToolCallSnapshotKey.CALL_ID.value]),
                            tool_name=str(call_data[_OldToolCallSnapshotKey.TOOL_NAME.value]),
                            arguments=ToolArguments(
                                values=dict(
                                    call_data.get(
                                        _OldToolCallSnapshotKey.ARGUMENTS.value, {}
                                    )
                                    or {}
                                ),
                            ),
                            approval_id=call_data.get(_OldToolCallSnapshotKey.APPROVAL_ID.value),  # type: ignore[arg-type]
                            decision=(
                                ApprovalDecision(str(raw_decision))
                                if raw_decision is not None
                                else None
                            ),
                            status=ToolCallStatus(
                                str(call_data[_OldToolCallSnapshotKey.STATUS.value])
                            ),
                        )
                    )
            state.tool_batches.append(
                ToolBatchState(
                    batch_id=str(batch_data[_OldToolBatchSnapshotKey.BATCH_ID.value]),
                    iteration=int(batch_data[_OldToolBatchSnapshotKey.ITERATION.value]),
                    calls=calls,
                    approval_id=batch_data.get(_OldToolBatchSnapshotKey.APPROVAL_ID.value),  # type: ignore[arg-type]
                    status=ToolBatchStatus(
                        str(batch_data[_OldToolBatchSnapshotKey.STATUS.value])
                    ),
                    operation_id=batch_data.get(_OldToolBatchSnapshotKey.OPERATION_ID.value),  # type: ignore[arg-type]
                )
            )
            batch = state.tool_batches[-1]
            if batch.operation_id is not None:
                state.operations.append(
                    OperationState(
                        operation_id=batch.operation_id,
                        kind=OperationKind.TOOL_BATCH,
                        status=OperationStatus.WAITING,
                        subject_id=batch.batch_id,
                    )
                )
    return state


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_request(call_id: str = "call-1", tool_name: str = "read_file") -> ApprovalRequestState:
    return ApprovalRequestState(
        request_id=f"r-{call_id}",
        approval_id="ap-1",
        tool_call_id=call_id,
        tool_name=tool_name,
        arguments=ToolArguments(values={"path": "notes.md", "limit": 3}),
        tier=ApprovalTier.DANGEROUS,
        iteration=1,
    )


def _make_transaction() -> ApprovalTransaction:
    tx = ApprovalTransaction(
        approval_id="ap-1",
        turn_id="t1",
        subject_type=ApprovalSubjectType.TOOL_BATCH,
        subject_ids=["batch-1"],
        requests=[_make_request("call-1"), _make_request("call-2", "write_file")],
    )
    tx.apply_decision("call-1", ApprovalDecision.ALLOWED)
    tx.apply_decision("call-2", ApprovalDecision.DENIED, reason="not allowed")
    return tx


def _make_tool_call(call_id: str = "call-1") -> ToolCallState:
    call = ToolCallState(
        call_id=call_id,
        tool_name="read_file" if call_id == "call-1" else "write_file",
        arguments=ToolArguments(values={"path": "a.txt"}),
    )
    call.decision = (
        ApprovalDecision.ALLOWED if call_id == "call-1" else ApprovalDecision.DENIED
    )
    call.status = (
        ToolCallStatus.ALLOWED if call_id == "call-1" else ToolCallStatus.DENIED
    )
    return call


def _make_tool_batch() -> ToolBatchState:
    return ToolBatchState(
        batch_id="batch-1",
        iteration=1,
        calls=[_make_tool_call("call-1"), _make_tool_call("call-2")],
        approval_id="ap-1",
        status=ToolBatchStatus.SUSPENDED,
        operation_id="op-1",
    )


def _make_state() -> ReActTurnState:
    """Realistic ReActTurnState at approval suspend time."""
    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="bot",
            session=SessionInfo.from_str("s1"),
            turn_id="t1",
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.SUSPENDED,
        current_node=ReActNode.TOOL,
        iteration=2,
    )
    state.tool_batches.append(_make_tool_batch())
    state.approval = _make_transaction()
    state.custom[TurnCustomKey.TURN_UUID] = "uuid-1234"
    # Add an operation matching the batch
    state.operations.append(
        OperationState(
            operation_id="op-1",
            kind=OperationKind.TOOL_BATCH,
            status=OperationStatus.WAITING,
            subject_id="batch-1",
        )
    )
    return state


# ---------------------------------------------------------------------------
# Tests: checkpoint / from_checkpoint round-trip
# ---------------------------------------------------------------------------


class TestCheckpointRoundTrip:
    """state.checkpoint() / state.from_checkpoint() preserve all fields."""

    def test_checkpoint_returns_dict_str_jsonvalue(self) -> None:
        state = _make_state()
        payload = state.checkpoint()
        assert isinstance(payload, dict)
        # All declared fields appear in the checkpoint
        expected_keys = {
            "identity",
            "agent_kind",
            "phase",
            "created_at",
            "updated_at",
            "message_delta",
            "operations",
            "cancellation",
            "custom",
            "current_node",
            "iteration",
            "turn_attempt",
            "llm_response",
            "tool_batches",
            "approval",
            "result",
            "resume_target",
            "node_scratch",
        }
        assert set(payload.keys()) == expected_keys

    def test_round_trip_preserves_react_fields(self) -> None:
        state = _make_state()
        payload = state.checkpoint()
        restored = ReActTurnState.from_checkpoint(payload)

        assert restored.current_node is ReActNode.TOOL
        assert restored.iteration == 2
        assert restored.phase is TurnPhase.SUSPENDED
        assert restored.agent_kind is AgentKind.REACT

    def test_round_trip_preserves_tool_batches(self) -> None:
        state = _make_state()
        restored = ReActTurnState.from_checkpoint(state.checkpoint())

        assert len(restored.tool_batches) == 1
        batch = restored.tool_batches[0]
        assert batch.batch_id == "batch-1"
        assert batch.iteration == 1
        assert batch.status is ToolBatchStatus.SUSPENDED
        assert batch.approval_id == "ap-1"
        assert batch.operation_id == "op-1"
        assert len(batch.calls) == 2

        call1 = batch.calls[0]
        assert call1.call_id == "call-1"
        assert call1.tool_name == "read_file"
        assert call1.decision is ApprovalDecision.ALLOWED
        assert call1.status is ToolCallStatus.ALLOWED
        assert call1.arguments.values["path"] == "a.txt"

        call2 = batch.calls[1]
        assert call2.call_id == "call-2"
        assert call2.tool_name == "write_file"
        assert call2.decision is ApprovalDecision.DENIED
        assert call2.status is ToolCallStatus.DENIED

    def test_round_trip_with_executed_tool_result(self) -> None:
        """ToolCallState.result (ToolResult) must survive checkpoint round-trip.

        Regression: ADR-0034 D1 Stage 2 migrated ToolCallState to BaseModel,
        but ToolResult was a plain class with arbitrary_types_allowed=True.
        model_dump(mode="json") raised PydanticSerializationError on
        ToolResult. Fix: ToolResult migrated to BaseModel with content
        as the source of truth (list[ContentPart]).
        """
        from modex_agent.core.tool_manager import ToolResult

        state = _make_state()
        batch = state.tool_batches[0]
        batch.calls[0].result = ToolResult.from_text(
            "read_file", "file contents here", execution_time=0.05, call_id="call-1"
        )
        batch.calls[0].status = ToolCallStatus.COMPLETED

        restored = ReActTurnState.from_checkpoint(state.checkpoint())
        r_batch = restored.tool_batches[0]
        r_call = r_batch.calls[0]
        assert r_call.result is not None
        assert r_call.result.tool_name == "read_file"
        assert r_call.result.message_content() == "file contents here"
        assert r_call.result.execution_time == 0.05
        assert r_call.result.call_id == "call-1"
        assert r_call.result.success is True

    def test_round_trip_preserves_approval(self) -> None:
        state = _make_state()
        restored = ReActTurnState.from_checkpoint(state.checkpoint())

        assert restored.approval is not None
        tx = restored.approval
        assert tx.approval_id == "ap-1"
        assert tx.turn_id == "t1"
        assert tx.subject_type is ApprovalSubjectType.TOOL_BATCH
        assert tx.subject_ids == ["batch-1"]
        assert tx.status is ApprovalStatus.DENIED
        assert tx.deny_reason == "not allowed"
        assert len(tx.requests) == 2
        assert tx.requests[0].tool_call_id == "call-1"
        assert tx.requests[0].tier is ApprovalTier.DANGEROUS
        # call-1 was ALLOWED, then preempted when call-2 was DENIED (ADR-0011)
        assert tx.decisions["call-1"] is ApprovalDecision.PREEMPTED
        assert tx.decisions["call-2"] is ApprovalDecision.DENIED

    def test_round_trip_preserves_custom_turn_uuid(self) -> None:
        state = _make_state()
        restored = ReActTurnState.from_checkpoint(state.checkpoint())

        assert restored.custom.get(TurnCustomKey.TURN_UUID) == "uuid-1234"

    def test_round_trip_preserves_identity(self) -> None:
        state = _make_state()
        restored = ReActTurnState.from_checkpoint(state.checkpoint())

        assert restored.identity.agent_id == "bot"
        assert restored.identity.turn_id == "t1"
        assert str(restored.identity.session) == str(state.identity.session)

    def test_round_trip_preserves_operations(self) -> None:
        state = _make_state()
        restored = ReActTurnState.from_checkpoint(state.checkpoint())

        assert len(restored.operations) == 1
        op = restored.operations[0]
        assert op.operation_id == "op-1"
        assert op.kind is OperationKind.TOOL_BATCH
        assert op.subject_id == "batch-1"

    def test_round_trip_preserves_none_fields(self) -> None:
        state = _make_state()
        restored = ReActTurnState.from_checkpoint(state.checkpoint())

        assert restored.llm_response is None
        assert restored.result is None
        assert restored.cancellation is None

    def test_round_trip_preserves_pep604_union_with_value(self) -> None:
        """PEP 604 union fields (``T | None``) round-trip with real values, not just ``None``.

        Covers ``cancellation`` / ``llm_response`` / ``approval`` / ``result`` —
        the case that was silently broken before ticket 01 fixed the per-channel
        codec's PEP 604 handling.
        """
        from modex_agent.core.constants import StopReason
        from modex_agent.core.emitter import AgentResult
        from modex_agent.core.types import LLMResponse
        from modex_agent.runtime.enums import CancellationSource
        from modex_agent.runtime.models import CancellationState

        state = _make_state()
        state.cancellation = CancellationState(
            reason="user stopped",
            source=CancellationSource.USER_COMMAND,
            operation_id="op-1",
        )
        state.llm_response = LLMResponse(
            content="thinking...",
            finish_reason="stop",
        )
        state.result = AgentResult(
            content="done",
            stop_reason=StopReason.COMPLETED,
        )

        restored = ReActTurnState.from_checkpoint(state.checkpoint())

        assert restored.cancellation is not None
        assert restored.cancellation.reason == "user stopped"
        assert restored.cancellation.source is CancellationSource.USER_COMMAND
        assert restored.cancellation.operation_id == "op-1"

        assert restored.llm_response is not None
        assert restored.llm_response.content == "thinking..."
        assert restored.llm_response.finish_reason == "stop"

        assert restored.approval is not None
        assert restored.approval.approval_id == "ap-1"

        assert restored.result is not None
        assert restored.result.content == "done"
        assert restored.result.stop_reason == StopReason.COMPLETED

    def test_round_trip_preserves_nested_basemodel(self) -> None:
        """``TurnIdentity`` (frozen Pydantic ``BaseModel``) nesting ``SessionInfo``
        (also Pydantic ``BaseModel``) round-trips with all nested fields preserved.

        The per-channel codec must reconstruct ``SessionInfo`` as a ``SessionInfo``
        instance, not a plain dict.
        """
        state = ReActTurnState(
            identity=TurnIdentity(
                agent_id="bot",
                session=SessionInfo.from_str("ws1.agent1"),
                turn_id="t-xyz",
            ),
            agent_kind=AgentKind.REACT,
            phase=TurnPhase.CREATED,
        )

        restored = ReActTurnState.from_checkpoint(state.checkpoint())

        assert restored.identity.agent_id == "bot"
        assert restored.identity.turn_id == "t-xyz"
        # SessionInfo is a Pydantic BaseModel nested inside a stdlib dataclass;
        # the per-channel codec must reconstruct it as a SessionInfo, not a dict.
        assert isinstance(restored.identity.session, SessionInfo)
        assert restored.identity.session.session_id == "ws1.agent1"
        assert restored.identity.session.agent_name == "agent1"
        assert str(restored.identity.session) == "ws1.agent1"

    def test_round_trip_preserves_list_of_dataclass(self) -> None:
        """``message_delta`` / ``operations`` / ``tool_batches`` with multiple
        complex entries round-trip — lists of stdlib dataclasses (some nesting
        Pydantic ``BaseModel`` fields) survive the per-channel codec.
        """
        from modex_agent.core.message import ChatMessage
        from modex_agent.runtime.enums import MessageDeltaSource
        from modex_agent.runtime.models import MessageDelta

        state = _make_state()

        state.message_delta.append(
            MessageDelta(
                message=ChatMessage(role="user", content="hello"),
                source=MessageDeltaSource.USER,
            )
        )
        state.message_delta.append(
            MessageDelta(
                message=ChatMessage(role="assistant", content="hi there"),
                source=MessageDeltaSource.ASSISTANT,
                provider_payload={"model": "gpt-4"},
            )
        )

        state.operations.append(
            OperationState(
                operation_id="op-2",
                kind=OperationKind.LLM_CALL,
                status=OperationStatus.WAITING,
                subject_id="llm-1",
            )
        )

        state.tool_batches.append(
            ToolBatchState(
                batch_id="batch-2",
                iteration=2,
                calls=[_make_tool_call("call-3")],
                status=ToolBatchStatus.CREATED,
            )
        )

        restored = ReActTurnState.from_checkpoint(state.checkpoint())

        assert len(restored.message_delta) == 2
        assert restored.message_delta[0].message.role == "user"
        assert restored.message_delta[0].message.content == "hello"
        assert restored.message_delta[0].source is MessageDeltaSource.USER
        assert restored.message_delta[1].message.role == "assistant"
        assert restored.message_delta[1].message.content == "hi there"
        assert restored.message_delta[1].source is MessageDeltaSource.ASSISTANT
        assert dict(restored.message_delta[1].provider_payload or {}) == {"model": "gpt-4"}

        assert len(restored.operations) == 2
        assert restored.operations[0].operation_id == "op-1"
        assert restored.operations[0].kind is OperationKind.TOOL_BATCH
        assert restored.operations[1].operation_id == "op-2"
        assert restored.operations[1].kind is OperationKind.LLM_CALL
        assert restored.operations[1].subject_id == "llm-1"

        assert len(restored.tool_batches) == 2
        assert restored.tool_batches[0].batch_id == "batch-1"
        assert restored.tool_batches[1].batch_id == "batch-2"
        assert restored.tool_batches[1].iteration == 2
        assert restored.tool_batches[1].status is ToolBatchStatus.CREATED
        assert len(restored.tool_batches[1].calls) == 1
        assert restored.tool_batches[1].calls[0].call_id == "call-3"


# ---------------------------------------------------------------------------
# Tests: OLD vs NEW parity (semantic equivalence)
# ---------------------------------------------------------------------------


class TestSnapshotParity:
    """OLD _build_payload / state_from_snapshot and NEW checkpoint / from_checkpoint
    both preserve the same resume-critical state data.

    The NEW path uses the per-channel codec (``GraphState.checkpoint()`` /
    ``from_checkpoint()``) after ADR-0033 D14 removed the ``model_dump`` override.
    The parity assertion compares the OLD hand-written baseline against this
    per-channel path, proving equivalence to both the baseline and the
    ``model_dump`` override it replaces.
    """

    def test_old_round_trip_preserves_state(self) -> None:
        """OLD path: _build_payload → TurnSnapshot → state_from_snapshot."""
        state = _make_state()
        old_payload = _old_build_payload(state)
        from modex_agent.runtime.models import ResumePoint

        snapshot = TurnSnapshot(
            identity=state.identity,
            agent_kind=AgentKind.REACT,
            phase=state.phase,
            reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
            resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=state.phase),
            message_delta=list(state.message_delta),
            state_payload=old_payload,
            created_at=time.time(),
        )
        restored = _old_state_from_snapshot(snapshot)

        # Assert resume-critical fields are preserved
        assert restored.current_node is ReActNode.TOOL
        assert restored.iteration == 2
        assert len(restored.tool_batches) == 1
        assert restored.tool_batches[0].batch_id == "batch-1"
        assert len(restored.tool_batches[0].calls) == 2
        assert restored.tool_batches[0].calls[0].decision is ApprovalDecision.ALLOWED
        assert restored.tool_batches[0].calls[1].decision is ApprovalDecision.DENIED
        assert restored.approval is not None
        assert restored.approval.approval_id == "ap-1"
        assert restored.approval.status is ApprovalStatus.DENIED
        # call-1 was ALLOWED, then preempted when call-2 was DENIED (ADR-0011)
        assert restored.approval.decisions["call-1"] is ApprovalDecision.PREEMPTED
        assert restored.custom.get(TurnCustomKey.TURN_UUID) == "uuid-1234"

    def test_new_round_trip_preserves_state(self) -> None:
        """NEW path: checkpoint → from_checkpoint."""
        state = _make_state()
        restored = ReActTurnState.from_checkpoint(state.checkpoint())

        assert restored.current_node is ReActNode.TOOL
        assert restored.iteration == 2
        assert len(restored.tool_batches) == 1
        assert restored.tool_batches[0].batch_id == "batch-1"
        assert len(restored.tool_batches[0].calls) == 2
        assert restored.tool_batches[0].calls[0].decision is ApprovalDecision.ALLOWED
        assert restored.tool_batches[0].calls[1].decision is ApprovalDecision.DENIED
        assert restored.approval is not None
        assert restored.approval.approval_id == "ap-1"
        assert restored.approval.status is ApprovalStatus.DENIED
        assert restored.custom.get(TurnCustomKey.TURN_UUID) == "uuid-1234"

    def test_both_round_trips_produce_equivalent_state(self) -> None:
        """Both OLD and NEW round-trips preserve the same resume-critical data."""
        state = _make_state()

        # OLD round-trip
        old_payload = _old_build_payload(state)
        from modex_agent.runtime.models import ResumePoint

        old_snapshot = TurnSnapshot(
            identity=state.identity,
            agent_kind=AgentKind.REACT,
            phase=state.phase,
            reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
            resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=state.phase),
            message_delta=list(state.message_delta),
            state_payload=old_payload,
            created_at=time.time(),
        )
        old_restored = _old_state_from_snapshot(old_snapshot)

        # NEW round-trip
        new_restored = ReActTurnState.from_checkpoint(state.checkpoint())

        # Compare resume-critical fields
        assert new_restored.current_node == old_restored.current_node
        assert new_restored.iteration == old_restored.iteration
        assert len(new_restored.tool_batches) == len(old_restored.tool_batches)
        assert new_restored.tool_batches[0].batch_id == old_restored.tool_batches[0].batch_id
        assert (
            len(new_restored.tool_batches[0].calls)
            == len(old_restored.tool_batches[0].calls)
        )
        assert (
            new_restored.tool_batches[0].calls[0].call_id
            == old_restored.tool_batches[0].calls[0].call_id
        )
        assert (
            new_restored.tool_batches[0].calls[0].decision
            == old_restored.tool_batches[0].calls[0].decision
        )
        assert (
            new_restored.tool_batches[0].calls[1].decision
            == old_restored.tool_batches[0].calls[1].decision
        )
        if new_restored.approval and old_restored.approval:
            assert new_restored.approval.approval_id == old_restored.approval.approval_id
            assert new_restored.approval.status == old_restored.approval.status
            assert new_restored.approval.decisions == old_restored.approval.decisions
        assert (
            new_restored.custom.get(TurnCustomKey.TURN_UUID)
            == old_restored.custom.get(TurnCustomKey.TURN_UUID)
        )


# ---------------------------------------------------------------------------
# Tests: full snapshot cycle (capture → codec → JSON → codec → restore)
# ---------------------------------------------------------------------------


class TestFullSnapshotCycle:
    """End-to-end: capture → TurnSnapshot → encode_turn → JSON → decode_turn →
    state_from_snapshot → assert restored state equals original.
    """

    def test_full_cycle_preserves_all_fields(self) -> None:
        state = _make_state()
        policy = ReActSnapshotPolicy()
        snapshot = policy.capture(state, SnapshotReason.TOOL_APPROVAL_REQUIRED)

        codec = ReActRuntimeStateCodec(RuntimeStateCodecConfig())
        encoded = codec.encode_turn(snapshot)
        json_str = json.dumps(encoded)
        decoded = json.loads(json_str)
        restored_snapshot = codec.decode_turn(decoded)
        restored_state = policy.state_from_snapshot(restored_snapshot)

        assert restored_state.current_node is ReActNode.TOOL
        assert restored_state.iteration == 2
        assert restored_state.phase is TurnPhase.SUSPENDED
        assert restored_state.agent_kind is AgentKind.REACT
        assert len(restored_state.tool_batches) == 1
        batch = restored_state.tool_batches[0]
        assert batch.batch_id == "batch-1"
        assert batch.iteration == 1
        assert batch.status is ToolBatchStatus.SUSPENDED
        assert batch.approval_id == "ap-1"
        assert len(batch.calls) == 2
        assert batch.calls[0].call_id == "call-1"
        assert batch.calls[0].decision is ApprovalDecision.ALLOWED
        assert batch.calls[1].call_id == "call-2"
        assert batch.calls[1].decision is ApprovalDecision.DENIED
        assert restored_state.approval is not None
        assert restored_state.approval.approval_id == "ap-1"
        assert restored_state.approval.status is ApprovalStatus.DENIED
        # call-1 was ALLOWED, then preempted when call-2 was DENIED (ADR-0011)
        assert restored_state.approval.decisions["call-1"] is ApprovalDecision.PREEMPTED
        assert restored_state.approval.decisions["call-2"] is ApprovalDecision.DENIED
        assert restored_state.custom.get(TurnCustomKey.TURN_UUID) == "uuid-1234"

    def test_full_cycle_preserves_identity(self) -> None:
        state = _make_state()
        policy = ReActSnapshotPolicy()
        snapshot = policy.capture(state, SnapshotReason.TOOL_APPROVAL_REQUIRED)
        codec = ReActRuntimeStateCodec(RuntimeStateCodecConfig())
        encoded = codec.encode_turn(snapshot)
        json_str = json.dumps(encoded)
        decoded = json.loads(json_str)
        restored_snapshot = codec.decode_turn(decoded)
        restored_state = policy.state_from_snapshot(restored_snapshot)

        assert restored_state.identity.agent_id == "bot"
        assert restored_state.identity.turn_id == "t1"


# ---------------------------------------------------------------------------
# Tests: replace_approval
# ---------------------------------------------------------------------------


class TestReplaceApproval:
    """ReActSnapshotPolicy.replace_approval updates approval in snapshot."""

    def test_replace_approval_persists_new_transaction(self) -> None:
        state = _make_state()
        policy = ReActSnapshotPolicy()
        snapshot = policy.capture(state, SnapshotReason.TOOL_APPROVAL_REQUIRED)

        # Build a new approval with updated decisions
        original_tx = state.approval
        assert original_tx is not None
        new_tx = ApprovalTransaction(
            approval_id=original_tx.approval_id,
            turn_id=original_tx.turn_id,
            subject_type=original_tx.subject_type,
            subject_ids=list(original_tx.subject_ids),
            requests=list(original_tx.requests),
            decisions=dict(original_tx.decisions),
            status=ApprovalStatus.APPROVED,
            deny_reason=None,
        )

        updated = ReActSnapshotPolicy.replace_approval(snapshot, new_tx)
        restored_state = policy.state_from_snapshot(updated)

        assert restored_state.approval is not None
        assert restored_state.approval.status is ApprovalStatus.APPROVED
        # Decisions copied from original (call-1 was preempted per ADR-0011)
        assert restored_state.approval.decisions["call-1"] is ApprovalDecision.PREEMPTED
        assert restored_state.approval.decisions["call-2"] is ApprovalDecision.DENIED

    def test_replace_approval_preserves_other_fields(self) -> None:
        state = _make_state()
        policy = ReActSnapshotPolicy()
        snapshot = policy.capture(state, SnapshotReason.TOOL_APPROVAL_REQUIRED)

        new_tx = ApprovalTransaction(
            approval_id="ap-2",
            turn_id="t1",
            subject_type=ApprovalSubjectType.TOOL_BATCH,
            subject_ids=["batch-1"],
            requests=[],
            status=ApprovalStatus.APPROVED,
        )

        updated = ReActSnapshotPolicy.replace_approval(snapshot, new_tx)
        restored_state = policy.state_from_snapshot(updated)

        # Other fields unchanged
        assert restored_state.current_node is ReActNode.TOOL
        assert restored_state.iteration == 2
        assert len(restored_state.tool_batches) == 1
        # Approval replaced
        assert restored_state.approval is not None
        assert restored_state.approval.approval_id == "ap-2"


# ---------------------------------------------------------------------------
# Tests: result field
# ---------------------------------------------------------------------------


class TestResultField:
    """The new explicit result field replaces custom[GRAPH_RESULT]."""

    def test_result_field_exists_with_default_none(self) -> None:
        state = ReActTurnState(
            identity=TurnIdentity(
                agent_id="bot",
                session=SessionInfo.from_str("s1"),
                turn_id="t1",
            ),
            agent_kind=AgentKind.REACT,
            phase=TurnPhase.CREATED,
        )
        assert state.result is None

    def test_result_field_round_trips(self) -> None:
        from modex_agent.core.constants import StopReason
        from modex_agent.core.emitter import AgentResult

        state = _make_state()
        state.result = AgentResult(
            content="task done",
            stop_reason=StopReason.COMPLETED,
        )
        restored = ReActTurnState.from_checkpoint(state.checkpoint())
        assert restored.result is not None
        assert restored.result.content == "task done"
        assert restored.result.stop_reason == StopReason.COMPLETED

    def test_result_field_checkpoint_included(self) -> None:
        from modex_agent.core.constants import StopReason
        from modex_agent.core.emitter import AgentResult

        state = _make_state()
        state.result = AgentResult(
            content="done",
            stop_reason=StopReason.COMPLETED,
        )
        payload = state.checkpoint()
        assert "result" in payload
        assert payload["result"] is not None
        assert isinstance(payload["result"], dict)
