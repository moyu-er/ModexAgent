"""framework.runtime — typed runtime state governance.

Replaces scattered ``ctx.metadata`` / ``ctx.extensions`` state with scoped,
persistable turn snapshots and typed operation records.
"""

from __future__ import annotations

from .constants import EXECUTOR_PROCESS_ID_KEY
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
    "ProcessIdentity",
    "ProcessRegistry",
    "SingletonProcessRegistry",
    "SnapshotPolicy",
    "TurnCustomKey",
    "TurnPhase",
    "require_runtime_state",
]
