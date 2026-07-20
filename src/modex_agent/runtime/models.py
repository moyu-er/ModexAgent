"""Runtime state governance models — typed dataclasses and Pydantic BaseModels for turn state, operations, approval, and snapshots.

Per ADR-0033 D14: the 5 ReAct state types that participate in the approval
state machine (``ApprovalTransaction`` / ``ApprovalRequestState`` /
``ToolBatchState`` / ``ToolCallState`` / ``ToolArguments``) are migrated from
``@dataclass`` to Pydantic ``BaseModel`` to enable the universal channel
codec (``model_dump()`` / ``model_validate()``). Frozen vs mutable is decided
per type based on whether the approval state machine mutates the object at
runtime.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.approval.constants import ApprovalDecision, ApprovalStatus, ApprovalTier
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ToolResult

from .enums import (
    AgentKind,
    ApprovalSubjectType,
    CancellationSource,
    MessageDeltaSource,
    OperationKind,
    OperationStatus,
    SnapshotReason,
    ToolBatchStatus,
    ToolCallStatus,
    TurnPhase,
)

if TYPE_CHECKING:
    from modex_agent.core.message import ChatMessage
    from modex_agent.core.tool_manager import ToolResult


type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class ToolArguments(BaseModel):
    """Typed wrapper around tool arguments for approval, audit, and recovery.

    Per ADR-0033 D14: frozen Pydantic ``BaseModel`` — truly immutable leaf
    value-object (just a typed wrapper around tool call arguments, never
    mutated after construction). The frozen model config prevents field
    reassignment; the ``values`` mapping is treated as read-only by
    consumers.

    Migrated from ``@dataclass(frozen=True)`` to ``BaseModel(frozen=True)``
    so that ``model_dump()`` / ``model_validate()`` serve as the universal
    channel codec (ADR-0033 D14).
    """

    model_config = ConfigDict(frozen=True)

    values: Mapping[str, JsonValue]


@dataclass(frozen=True)
class TurnIdentity:
    """Stable identity for every turn."""

    agent_id: str
    session: SessionInfo
    turn_id: str


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
    """Typed per-turn state for hooks and interceptors.

    Keys must be ``TurnCustomKey`` enum values. Values must be JSON-serializable
    if turn snapshots are persisted. Do not store process-local services,
    provider instances, or unbounded data here.
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


class ApprovalRequestState(BaseModel):
    """Per-approval-request state (one per pending tool call).

    Per ADR-0033 D14: mutable ``BaseModel`` (NOT frozen) — kept mutable for
    consistency with ``ApprovalTransaction``, whose state machine may
    reference and update these records during the approval lifecycle.
    """

    request_id: str
    approval_id: str
    tool_call_id: str
    tool_name: str
    arguments: ToolArguments
    tier: ApprovalTier
    iteration: int
    created_at: float = Field(default_factory=time.time)


class ApprovalTransaction(BaseModel):
    """Approval state machine — tracks per-tool-call decisions for a batch.

    Per ADR-0033 D14: mutable ``BaseModel`` (NOT frozen). The approval state
    machine mutates ``decisions`` dict externally (``apply_decision`` updates
    ``approval.decisions[call_id]`` from ``PENDING`` to ``ALLOWED``/
    ``DENIED``; ``_normalize_batch_decisions`` may rewrite ``ALLOWED`` to
    ``PREEMPTED`` for atomicity per ADR-0011). Frozen would break the state
    machine.

    Migrated from ``@dataclass`` to mutable ``BaseModel`` so that
    ``model_dump()`` / ``model_validate()`` serve as the universal channel
    codec (ADR-0033 D14). Methods and properties are preserved verbatim —
    Pydantic v2 allows methods on models, and mutable models permit field
    reassignment (``validate_assignment`` defaults to ``False``).
    """

    approval_id: str
    turn_id: str
    subject_type: ApprovalSubjectType
    subject_ids: list[str]
    requests: list[ApprovalRequestState]
    decisions: dict[str, ApprovalDecision] = Field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    deny_reason: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def apply_decision(
        self, tool_call_id: str, decision: ApprovalDecision, *, reason: str | None = None
    ) -> None:
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
            tc_id in self.decisions and self.decisions[tc_id] != ApprovalDecision.PENDING
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


class ToolCallState(BaseModel):
    """Per-tool-call execution state — tracks approval decision and execution status.

    Per ADR-0033 D14: mutable ``BaseModel`` (NOT frozen). The ``decision``
    field transitions ``PENDING`` → ``ALLOWED``/``DENIED``/``PREEMPTED``;
    ``status`` transitions during execution; ``result`` is set after tool
    execution. ``arbitrary_types_allowed=True`` allows the ``result`` field
    to hold a ``ToolResult`` (a plain class, not a Pydantic model) without
    validation.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    call_id: str
    tool_name: str
    arguments: ToolArguments
    approval_id: str | None = None
    decision: ApprovalDecision | None = None
    result: ToolResult | None = None
    status: ToolCallStatus = ToolCallStatus.PENDING
    operation_id: str | None = None


class ToolBatchState(BaseModel):
    """Per-batch tool execution state — groups multiple ``ToolCallState`` records.

    Per ADR-0033 D14: mutable ``BaseModel`` (NOT frozen). The ``status``
    field transitions ``WAITING`` → ``COMPLETED``/``FAILED``/``CANCELLED``
    during execution; ``operation_id`` may be set after construction.
    """

    batch_id: str
    iteration: int
    calls: list[ToolCallState]
    approval_id: str | None = None
    status: ToolBatchStatus = ToolBatchStatus.CREATED
    operation_id: str | None = None


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
