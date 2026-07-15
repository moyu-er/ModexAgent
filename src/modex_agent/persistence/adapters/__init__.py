"""SQLite-backed persistence adapters for runtime state and memory.

Adapters implement store ABCs, each owning one storage concern and sharing
one :class:`ConnectionManager`.
"""

from __future__ import annotations

from modex_agent.persistence.adapters.approval_audit_store import (
    ApprovalAuditStore,
    SqliteApprovalAuditStore,
)
from modex_agent.persistence.adapters.cursor_store import SqliteCursorStore
from modex_agent.persistence.adapters.external_session_map_store import (
    SqliteExternalSessionMapStore,
)
from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ
from modex_agent.persistence.adapters.kv_store import SqliteKVStore
from modex_agent.persistence.adapters.message_store import SqliteMessageStore
from modex_agent.persistence.adapters.pool_routing_store import (
    PoolRoutingCorruptionError,
    SqlitePoolRoutingStore,
)
from modex_agent.persistence.adapters.session_store import SqliteSessionStore
from modex_agent.persistence.adapters.todo_store import SqliteTodoStore
from modex_agent.persistence.adapters.turn_state_store import SqliteTurnStateStore
from modex_agent.persistence.adapters.workspace_registry_store import (
    SqliteWorkspaceRegistryStore,
)
from modex_agent.runtime.approval_decision import ApprovalAuditEntry

__all__ = [
    "ApprovalAuditEntry",
    "ApprovalAuditStore",
    "PoolRoutingCorruptionError",
    "SqliteApprovalAuditStore",
    "SqliteCursorStore",
    "SqliteExternalSessionMapStore",
    "SqliteInboxMQ",
    "SqliteKVStore",
    "SqliteMessageStore",
    "SqlitePoolRoutingStore",
    "SqliteSessionStore",
    "SqliteTodoStore",
    "SqliteTurnStateStore",
    "SqliteWorkspaceRegistryStore",
]
