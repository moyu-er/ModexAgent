"""Persistence-backend bundles — the persistence store families' constructor face.

One named persistence backend owns the construction of EVERY
persistence-backed store family (inbox, turn state, sessions, pool routing,
external session map, memory registry, workspace registry, approval audit,
session trees) plus the shared-manager opening pair. ``persistence.backend``
in the config names a bundle in the process-level
:class:`~modex_agent.core.backend_registry.BackendRegistry`; the framework
bundles ``file`` and ``sqlite`` are seeded into the registry at first
access, and a third-party plugin registers its own bundle under a new name
through
:meth:`~modex_agent.plugins.loader.PluginRegistrationContext.register_persistence_backend`
— a third backend then deploys with zero framework changes.

This module is the family's single owner: the ABC, the two bundled
implementations (the branch bodies of the former backend-selection
factories, moved verbatim), the bundled name constants, and the
process-level registry accessor the assembly factories resolve through. It
sits beside :mod:`modex_agent.plugins.backends` (the service-level
factory faces) and :mod:`modex_agent.core.backend_registry` (the generic
registry container), not
inside it, because the loader must reference the ABC without importing the
bundled implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.backend_registry import BackendRegistry

if TYPE_CHECKING:
    from modex_agent.app.config import AppConfig
    from modex_agent.core.external_session import ExternalSessionMapStore
    from modex_agent.core.inbox import InboxMQ
    from modex_agent.core.scope import RecordScope
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.stores import PoolRoutingStore
    from modex_agent.core.turn.approval_decision import ApprovalAuditStore
    from modex_agent.core.turn.codec import RuntimeStateCodecRegistry
    from modex_agent.core.turn.store import TurnStateStore
    from modex_agent.memory.registry import MemoryStoreRegistry
    from modex_agent.multi_agent.session_tree.store_node import TreeNodeStore
    from modex_agent.multi_agent.session_tree.store_track import MessageTrackStore
    from modex_agent.multi_agent.session_tree.store_tree import SessionTreeStore
    from modex_agent.persistence.managers import (
        RegistryPersistenceManager,
        WorkspacePersistenceManager,
    )
    from modex_agent.persistence.session_store import SessionStore
    from modex_agent.workspace.registry import ScopeRegistryStore

__all__ = [
    "FILE_BACKEND_NAME",
    "SQLITE_BACKEND_NAME",
    "FilePersistenceBackendBundle",
    "PersistenceBackendBundle",
    "SqlitePersistenceBackendBundle",
    "persistence_backend_registry",
    "resolve_persistence_backend",
]

FILE_BACKEND_NAME = "file"
"""The bundled file-based persistence backend name (the no-database default)."""

SQLITE_BACKEND_NAME = "sqlite"
"""The bundled SQLite persistence backend name (the config default)."""


class PersistenceBackendBundle(ABC):
    """One named persistence backend: the constructor for every
    persistence-backed store family.

    The ``file`` and ``sqlite`` bundles are the framework defaults; a
    third-party plugin registers its own bundle under a new name and
    ``persistence.backend`` selects it with zero framework changes.

    The bundle IS the backend choice — members do not re-read config. They
    receive the open persistence managers (``None`` on backends that open
    none) and the path/scope/resolver/codec arguments exactly as the
    ``build_*`` assembly factories always threaded them.
    """

    @abstractmethod
    def build_inbox(
        self,
        persistence: WorkspacePersistenceManager | None,
        inbox_dir: Path,
        db_path: Path,
        scope: RecordScope,
    ) -> InboxMQ: ...

    @abstractmethod
    def build_turn_state_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        turns_dir: Path,
        codec_registry: RuntimeStateCodecRegistry,
    ) -> TurnStateStore: ...

    @abstractmethod
    def build_session_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        session_index_dir: Path,
        pool_resolver: Callable[[SessionInfo], str],
        data_dir_name: str,
    ) -> SessionStore: ...

    @abstractmethod
    def build_pool_routing_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        data_dir: Path,
        db_path: Path,
    ) -> PoolRoutingStore: ...

    @abstractmethod
    def build_external_session_map_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        workspace_dir: Path,
        scope: RecordScope,
    ) -> ExternalSessionMapStore: ...

    @abstractmethod
    def build_memory_registry(
        self,
        persistence: WorkspacePersistenceManager | None,
        memory_dir: Path,
        scope: RecordScope,
    ) -> MemoryStoreRegistry | None: ...

    @abstractmethod
    def build_workspace_registry_store(
        self,
        registry_persistence: RegistryPersistenceManager | None,
        home: Path,
        data_dir_name: str,
    ) -> ScopeRegistryStore: ...

    @abstractmethod
    def build_approval_audit_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        scope: RecordScope,
    ) -> ApprovalAuditStore | None: ...

    @abstractmethod
    def build_session_tree_stores(
        self,
        persistence: WorkspacePersistenceManager | None,
        data_dir: Path,
        scope: RecordScope,
    ) -> tuple[SessionTreeStore, TreeNodeStore, MessageTrackStore]: ...

    @abstractmethod
    def open_registry_manager(
        self, registry_db_path: Path
    ) -> RegistryPersistenceManager | None:
        """Construct this backend's registry-level manager (``None`` when the
        backend opens no registry DB — the caller owns ``open()``/``close()``)."""

    @abstractmethod
    def open_workspace_manager(
        self, db_path: Path
    ) -> WorkspacePersistenceManager | None:
        """Construct this backend's workspace-level manager (``None`` when the
        backend opens no workspace DB — the caller owns ``open()``/``close()``)."""


class FilePersistenceBackendBundle(PersistenceBackendBundle):
    """The bundled ``file`` backend — the file-based store implementations."""

    def build_inbox(
        self,
        persistence: WorkspacePersistenceManager | None,
        inbox_dir: Path,
        db_path: Path,
        scope: RecordScope,
    ) -> InboxMQ:
        from modex_agent.multi_agent.inbox.server_local import LocalFileInboxMQ

        return LocalFileInboxMQ(workspace=inbox_dir)

    def build_turn_state_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        turns_dir: Path,
        codec_registry: RuntimeStateCodecRegistry,
    ) -> TurnStateStore:
        from modex_agent.runtime.store import JsonFileTurnStateStore

        return JsonFileTurnStateStore(turns_dir, codec_registry)

    def build_session_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        session_index_dir: Path,
        pool_resolver: Callable[[SessionInfo], str],
        data_dir_name: str,
    ) -> SessionStore:
        from modex_agent.persistence.adapters.pool_session_store import (
            WorkspacePoolSessionStore,
        )

        return WorkspacePoolSessionStore(
            base_dir=session_index_dir,
            pool_resolver=pool_resolver,
            data_dir_name=data_dir_name,
        )

    def build_pool_routing_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        data_dir: Path,
        db_path: Path,
    ) -> PoolRoutingStore:
        from modex_agent.multi_agent.pool_router import LocalFilePoolRoutingStore

        return LocalFilePoolRoutingStore(data_dir=data_dir)

    def build_external_session_map_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        workspace_dir: Path,
        scope: RecordScope,
    ) -> ExternalSessionMapStore:
        from modex_agent.agents.external.paths import ExternalPaths
        from modex_agent.agents.external.session_store import (
            LocalFileExternalSessionMapStore,
        )

        return LocalFileExternalSessionMapStore(ExternalPaths(workspace_dir))

    def build_memory_registry(
        self,
        persistence: WorkspacePersistenceManager | None,
        memory_dir: Path,
        scope: RecordScope,
    ) -> MemoryStoreRegistry | None:
        return None

    def build_workspace_registry_store(
        self,
        registry_persistence: RegistryPersistenceManager | None,
        home: Path,
        data_dir_name: str,
    ) -> ScopeRegistryStore:
        from modex_agent.workspace.store import GlobalWorkspaceStore

        return GlobalWorkspaceStore(home=home, data_dir_name=data_dir_name)

    def build_approval_audit_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        scope: RecordScope,
    ) -> ApprovalAuditStore | None:
        return None

    def build_session_tree_stores(
        self,
        persistence: WorkspacePersistenceManager | None,
        data_dir: Path,
        scope: RecordScope,
    ) -> tuple[SessionTreeStore, TreeNodeStore, MessageTrackStore]:
        from modex_agent.multi_agent.session_tree.store_node import LocalFileTreeNodeStore
        from modex_agent.multi_agent.session_tree.store_track import (
            LocalFileMessageTrackStore,
        )
        from modex_agent.multi_agent.session_tree.store_tree import (
            LocalFileSessionTreeStore,
        )

        return (
            LocalFileSessionTreeStore(data_dir / "trees"),
            LocalFileTreeNodeStore(data_dir / "nodes"),
            LocalFileMessageTrackStore(data_dir / "tracks"),
        )

    def open_registry_manager(
        self, registry_db_path: Path
    ) -> RegistryPersistenceManager | None:
        return None

    def open_workspace_manager(self, db_path: Path) -> WorkspacePersistenceManager | None:
        return None


class SqlitePersistenceBackendBundle(PersistenceBackendBundle):
    """The bundled ``sqlite`` backend — the SQLite adapters bound to the
    workspace/registry persistence managers (the hybrid SQLite+file layer).

    Manager presence IS part of the historical selection semantics: a
    ``None`` manager (harnesses and partial boots that never opened one)
    falls back to the file bundle's store for that family — exactly the
    pre-bundle behavior where the manager check gated the sqlite branch.
    """

    def build_inbox(
        self,
        persistence: WorkspacePersistenceManager | None,
        inbox_dir: Path,
        db_path: Path,
        scope: RecordScope,
    ) -> InboxMQ:
        if persistence is None:
            return _FILE_BUNDLE.build_inbox(persistence, inbox_dir, db_path, scope)
        manager = persistence
        from modex_agent.persistence.adapters.inbox_mq import SqliteInboxMQ

        return SqliteInboxMQ(
            db_path,
            scope,
            connection=manager.connection,
        )

    def build_turn_state_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        turns_dir: Path,
        codec_registry: RuntimeStateCodecRegistry,
    ) -> TurnStateStore:
        if persistence is None:
            return _FILE_BUNDLE.build_turn_state_store(persistence, turns_dir, codec_registry)
        manager = persistence
        from modex_agent.persistence.adapters.turn_state_store import SqliteTurnStateStore

        return SqliteTurnStateStore(manager.connection, codec_registry)

    def build_session_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        session_index_dir: Path,
        pool_resolver: Callable[[SessionInfo], str],
        data_dir_name: str,
    ) -> SessionStore:
        if persistence is None:
            return _FILE_BUNDLE.build_session_store(persistence, session_index_dir, pool_resolver, data_dir_name)
        manager = persistence
        from modex_agent.persistence.adapters.session_store import SqliteSessionStore

        return SqliteSessionStore(manager.connection)

    def build_pool_routing_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        data_dir: Path,
        db_path: Path,
    ) -> PoolRoutingStore:
        if persistence is None:
            return _FILE_BUNDLE.build_pool_routing_store(persistence, data_dir, db_path)
        from modex_agent.persistence.adapters.pool_routing_store import SqlitePoolRoutingStore

        return SqlitePoolRoutingStore(db_path)

    def build_external_session_map_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        workspace_dir: Path,
        scope: RecordScope,
    ) -> ExternalSessionMapStore:
        if persistence is None:
            return _FILE_BUNDLE.build_external_session_map_store(persistence, workspace_dir, scope)
        manager = persistence
        from modex_agent.persistence.adapters.external_session_map_store import (
            SqliteExternalSessionMapStore,
        )

        return SqliteExternalSessionMapStore(manager.connection, scope)

    def build_memory_registry(
        self,
        persistence: WorkspacePersistenceManager | None,
        memory_dir: Path,
        scope: RecordScope,
    ) -> MemoryStoreRegistry | None:
        if persistence is None:
            return _FILE_BUNDLE.build_memory_registry(persistence, memory_dir, scope)
        manager = persistence
        from modex_agent.memory.registry import HybridMemoryStoreRegistry

        return HybridMemoryStoreRegistry(
            file_root=memory_dir,
            persistence=manager,
            base_scope=scope,
        )

    def build_workspace_registry_store(
        self,
        registry_persistence: RegistryPersistenceManager | None,
        home: Path,
        data_dir_name: str,
    ) -> ScopeRegistryStore:
        if registry_persistence is None:
            return _FILE_BUNDLE.build_workspace_registry_store(
                registry_persistence, home, data_dir_name
            )
        return registry_persistence.store

    def build_approval_audit_store(
        self,
        persistence: WorkspacePersistenceManager | None,
        scope: RecordScope,
    ) -> ApprovalAuditStore | None:
        if persistence is None:
            return _FILE_BUNDLE.build_approval_audit_store(persistence, scope)
        manager = persistence
        from modex_agent.persistence.adapters.approval_audit_store import (
            SqliteApprovalAuditStore,
        )

        return SqliteApprovalAuditStore(manager.connection, scope)

    def build_session_tree_stores(
        self,
        persistence: WorkspacePersistenceManager | None,
        data_dir: Path,
        scope: RecordScope,
    ) -> tuple[SessionTreeStore, TreeNodeStore, MessageTrackStore]:
        if persistence is None:
            return _FILE_BUNDLE.build_session_tree_stores(persistence, data_dir, scope)
        manager = persistence
        from modex_agent.multi_agent.session_tree.store_node import SqliteTreeNodeStore
        from modex_agent.multi_agent.session_tree.store_track import (
            SqliteMessageTrackStore,
        )
        from modex_agent.multi_agent.session_tree.store_tree import (
            SqliteSessionTreeStore,
        )

        connection = manager.connection
        return (
            SqliteSessionTreeStore(connection, scope),
            SqliteTreeNodeStore(connection, scope),
            SqliteMessageTrackStore(connection, scope),
        )

    def open_registry_manager(
        self, registry_db_path: Path
    ) -> RegistryPersistenceManager | None:
        from modex_agent.persistence.managers import RegistryPersistenceManager

        return RegistryPersistenceManager(registry_db_path)

    def open_workspace_manager(self, db_path: Path) -> WorkspacePersistenceManager | None:
        from modex_agent.persistence.managers import WorkspacePersistenceManager

        return WorkspacePersistenceManager(db_path)


#: The shared file-bundle instance the sqlite bundle's manager-absent
#: fallbacks delegate to (the historical manager-presence selection).
_FILE_BUNDLE = FilePersistenceBackendBundle()

_registry: BackendRegistry[PersistenceBackendBundle] | None = None


def persistence_backend_registry() -> BackendRegistry[PersistenceBackendBundle]:
    """The process-level persistence-backend registry — the resolution face
    the assembly factories and the service manager gate read.

    Created on first access with the two bundled backends seeded (``file``,
    ``sqlite``), so every resolution road works with or without a plugin
    load; third-party registrations land here through the plugin loader
    (``PluginDiscoveryConfig.persistence_backends`` carries this registry).
    """
    global _registry
    if _registry is None:
        registry = BackendRegistry[PersistenceBackendBundle](family="persistence")
        registry.register(FILE_BACKEND_NAME, FilePersistenceBackendBundle)
        registry.register(SQLITE_BACKEND_NAME, SqlitePersistenceBackendBundle)
        _registry = registry
    return _registry


def resolve_persistence_backend(app_config: AppConfig | None) -> PersistenceBackendBundle:
    """Resolve the bundle named by ``app_config.persistence.backend``.

    ``app_config=None`` selects the bundled ``file`` backend — the
    unconfigured assembly road keeps the file outcome it always had. An
    unknown name raises the registry's loud error listing the registered
    options.
    """
    name = (
        app_config.persistence.backend if app_config is not None else FILE_BACKEND_NAME
    )
    return persistence_backend_registry().resolve(name)
