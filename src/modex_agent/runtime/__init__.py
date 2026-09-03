"""framework.runtime — typed runtime state governance.

Replaces scattered ``ctx.metadata`` / ``ctx.extensions`` state with scoped,
persistable turn snapshots and typed operation records.
"""

from __future__ import annotations

from .constants import EXECUTOR_PROCESS_ID_KEY
from .context import (
    InMemoryRuntimeContext,
    RuntimeContext,
    RuntimeContextManager,
    ToolCallRecord,
)
from .enums import AgentKind, TurnCustomKey, TurnPhase
from .policy import SnapshotPolicy
from .process_identity import ProcessIdentity
from .process_registry import ProcessRegistry, SingletonProcessRegistry
from .services import AgentRuntime, AgentRuntimeServices, require_runtime_state

__all__ = [
    "AgentKind",
    "AgentRuntime",
    "AgentRuntimeServices",
    "EXECUTOR_PROCESS_ID_KEY",
    "InMemoryRuntimeContext",
    "ProcessIdentity",
    "ProcessRegistry",
    "RuntimeContext",
    "RuntimeContextManager",
    "SingletonProcessRegistry",
    "SnapshotPolicy",
    "ToolCallRecord",
    "TurnCustomKey",
    "TurnPhase",
    "require_runtime_state",
]
