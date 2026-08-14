"""Session-tree store factory — builds the three-store bundle per backend.

Returns the SQLite adapters when the SQLITE backend is selected and a
``WorkspacePersistenceManager`` is available; otherwise the file-based impls.
Mirrors the ``build_*`` pattern from ``examples/bot_project/bot/service/builders.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.scope import RecordScope
from modex_agent.multi_agent.session_tree.store_node import TreeNodeStore
from modex_agent.multi_agent.session_tree.store_track import MessageTrackStore
from modex_agent.multi_agent.session_tree.store_tree import SessionTreeStore
from modex_agent.persistence.config import PersistenceBackend

if TYPE_CHECKING:
    from modex_agent.ioc.configs.app import AppConfig
    from modex_agent.persistence.managers import WorkspacePersistenceManager


def _is_sqlite(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
) -> bool:
    """True when the SQLITE backend is selected and a manager is available."""
    return (
        app_config is not None
        and persistence is not None
        and app_config.persistence.backend is PersistenceBackend.SQLITE
    )


def build_session_tree_stores(
    app_config: AppConfig | None,
    persistence: WorkspacePersistenceManager | None,
    data_dir: Path,
    scope: RecordScope,
) -> tuple[SessionTreeStore, TreeNodeStore, MessageTrackStore]:
    """Build the three session-tree stores for the configured backend.

    Args:
        app_config: Root application config; ``None`` selects the file backend.
        persistence: Workspace persistence manager; ``None`` selects the file
            backend even when ``app_config`` requests SQLITE.
        data_dir: Directory used by the file backend. Subdirectories are
            created per store (``trees/``, ``nodes/``, ``tracks/``) to keep
            record files from colliding.
        scope: Record scope for SQLite-backed stores.

    Returns:
        ``(SessionTreeStore, TreeNodeStore, MessageTrackStore)`` bound to the
        same persistence backend.
    """
    if _is_sqlite(app_config, persistence):
        assert persistence is not None  # narrows for the type checker
        from modex_agent.multi_agent.session_tree.store_node import SqliteTreeNodeStore
        from modex_agent.multi_agent.session_tree.store_track import (
            SqliteMessageTrackStore,
        )
        from modex_agent.multi_agent.session_tree.store_tree import (
            SqliteSessionTreeStore,
        )

        connection = persistence.connection
        return (
            SqliteSessionTreeStore(connection, scope),
            SqliteTreeNodeStore(connection, scope),
            SqliteMessageTrackStore(connection, scope),
        )

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
