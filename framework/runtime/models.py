"""Runtime state governance models — typed dataclasses for turn state, operations, approval, and snapshots."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeAlias
from uuid import uuid4

from framework.approval.constants import ApprovalDecision, ApprovalStatus, ApprovalTier

from .enums import (
    AgentKind,
    ApprovalSubjectType,
    CancellationSource,
    ControlCommandKind,
    MessageDeltaSource,
    OperationKind,
    OperationStatus,
    SnapshotReason,
    ToolBatchStatus,
    ToolCallStatus,
    TurnPhase,
)

if TYPE_CHECKING:
    from framework.core.tool_manager import ToolResult
    from framework.memory.core.message import ChatMessage


JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolArguments:
    """Typed wrapper around tool arguments for approval, audit, and recovery.

    ``values`` must be treated as read-only by consumers.
    """

    values: Mapping[str, JsonValue]


@dataclass(frozen=True)
class TurnIdentity:
    """Stable identity for every turn."""

    agent_id: str
    session_id: str
    turn_id: str
    conversation_id: str | None = None


# ---------------------------------------------------------------------------
# Error and cancellation
# ---------------------------------------------------------------------------


@dataclass
class RuntimeErrorState:
    error_type: str
    message: str
    retryable: bool


@dataclass
class CancellationState:
    reason: str
    source: CancellationSource
    requested_at: float = field(default_factory=time.time)
    operation_id: str | None = None


# ---------------------------------------------------------------------------
# Message delta (current turn only — NOT full session history)
# ---------------------------------------------------------------------------


@dataclass
class MessageDelta:
    message: ChatMessage
    source: MessageDeltaSource
    provider_payload: Mapping[str, JsonValue] | None = None


# ---------------------------------------------------------------------------
# Operation state (generic audit index)
# ---------------------------------------------------------------------------


@dataclass
class OperationState:
    operation_id: str
    kind: OperationKind
    status: OperationStatus
    subject_id: str | None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: RuntimeErrorState | None = None


# ---------------------------------------------------------------------------
# Turn state base (shared across agent modes)
# ---------------------------------------------------------------------------


@dataclass
class TurnStateBase:
    identity: TurnIdentity
    agent_kind: AgentKind
    phase: TurnPhase
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    message_delta: list[MessageDelta] = field(default_factory=list)
    operations: list[OperationState] = field(default_factory=list)
    cancellation: CancellationState | None = None
    custom: dict[str, Any] = field(default_factory=dict)
    """Typed migration target for hook/interceptor per-turn data.
    
    Hooks and interceptors may store lightweight per-turn state here
    instead of the old ``ctx.metadata`` dict. Values must be JSON-serializable
    if turn snapshots are persisted. Process-local services and provider
    instances must never be placed here.
    """

    def add_operation(
        self,
        kind: OperationKind,
        subject_id: str | None,
        status: OperationStatus = OperationStatus.CREATED,
    ) -> OperationState:
        op = OperationState(
            operation_id=uuid4().hex,
            kind=kind,
            status=status,
            subject_id=subject_id,
        )
        self.operations.append(op)
        self.updated_at = time.time()
        return op

    def update_operation(
        self,
        operation_id: str,
        status: OperationStatus,
        error: RuntimeErrorState | None = None,
    ) -> None:
        for op in self.operations:
            if op.operation_id == operation_id:
                op.status = status
                op.error = error
                op.updated_at = time.time()
                self.updated_at = op.updated_at
                return
        raise KeyError(f"operation not found: {operation_id}")


# ---------------------------------------------------------------------------
# Approval (transaction inside turn state)
# ---------------------------------------------------------------------------


@dataclass
class ApprovalRequestState:
    request_id: str
    approval_id: str
    tool_call_id: str
    tool_name: str
    arguments: ToolArguments
    tier: ApprovalTier
    iteration: int
    created_at: float = field(default_factory=time.time)


@dataclass
class ApprovalTransaction:
    approval_id: str
    turn_id: str
    subject_type: ApprovalSubjectType
    subject_ids: list[str]
    requests: list[ApprovalRequestState]
    decisions: dict[str, ApprovalDecision] = field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    deny_reason: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def apply_decision(self, tool_call_id: str, decision: ApprovalDecision, *, reason: str | None = None) -> None:
        """Apply a single decision. ``DENIED`` preempts all unresolved requests."""
        self.decisions[tool_call_id] = decision
        if reason:
            self.deny_reason = reason
        if decision == ApprovalDecision.DENIED:
            for req in self.requests:
                if req.tool_call_id not in self.decisions or self.decisions[req.tool_call_id] in (
                    ApprovalDecision.PENDING,
                    ApprovalDecision.ALLOWED,
                ):
                    self.decisions[req.tool_call_id] = ApprovalDecision.PREEMPTED
            self.status = ApprovalStatus.DENIED
        elif self._every_tool_decided():
            self.status = ApprovalStatus.APPROVED
        else:
            self.status = ApprovalStatus.PARTIAL
        self.updated_at = time.time()

    def _every_tool_decided(self) -> bool:
        return all(
            tc_id in self.decisions
            and self.decisions[tc_id] != ApprovalDecision.PENDING
            for tc_id in (r.tool_call_id for r in self.requests)
        )

    @property
    def every_tool_decided(self) -> bool:
        return self._every_tool_decided()


@dataclass(frozen=True)
class ApprovalDenialContext:
    """审批拒绝时的完整上下文，写入 checkpoint 供恢复分析。"""

    tool_name: str
    tool_call_id: str
    arguments: dict[str, object]
    tier: str
    denied_at: float
    reason: str
    session_id: str
    turn_id: str = ""
    iteration: int = 0


# ---------------------------------------------------------------------------
# Tool execution state (ReAct and future modes)
# ---------------------------------------------------------------------------


@dataclass
class ToolCallState:
    call_id: str
    tool_name: str
    arguments: ToolArguments
    approval_id: str | None = None
    decision: ApprovalDecision | None = None
    result: ToolResult | None = None
    status: ToolCallStatus = ToolCallStatus.PENDING
    operation_id: str | None = None


@dataclass
class ToolBatchState:
    batch_id: str
    iteration: int
    calls: list[ToolCallState]
    approval_id: str | None = None
    status: ToolBatchStatus = ToolBatchStatus.CREATED
    operation_id: str | None = None


# ---------------------------------------------------------------------------
# Control command state
# ---------------------------------------------------------------------------


@dataclass
class ControlCommandState:
    command_id: str
    kind: ControlCommandKind
    agent_id: str
    session_id: str | None
    payload: Mapping[str, JsonValue]
    status: OperationStatus = OperationStatus.CREATED
    created_at: float = field(default_factory=time.time)
    applied_at: float | None = None


@dataclass(frozen=True)
class ControlMutation:
    command_id: str
    operation_id: str
    target_phase: TurnPhase | None = None
    cancellation: CancellationState | None = None
    snapshot_reason: SnapshotReason | None = None


# ---------------------------------------------------------------------------
# Snapshot (persistence)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResumePoint:
    """Query metadata only — node / iteration decoded from state_payload."""

    agent_kind: AgentKind
    phase: TurnPhase


@dataclass
class TurnSnapshot:
    """Mutable in-progress turn snapshot. Identity via ``identity`` field."""

    identity: TurnIdentity
    agent_kind: AgentKind
    phase: TurnPhase
    reason: SnapshotReason
    resume_point: ResumePoint
    message_delta: list[MessageDelta]
    state_payload: Mapping[str, JsonValue]
    schema_version: int = 1
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class StateQueryScope:
    agent_id: str | None = None
    session_id: str | None = None
    agent_kind: AgentKind | None = None
    phase: TurnPhase | None = None
    reason: SnapshotReason | None = None
    created_before: float | None = None


@dataclass
class TurnSummary:
    """Optional audit output — not used for resume. Must not contain message history."""

    identity: TurnIdentity
    agent_kind: AgentKind
    final_phase: TurnPhase
    completed_at: float
    operation_count: int
    error: RuntimeErrorState | None = None
    cancellation: CancellationState | None = None
