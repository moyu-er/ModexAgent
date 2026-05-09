"""ReAct typed turn state, snapshot policy, and runtime state codec.

Replaces the old ``TurnResumeState`` / ``TurnResumeStateStore`` /
``StateStoreTurnResumeStateStore`` with typed ``ReActTurnState`` and
agent-kind-aware ``ReActSnapshotPolicy`` + ``ReActRuntimeStateCodec``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import uuid4

from framework.core.types import LLMResponse
from framework.memory.core.message import ChatMessage
from framework.runtime.codec import RuntimeStateCodec, RuntimeStateCodecConfig
from framework.runtime.enums import (
    AgentKind,
    MessageDeltaSource,
    OperationKind,
    SnapshotReason,
    ToolBatchStatus,
    ToolCallStatus,
    TurnPhase,
)
from framework.runtime.models import (
    ApprovalTransaction,
    JsonValue,
    MessageDelta,
    ResumePoint,
    ToolBatchState,
    ToolCallState,
    TurnIdentity,
    TurnSnapshot,
    TurnStateBase,
)
from framework.runtime.policy import SnapshotPolicy

from .constants import ReActNode

# =========================================================================
# ReActTurnState
# =========================================================================


@dataclass
class ReActTurnState(TurnStateBase):
    """ReAct-specific turn state — extends the generic turn state base.

    Graph nodes read and write this object directly instead of reaching into
    ``ctx.metadata``.
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
            if b.status not in (ToolBatchStatus.COMPLETED, ToolBatchStatus.FAILED, ToolBatchStatus.CANCELLED):
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


def require_react_state(ctx: object) -> ReActTurnState:
    """Validate and return typed ReActTurnState from AgentContext.runtime.state."""
    from framework.core.agent import AgentContext
    if not isinstance(ctx, AgentContext):
        raise TypeError(f"require_react_state expects AgentContext, got {type(ctx).__name__}")
    runtime = ctx.runtime
    if runtime is None or not hasattr(runtime, "state"):
        raise TypeError("AgentContext.runtime is not an AgentRuntime")
    state = runtime.state
    if isinstance(state, ReActTurnState):
        return state
    raise TypeError(f"ReAct requires ReActTurnState, got {type(state).__name__}")


# =========================================================================
# ReActSnapshotPolicy
# =========================================================================


class ReActSnapshotPolicy(SnapshotPolicy):
    """ReAct-specific snapshot policy — captures current_node, iteration, tool_batches, approval."""

    def should_capture(self, state: TurnStateBase, reason: SnapshotReason) -> bool:
        return True

    def capture(self, state: TurnStateBase, reason: SnapshotReason) -> TurnSnapshot:
        if not isinstance(state, ReActTurnState):
            raise TypeError(f"ReActSnapshotPolicy requires ReActTurnState, got {type(state).__name__}")
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
            "current_node": state.current_node.value,
            "iteration": state.iteration,
            "tool_batches": [
                {
                    "batch_id": b.batch_id,
                    "iteration": b.iteration,
                    "status": b.status.value,
                    "approval_id": b.approval_id,
                    "operation_id": b.operation_id,
                    "calls": [
                        {
                            "call_id": tc.call_id,
                            "tool_name": tc.tool_name,
                            "arguments": dict(tc.arguments.values),
                            "approval_id": tc.approval_id,
                            "decision": tc.decision.value if tc.decision else None,
                            "status": tc.status.value,
                        }
                        for tc in b.calls
                    ],
                }
                for b in state.tool_batches
            ],
        }
        if state.approval is not None:
            payload["approval"] = self._serialize_approval(state.approval)
        return payload

    @staticmethod
    def _serialize_approval(tx: ApprovalTransaction) -> dict[str, JsonValue]:
        return {
            "approval_id": tx.approval_id,
            "turn_id": tx.turn_id,
            "subject_type": tx.subject_type.value,
            "subject_ids": list(tx.subject_ids),
            "requests": [
                {
                    "request_id": r.request_id,
                    "approval_id": r.approval_id,
                    "tool_call_id": r.tool_call_id,
                    "tool_name": r.tool_name,
                    "arguments": dict(r.arguments.values),
                    "tier": r.tier.value if hasattr(r.tier, "value") else str(r.tier),
                    "iteration": r.iteration,
                    "created_at": r.created_at,
                }
                for r in tx.requests
            ],
            "decisions": {k: v.value if hasattr(v, "value") else str(v) for k, v in tx.decisions.items()},
            "status": tx.status.value if hasattr(tx.status, "value") else str(tx.status),
            "deny_reason": tx.deny_reason,
        }


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
                "session_id": snapshot.identity.session_id,
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
            "message_delta": [
                self._encode_message_delta(md) for md in snapshot.message_delta
            ],
            "state_payload": dict(snapshot.state_payload),
        }

    def decode_turn(self, payload: Mapping[str, JsonValue]) -> TurnSnapshot:
        identity_data: Mapping[str, JsonValue] = payload["identity"]  # type: ignore[assignment]
        resume_data: Mapping[str, JsonValue] = payload["resume_point"]  # type: ignore[assignment]
        return TurnSnapshot(
            identity=TurnIdentity(
                agent_id=str(identity_data["agent_id"]),
                session_id=str(identity_data["session_id"]),
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
