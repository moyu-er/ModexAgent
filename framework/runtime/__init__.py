"""framework.runtime — typed runtime state governance.

Replaces scattered ``ctx.metadata`` / ``ctx.extensions`` state with scoped,
persistable turn snapshots and typed operation records.
"""

from __future__ import annotations

from .enums import AgentKind, TurnCustomKey, TurnPhase
from .policy import SnapshotPolicy
from .services import AgentRuntime, AgentRuntimeServices, require_runtime_state

__all__ = [
    "AgentKind",
    "TurnCustomKey",
    "TurnPhase",
    "SnapshotPolicy",
    "AgentRuntime",
    "AgentRuntimeServices",
    "require_runtime_state",
]
