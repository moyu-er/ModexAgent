"""Session-tree store factory — builds the three-store bundle per backend.

W2b: the factory resolves the
:class:`~modex_agent.plugins.persistence_backends.PersistenceBackendBundle`
named by ``persistence.backend`` and delegates to its
``build_session_tree_stores`` (the bundled ``file``/``sqlite``
implementations plus whatever bundles plugins registered). The signature
is unchanged — callers thread the deployment's ``AppConfig`` and the open
``WorkspacePersistenceManager`` as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.scope import RecordScope
from modex_agent.multi_agent.session_tree.store_node import TreeNodeStore
from modex_agent.multi_agent.session_tree.store_track import MessageTrackStore
from modex_agent.multi_agent.session_tree.store_tree import SessionTreeStore
from modex_agent.plugins.persistence_backends import resolve_persistence_backend

if TYPE_CHECKING:
    from modex_agent.app.config import AppConfig
    from modex_agent.persistence.managers import WorkspacePersistenceManager


def build_session_tree_stores(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    data_dir: Path,
    scope: RecordScope,
) -> tuple[SessionTreeStore, TreeNodeStore, MessageTrackStore]:
    """Build the three session-tree stores for the configured backend.

    Args:
        app_config: Root application config; ``None`` selects the file backend.
        persistence: Workspace persistence manager; the sqlite bundle
            requires it (a ``None`` manager after selecting sqlite is a
            wiring defect raised loudly, not a fallback).
        data_dir: Directory used by the file backend. Subdirectories are
            created per store (``trees/``, ``nodes/``, ``tracks/``) to keep
            record files from colliding.
        scope: Record scope for SQLite-backed stores.

    Returns:
        ``(SessionTreeStore, TreeNodeStore, MessageTrackStore)`` bound to
        the same persistence backend.
    """
    return resolve_persistence_backend(app_config).build_session_tree_stores(
        persistence, data_dir, scope
    )
