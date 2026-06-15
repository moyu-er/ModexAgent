"""ReAct typed turn state, snapshot policy, and runtime state codec."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

from framework.approval.constants import ApprovalDecision, ApprovalStatus, ApprovalTier
from framework.core.agent import AgentContext
from framework.core.types import LLMResponse
from framework.memory.core.message import ChatMessage
from framework.runtime.codec import RuntimeStateCodec, RuntimeStateCodecConfig
from framework.runtime.enums import (
    AgentKind,
    ApprovalSubjectType,
    MessageDeltaSource,
    OperationKind,
    OperationStatus,
    SnapshotReason,
    ToolBatchStatus,
    ToolCallStatus,
    TurnPhase,
)
from framework.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    JsonValue,
    MessageDelta,
    OperationState,
    ResumePoint,
    ToolArguments,
    ToolBatchState,
    ToolCallState,
    TurnIdentity,
    TurnSnapshot,
    TurnStateBase,
)
from framework.runtime.policy import SnapshotPolicy
from framework.core.session_id import SessionInfo

from .constants import ReActNode

# =========================================================================
# ReActTurnState
# =========================================================================


class ReActSnapshotPayloadKey(StrEnum):
    CURRENT_NODE = "current_node"
    ITERATION = "iteration"
    TOOL_BATCHES = "tool_batches"
    APPROVAL = "approval"


class ToolBatchSnapshotKey(StrEnum):
    BATCH_ID = "batch_id"
    ITERATION = "iteration"
    STATUS = "status"
    APPROVAL_ID = "approval_id"
    OPERATION_ID = "operation_id"
    CALLS = "calls"


class ToolCallSnapshotKey(StrEnum):
    CALL_ID = "call_id"
    TOOL_NAME = "tool_name"
    ARGUMENTS = "arguments"
    APPROVAL_ID = "approval_id"
    DECISION = "decision"
    STATUS = "status"


class ApprovalSnapshotKey(StrEnum):
    APPROVAL_ID = "approval_id"
    TURN_ID = "turn_id"
    SUBJECT_TYPE = "subject_type"
    SUBJECT_IDS = "subject_ids"
    REQUESTS = "requests"
    DECISIONS = "decisions"
    STATUS = "status"
    DENY_REASON = "deny_reason"


class ApprovalRequestSnapshotKey(StrEnum):
    REQUEST_ID = "request_id"
    APPROVAL_ID = "approval_id"
    TOOL_CALL_ID = "tool_call_id"
    TOOL_NAME = "tool_name"
    ARGUMENTS = "arguments"
    TIER = "tier"
    ITERATION = "iteration"
    CREATED_AT = "created_at"


@dataclass
class ReActTurnState(TurnStateBase):
    """ReAct-specific turn state — extends the generic turn state base.

    Graph nodes read and write this object directly through
    ``AgentContext.runtime.state``.
    """

    current_node: ReActNode = ReActNode.START
    iteration: int = 0
    llm_response: LLMResponse | None = None
    tool_batches: list[ToolBatchState] = field(default_factory=list)
    approval: ApprovalTransaction | None = None

    # ---- tool batch helpers ----

    def create_tool_batch(
        self,
        iteration: int,
        calls: list[ToolCallState],
    ) -> ToolBatchState:
        batch_id = uuid4().hex
        op = self.add_operation(OperationKind.TOOL_BATCH, batch_id)
        batch = ToolBatchState(
            batch_id=batch_id,
            iteration=iteration,
            calls=calls,
            operation_id=op.operation_id,
        )
        self.tool_batches.append(batch)
        return batch

    def active_tool_batch(self) -> ToolBatchState | None:
        for b in reversed(self.tool_batches):
            if b.status not in (
                ToolBatchStatus.COMPLETED,
                ToolBatchStatus.FAILED,
                ToolBatchStatus.CANCELLED,
            ):
                return b
        return None

    def completed_tool_calls(self) -> list[ToolCallState]:
        return [
            tc
            for batch in self.tool_batches
            for tc in batch.calls
            if tc.status is ToolCallStatus.COMPLETED
        ]

    def mark_completed(self) -> None:
        self.phase = TurnPhase.COMPLETED
        self.updated_at = time.time()


def require_react_state(ctx: AgentContext) -> ReActTurnState:
    """Validate and return typed ReActTurnState from AgentContext.runtime.state."""
    runtime = ctx.runtime
    if runtime is None or not hasattr(runtime, "state"):
        raise TypeError("AgentContext.runtime is not an AgentRuntime")
    state = runtime.state
    if isinstance(state, ReActTurnState):
        return state
    raise TypeError(f"ReAct requires ReActTurnState, got {type(state).__name__}")


def get_react_state(ctx: AgentContext) -> ReActTurnState | None:
    """Safely extract ReActTurnState from AgentContext, returning None if unavailable."""
    if ctx.identity is None or ctx.runtime is None:
        return None
    state = ctx.runtime.state
    return state if isinstance(state, ReActTurnState) else None


# =========================================================================
# ReActSnapshotPolicy
# =========================================================================


class ReActSnapshotPolicy(SnapshotPolicy):
    """ReAct-specific snapshot policy — captures current_node, iteration, tool_batches, approval."""

    def should_capture(self, state: TurnStateBase, reason: SnapshotReason) -> bool:
        return True

    def capture(self, state: TurnStateBase, reason: SnapshotReason) -> TurnSnapshot:
        if not isinstance(state, ReActTurnState):
            raise TypeError(
                f"ReActSnapshotPolicy requires ReActTurnState, got {type(state).__name__}"
            )
        return TurnSnapshot(
            identity=state.identity,
            agent_kind=AgentKind.REACT,
            phase=state.phase,
            reason=reason,
            resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=state.phase),
            message_delta=list(state.message_delta),
            state_payload=self._build_payload(state),
        )

    def _build_payload(self, state: ReActTurnState) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            ReActSnapshotPayloadKey.CURRENT_NODE.value: state.current_node.value,
            ReActSnapshotPayloadKey.ITERATION.value: state.iteration,
            ReActSnapshotPayloadKey.TOOL_BATCHES.value: [
                {
                    ToolBatchSnapshotKey.BATCH_ID.value: b.batch_id,
                    ToolBatchSnapshotKey.ITERATION.value: b.iteration,
                    ToolBatchSnapshotKey.STATUS.value: b.status.value,
                    ToolBatchSnapshotKey.APPROVAL_ID.value: b.approval_id,
                    ToolBatchSnapshotKey.OPERATION_ID.value: b.operation_id,
                    ToolBatchSnapshotKey.CALLS.value: [
                        {
                            ToolCallSnapshotKey.CALL_ID.value: tc.call_id,
                            ToolCallSnapshotKey.TOOL_NAME.value: tc.tool_name,
                            ToolCallSnapshotKey.ARGUMENTS.value: dict(tc.arguments.values),
                            ToolCallSnapshotKey.APPROVAL_ID.value: tc.approval_id,
                            ToolCallSnapshotKey.DECISION.value: tc.decision.value
                            if tc.decision
                            else None,
                            ToolCallSnapshotKey.STATUS.value: tc.status.value,
                        }
                        for tc in b.calls
                    ],
                }
                for b in state.tool_batches
            ],
        }
        if state.approval is not None:
            payload[ReActSnapshotPayloadKey.APPROVAL.value] = self.serialize_approval(
                state.approval
            )
        return payload

    @staticmethod
    def serialize_approval(tx: ApprovalTransaction) -> dict[str, JsonValue]:
        return {
            ApprovalSnapshotKey.APPROVAL_ID.value: tx.approval_id,
            ApprovalSnapshotKey.TURN_ID.value: tx.turn_id,
            ApprovalSnapshotKey.SUBJECT_TYPE.value: tx.subject_type.value,
            ApprovalSnapshotKey.SUBJECT_IDS.value: list(tx.subject_ids),
            ApprovalSnapshotKey.REQUESTS.value: [
                {
                    ApprovalRequestSnapshotKey.REQUEST_ID.value: r.request_id,
                    ApprovalRequestSnapshotKey.APPROVAL_ID.value: r.approval_id,
                    ApprovalRequestSnapshotKey.TOOL_CALL_ID.value: r.tool_call_id,
                    ApprovalRequestSnapshotKey.TOOL_NAME.value: r.tool_name,
                    ApprovalRequestSnapshotKey.ARGUMENTS.value: dict(r.arguments.values),
                    ApprovalRequestSnapshotKey.TIER.value: r.tier.value
                    if hasattr(r.tier, "value")
                    else str(r.tier),
                    ApprovalRequestSnapshotKey.ITERATION.value: r.iteration,
                    ApprovalRequestSnapshotKey.CREATED_AT.value: r.created_at,
                }
                for r in tx.requests
            ],
            ApprovalSnapshotKey.DECISIONS.value: {
                k: v.value if hasattr(v, "value") else str(v) for k, v in tx.decisions.items()
            },
            ApprovalSnapshotKey.STATUS.value: tx.status.value
            if hasattr(tx.status, "value")
            else str(tx.status),
            ApprovalSnapshotKey.DENY_REASON.value: tx.deny_reason,
        }

    @staticmethod
    def approval_from_snapshot(snapshot: TurnSnapshot) -> ApprovalTransaction | None:
        approval_data = snapshot.state_payload.get(ReActSnapshotPayloadKey.APPROVAL.value)
        if not isinstance(approval_data, Mapping):
            return None

        requests: list[ApprovalRequestState] = []
        raw_requests = approval_data.get(ApprovalSnapshotKey.REQUESTS.value, [])
        if isinstance(raw_requests, list):
            for request_data in raw_requests:
                if not isinstance(request_data, Mapping):
                    continue
                requests.append(
                    ApprovalRequestState(
                        request_id=str(request_data[ApprovalRequestSnapshotKey.REQUEST_ID.value]),
                        approval_id=str(request_data[ApprovalRequestSnapshotKey.APPROVAL_ID.value]),
                        tool_call_id=str(
                            request_data[ApprovalRequestSnapshotKey.TOOL_CALL_ID.value]
                        ),
                        tool_name=str(request_data[ApprovalRequestSnapshotKey.TOOL_NAME.value]),
                        arguments=ToolArguments(
                            values=dict(
                                request_data.get(ApprovalRequestSnapshotKey.ARGUMENTS.value, {})
                                or {}
                            ),
                        ),
                        tier=ApprovalTier(str(request_data[ApprovalRequestSnapshotKey.TIER.value])),
                        iteration=int(request_data[ApprovalRequestSnapshotKey.ITERATION.value]),
                        created_at=float(
                            request_data.get(
                                ApprovalRequestSnapshotKey.CREATED_AT.value,
                                snapshot.created_at,
                            )
                        ),
                    )
                )

        raw_decisions = approval_data.get(ApprovalSnapshotKey.DECISIONS.value, {})
        decisions = {
            str(key): ApprovalDecision(str(value))
            for key, value in dict(
                raw_decisions if isinstance(raw_decisions, Mapping) else {}
            ).items()
        }
        return ApprovalTransaction(
            approval_id=str(approval_data[ApprovalSnapshotKey.APPROVAL_ID.value]),
            turn_id=str(approval_data[ApprovalSnapshotKey.TURN_ID.value]),
            subject_type=ApprovalSubjectType(
                str(approval_data[ApprovalSnapshotKey.SUBJECT_TYPE.value])
            ),
            subject_ids=[
                str(item) for item in approval_data.get(ApprovalSnapshotKey.SUBJECT_IDS.value, [])
            ],
            requests=requests,
            decisions=decisions,
            status=ApprovalStatus(str(approval_data[ApprovalSnapshotKey.STATUS.value])),
            deny_reason=approval_data.get(ApprovalSnapshotKey.DENY_REASON.value),  # type: ignore[arg-type]
        )

    @staticmethod
    def replace_approval(snapshot: TurnSnapshot, tx: ApprovalTransaction) -> TurnSnapshot:
        payload = dict(snapshot.state_payload)
        payload[ReActSnapshotPayloadKey.APPROVAL.value] = ReActSnapshotPolicy.serialize_approval(tx)
        snapshot.state_payload = payload
        return snapshot

    @staticmethod
    def state_from_snapshot(snapshot: TurnSnapshot) -> ReActTurnState:
        payload = snapshot.state_payload
        state = ReActTurnState(
            identity=snapshot.identity,
            agent_kind=AgentKind.REACT,
            phase=snapshot.phase,
            current_node=ReActNode(str(payload[ReActSnapshotPayloadKey.CURRENT_NODE.value])),
            iteration=int(payload[ReActSnapshotPayloadKey.ITERATION.value]),
            message_delta=list(snapshot.message_delta),
            approval=ReActSnapshotPolicy.approval_from_snapshot(snapshot),
        )

        raw_batches = payload.get(ReActSnapshotPayloadKey.TOOL_BATCHES.value, [])
        if isinstance(raw_batches, list):
            for batch_data in raw_batches:
                if not isinstance(batch_data, Mapping):
                    continue
                calls: list[ToolCallState] = []
                raw_calls = batch_data.get(ToolBatchSnapshotKey.CALLS.value, [])
                if isinstance(raw_calls, list):
                    for call_data in raw_calls:
                        if not isinstance(call_data, Mapping):
                            continue
                        raw_decision = call_data.get(ToolCallSnapshotKey.DECISION.value)
                        calls.append(
                            ToolCallState(
                                call_id=str(call_data[ToolCallSnapshotKey.CALL_ID.value]),
                                tool_name=str(call_data[ToolCallSnapshotKey.TOOL_NAME.value]),
                                arguments=ToolArguments(
                                    values=dict(
                                        call_data.get(ToolCallSnapshotKey.ARGUMENTS.value, {}) or {}
                                    ),
                                ),
                                approval_id=call_data.get(ToolCallSnapshotKey.APPROVAL_ID.value),  # type: ignore[arg-type]
                                decision=(
                                    ApprovalDecision(str(raw_decision))
                                    if raw_decision is not None
                                    else None
                                ),
                                status=ToolCallStatus(
                                    str(call_data[ToolCallSnapshotKey.STATUS.value])
                                ),
                            )
                        )
                state.tool_batches.append(
                    ToolBatchState(
                        batch_id=str(batch_data[ToolBatchSnapshotKey.BATCH_ID.value]),
                        iteration=int(batch_data[ToolBatchSnapshotKey.ITERATION.value]),
                        calls=calls,
                        approval_id=batch_data.get(ToolBatchSnapshotKey.APPROVAL_ID.value),  # type: ignore[arg-type]
                        status=ToolBatchStatus(str(batch_data[ToolBatchSnapshotKey.STATUS.value])),
                        operation_id=batch_data.get(ToolBatchSnapshotKey.OPERATION_ID.value),  # type: ignore[arg-type]
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


# =========================================================================
# ReActRuntimeStateCodec
# =========================================================================


class ReActRuntimeStateCodec(RuntimeStateCodec):
    """Codec that round-trips ReAct snapshot payloads."""

    agent_kind = AgentKind.REACT

    def __init__(self, config: RuntimeStateCodecConfig | None = None) -> None:
        super().__init__(config)

    def encode_turn(self, snapshot: TurnSnapshot) -> Mapping[str, JsonValue]:
        for md in snapshot.message_delta:
            self._validate_provider_payload(md.provider_payload)

        return {
            "schema_version": snapshot.schema_version,
            "identity": {
                "agent_id": snapshot.identity.agent_id,
                "session_id": str(snapshot.identity.session),
                "turn_id": snapshot.identity.turn_id,
                "conversation_id": snapshot.identity.conversation_id,
            },
            "agent_kind": snapshot.agent_kind.value,
            "phase": snapshot.phase.value,
            "reason": snapshot.reason.value,
            "created_at": snapshot.created_at,
            "resume_point": {
                "agent_kind": snapshot.resume_point.agent_kind.value,
                "phase": snapshot.resume_point.phase.value,
            },
            "message_delta": [self._encode_message_delta(md) for md in snapshot.message_delta],
            "state_payload": dict(snapshot.state_payload),
        }

    def decode_turn(self, payload: Mapping[str, JsonValue]) -> TurnSnapshot:
        identity_data: Mapping[str, JsonValue] = payload["identity"]  # type: ignore[assignment]
        resume_data: Mapping[str, JsonValue] = payload["resume_point"]  # type: ignore[assignment]
        return TurnSnapshot(
            identity=TurnIdentity(
                agent_id=str(identity_data["agent_id"]),
                session=SessionInfo.from_str(str(identity_data["session_id"])),
                turn_id=str(identity_data["turn_id"]),
                conversation_id=identity_data.get("conversation_id"),  # type: ignore[arg-type]
            ),
            agent_kind=AgentKind(str(payload["agent_kind"])),
            phase=TurnPhase(str(payload["phase"])),
            reason=SnapshotReason(str(payload["reason"])),
            resume_point=ResumePoint(
                agent_kind=AgentKind(str(resume_data["agent_kind"])),
                phase=TurnPhase(str(resume_data["phase"])),
            ),
            message_delta=[
                self._decode_message_delta(md) for md in payload.get("message_delta", [])
            ],
            state_payload=dict(payload.get("state_payload", {})),
            schema_version=int(payload.get("schema_version", 1)),
            created_at=float(payload.get("created_at", time.time())),
        )

    def _encode_message_delta(self, md: MessageDelta) -> dict[str, JsonValue]:
        msg = md.message
        return {
            "role": msg.role,
            "content": msg.content,
            "source": md.source.value,
            "provider_payload": dict(md.provider_payload) if md.provider_payload else None,
            "tool_calls": msg.tool_calls,
            "tool_call_id": msg.tool_call_id,
            "name": msg.name,
        }

    def _decode_message_delta(self, data: Mapping[str, JsonValue]) -> MessageDelta:
        return MessageDelta(
            message=ChatMessage(
                role=str(data.get("role", "")),
                content=data.get("content"),
                tool_calls=data.get("tool_calls"),
                tool_call_id=data.get("tool_call_id"),
                name=data.get("name"),
            ),
            source=MessageDeltaSource(str(data["source"])),
            provider_payload=data.get("provider_payload"),
        )
