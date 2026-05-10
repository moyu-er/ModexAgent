"""framework.runtime — typed runtime state governance.

Replaces scattered ``ctx.metadata`` / ``ctx.extensions`` state with scoped,
persistable turn snapshots and typed operation records.
"""

from __future__ import annotations

from .codec import (
    RuntimeStateCodec,
    RuntimeStateCodecConfig,
    RuntimeStateCodecError,
    RuntimeStateCodecRegistry,
    UnsupportedAgentKindError,
)
from .enums import (
    AgentKind,
    ApprovalDenyPolicy,
    ApprovalSubjectType,
    CancellationSource,
    ControlCommandKind,
    MessageDeltaSource,
    OperationKind,
    OperationStatus,
    SnapshotReason,
    StateScope,
    ToolBatchStatus,
    ToolCallStatus,
    TurnCustomKey,
    TurnPhase,
)
from .models import (
    ApprovalRequestState,
    ApprovalTransaction,
    CancellationState,
    ControlCommandState,
    ControlMutation,
    JsonValue,
    MessageDelta,
    OperationState,
    ResumePoint,
    RuntimeErrorState,
    StateQueryScope,
    ToolArguments,
    ToolBatchState,
    ToolCallState,
    TurnIdentity,
    TurnSnapshot,
    TurnStateBase,
    TurnSummary,
)
from .policy import SnapshotPolicy
from .services import AgentRuntime, AgentRuntimeServices, require_runtime_state
from .store import (
    ActiveTurnConflictError,
    InMemoryRuntimeCommandStore,
    InMemoryTurnStateStore,
    JsonFileRuntimeCommandStore,
    JsonFileTurnStateStore,
    NoOpRuntimeCommandStore,
    NoOpTurnStateStore,
    RuntimeCommandStore,
    TurnStateStore,
)

__all__ = [
    # Enums
    "AgentKind",
    "ApprovalDenyPolicy",
    "ApprovalSubjectType",
    "CancellationSource",
    "ControlCommandKind",
    "MessageDeltaSource",
    "OperationKind",
    "OperationStatus",
    "SnapshotReason",
    "StateScope",
    "ToolBatchStatus",
    "ToolCallStatus",
    "TurnCustomKey",
    "TurnPhase",
    # Models
    "ApprovalRequestState",
    "ApprovalTransaction",
    "CancellationState",
    "ControlCommandState",
    "ControlMutation",
    "JsonValue",
    "MessageDelta",
    "OperationState",
    "ResumePoint",
    "RuntimeErrorState",
    "StateQueryScope",
    "ToolArguments",
    "ToolBatchState",
    "ToolCallState",
    "TurnIdentity",
    "TurnSnapshot",
    "TurnStateBase",
    "TurnSummary",
    # Services
    "AgentRuntime",
    "AgentRuntimeServices",
    "require_runtime_state",
    # Policy
    "SnapshotPolicy",
    # Codec
    "RuntimeStateCodec",
    "RuntimeStateCodecConfig",
    "RuntimeStateCodecError",
    "RuntimeStateCodecRegistry",
    "UnsupportedAgentKindError",
    # Stores
    "ActiveTurnConflictError",
    "InMemoryRuntimeCommandStore",
    "InMemoryTurnStateStore",
    "JsonFileRuntimeCommandStore",
    "JsonFileTurnStateStore",
    "NoOpRuntimeCommandStore",
    "NoOpTurnStateStore",
    "RuntimeCommandStore",
    "TurnStateStore",
]
