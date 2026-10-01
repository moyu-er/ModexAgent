"""Persistence backend-selection factories for pool/workspace assembly.

Promoted from ``examples/bot_project/bot/service/builders.py`` (W4a, plan
SD-7): each factory selects the SQLite adapter when the config picks
``SQLITE`` and a workspace persistence manager is available, otherwise the
file-based implementation. The selection is generic deployment wiring — it
lives with the assembly layer, not with any one backend, because the FILE
fallbacks span the persistence/memory/multi_agent/agents/runtime domains.

All factories are module-level pure functions over their arguments; callers
thread the deployment's ``AppConfig`` and the open
:class:`~modex_agent.persistence.managers.WorkspacePersistenceManager`
(or ``RegistryPersistenceManager`` for the registry store).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.external_session import ExternalSessionMapStore
from modex_agent.core.inbox import InboxMQ
from modex_agent.core.scope import RecordScope
from modex_agent.core.stores import PoolRoutingStore
from modex_agent.core.turn.approval_decision import ApprovalAuditStore
from modex_agent.core.turn.store import TurnStateStore
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.persistence.session_store import SessionStore

if TYPE_CHECKING:
    from modex_agent.app.config import AppConfig
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.turn.codec import RuntimeStateCodecRegistry
    from modex_agent.memory.registry import MemoryStoreRegistry
    from modex_agent.persistence.managers import (
        RegistryPersistenceManager,
        WorkspacePersistenceManager,
    )
    from modex_agent.workspace.registry import ScopeRegistryStore

__all__ = [
    "build_approval_audit_store",
    "build_external_session_map_store",
    "build_inbox",
    "build_memory_registry",
    "build_pool_routing_store",
    "build_session_store",
    "build_turn_state_store",
    "build_workspace_registry_store",
]


def _is_sqlite(
    app_config: AppConfig | None,
    persistence: object | None,
) -> bool:
    """True when the SQLITE backend is selected and a manager is available."""
    return (
        app_config is not None
        and persistence is not None
        and app_config.persistence.backend is PersistenceBackend.SQLITE
    )


def build_inbox(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    inbox_dir: Path,
    db_path: Path,
    scope: RecordScope,
) -> InboxMQ:
    if _is_sqlite(app_config, persistence):
        from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ

        return SqliteInboxMQ(
            db_path,
            scope,
            connection=persistence.connection,
        )
    from modex_agent.multi_agent.inbox.server_local import LocalFileInboxMQ

    return LocalFileInboxMQ(workspace=inbox_dir)


def build_turn_state_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    turns_dir: Path,
    codec_registry: RuntimeStateCodecRegistry,
) -> TurnStateStore:
    if _is_sqlite(app_config, persistence):
        assert persistence is not None
        from modex_agent.persistence.adapters.turn_state_store import SqliteTurnStateStore

        return SqliteTurnStateStore(persistence.connection, codec_registry)
    from modex_agent.runtime.store import JsonFileTurnStateStore

    return JsonFileTurnStateStore(turns_dir, codec_registry)


def build_session_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    session_index_dir: Path,
    pool_resolver: Callable[[SessionInfo], str],
    data_dir_name: str,
) -> SessionStore:
    if _is_sqlite(app_config, persistence):
        assert persistence is not None
        from modex_agent.persistence.adapters.session_store import SqliteSessionStore

        return SqliteSessionStore(persistence.connection)
    from modex_agent.persistence.adapters.pool_session_store import (
        WorkspacePoolSessionStore,
    )

    return WorkspacePoolSessionStore(
        base_dir=session_index_dir,
        pool_resolver=pool_resolver,
        data_dir_name=data_dir_name,
    )


def build_pool_routing_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    data_dir: Path,
    db_path: Path,
) -> PoolRoutingStore:
    if _is_sqlite(app_config, persistence):
        from modex_agent.persistence.adapters.pool_routing_store import SqlitePoolRoutingStore

        return SqlitePoolRoutingStore(db_path)
    from modex_agent.multi_agent.pool_router import LocalFilePoolRoutingStore

    return LocalFilePoolRoutingStore(data_dir=data_dir)


def build_external_session_map_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    workspace_dir: Path,
    scope: RecordScope,
) -> ExternalSessionMapStore:
    if _is_sqlite(app_config, persistence):
        assert persistence is not None
        from modex_agent.persistence.adapters.external_session_map_store import (
            SqliteExternalSessionMapStore,
        )

        return SqliteExternalSessionMapStore(persistence.connection, scope)
    from modex_agent.agents.external.paths import ExternalPaths
    from modex_agent.agents.external.session_store import (
        LocalFileExternalSessionMapStore,
    )

    return LocalFileExternalSessionMapStore(ExternalPaths(workspace_dir))


def build_memory_registry(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    memory_dir: Path,
    scope: RecordScope,
) -> MemoryStoreRegistry | None:
    if _is_sqlite(app_config, persistence):
        assert persistence is not None
        from modex_agent.memory.registry import HybridMemoryStoreRegistry

        return HybridMemoryStoreRegistry(
            file_root=memory_dir,
            persistence=persistence,
            base_scope=scope,
        )
    return None


def build_workspace_registry_store(
    app_config: AppConfig | None,
    registry_persistence: RegistryPersistenceManager | None,
    home: Path,
    data_dir_name: str,
) -> ScopeRegistryStore:
    if _is_sqlite(app_config, registry_persistence):
        assert registry_persistence is not None
        return registry_persistence.store
    from modex_agent.workspace.store import GlobalWorkspaceStore

    return GlobalWorkspaceStore(home=home, data_dir_name=data_dir_name)


def build_approval_audit_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    scope: RecordScope,
) -> ApprovalAuditStore | None:
    if _is_sqlite(app_config, persistence):
        assert persistence is not None
        from modex_agent.persistence.adapters.approval_audit_store import (
            SqliteApprovalAuditStore,
        )

        return SqliteApprovalAuditStore(persistence.connection, scope)
    return None
