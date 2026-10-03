"""Persistence backend-selection factories for pool/workspace assembly.

Promoted from ``examples/bot_project/bot/service/builders.py`` (W4a, plan
SD-7), W2b turned each factory into a thin resolver: the
``persistence.backend`` config name resolves a
:class:`~modex_agent.plugins.persistence_backends.PersistenceBackendBundle`
from the process-level registry (the bundled ``file``/``sqlite``
implementations plus whatever bundles plugins registered), and the bundle
owns every store family's construction. The factories keep their original
signatures — callers across assembly thread the deployment's
``AppConfig`` and the open
:class:`~modex_agent.persistence.managers.WorkspacePersistenceManager`
(or ``RegistryPersistenceManager`` for the registry store) unchanged; a
``None`` ``app_config`` selects the bundled file backend.
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
from modex_agent.persistence.session_store import SessionStore
from modex_agent.plugins.persistence_backends import resolve_persistence_backend

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


def build_inbox(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    inbox_dir: Path,
    db_path: Path,
    scope: RecordScope,
) -> InboxMQ:
    return resolve_persistence_backend(app_config).build_inbox(
        persistence, inbox_dir, db_path, scope
    )


def build_turn_state_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    turns_dir: Path,
    codec_registry: RuntimeStateCodecRegistry,
) -> TurnStateStore:
    return resolve_persistence_backend(app_config).build_turn_state_store(
        persistence, turns_dir, codec_registry
    )


def build_session_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    session_index_dir: Path,
    pool_resolver: Callable[[SessionInfo], str],
    data_dir_name: str,
) -> SessionStore:
    return resolve_persistence_backend(app_config).build_session_store(
        persistence, session_index_dir, pool_resolver, data_dir_name
    )


def build_pool_routing_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    data_dir: Path,
    db_path: Path,
) -> PoolRoutingStore:
    return resolve_persistence_backend(app_config).build_pool_routing_store(
        persistence, data_dir, db_path
    )


def build_external_session_map_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    workspace_dir: Path,
    scope: RecordScope,
) -> ExternalSessionMapStore:
    return resolve_persistence_backend(app_config).build_external_session_map_store(
        persistence, workspace_dir, scope
    )


def build_memory_registry(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    memory_dir: Path,
    scope: RecordScope,
) -> MemoryStoreRegistry | None:
    return resolve_persistence_backend(app_config).build_memory_registry(
        persistence, memory_dir, scope
    )


def build_workspace_registry_store(
    app_config: AppConfig | None,
    registry_persistence: RegistryPersistenceManager | None,
    home: Path,
    data_dir_name: str,
) -> ScopeRegistryStore:
    return resolve_persistence_backend(app_config).build_workspace_registry_store(
        registry_persistence, home, data_dir_name
    )


def build_approval_audit_store(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    scope: RecordScope,
) -> ApprovalAuditStore | None:
    return resolve_persistence_backend(app_config).build_approval_audit_store(
        persistence, scope
    )
