"""Tests for the session-tree store IOC factory."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from modex_agent.core.scope import RecordScope
from modex_agent.ioc.factories.session_tree import build_session_tree_stores
from modex_agent.multi_agent.session_tree import (
    LocalFileMessageTrackStore,
    LocalFileSessionTreeStore,
    LocalFileTreeNodeStore,
    MessageTrackStore,
    SessionTreeStore,
    SqliteMessageTrackStore,
    SqliteSessionTreeStore,
    SqliteTreeNodeStore,
    TreeNodeStore,
)
from modex_agent.persistence.config import PersistenceBackend


class TestBuildSessionTreeStores:
    def test_file_backend_with_none_config(self, tmp_path: Path) -> None:
        """No app_config → file backend returns LocalFile implementations."""
        scope = RecordScope(workspace_id="test")
        tree, node, track = build_session_tree_stores(None, None, tmp_path, scope)

        assert isinstance(tree, LocalFileSessionTreeStore)
        assert isinstance(node, LocalFileTreeNodeStore)
        assert isinstance(track, LocalFileMessageTrackStore)
        assert isinstance(tree, SessionTreeStore)
        assert isinstance(node, TreeNodeStore)
        assert isinstance(track, MessageTrackStore)

    def test_file_backend_creates_subdirectories(self, tmp_path: Path) -> None:
        """LocalFile stores get separate subdirectories under data_dir."""
        scope = RecordScope()
        build_session_tree_stores(None, None, tmp_path, scope)

        assert (tmp_path / "trees").is_dir()
        assert (tmp_path / "nodes").is_dir()

    def test_sqlite_backend_returns_sqlite_stores(self, tmp_path: Path) -> None:
        """SQLITE backend with persistence manager returns Sqlite implementations."""
        app_config = MagicMock()
        app_config.persistence.backend = PersistenceBackend.SQLITE
        persistence = MagicMock()
        persistence.connection = MagicMock()

        scope = RecordScope(workspace_id="test")
        tree, node, track = build_session_tree_stores(
            app_config, persistence, tmp_path, scope
        )

        assert isinstance(tree, SqliteSessionTreeStore)
        assert isinstance(node, SqliteTreeNodeStore)
        assert isinstance(track, SqliteMessageTrackStore)

    def test_sqlite_backend_without_persistence_falls_back_to_file(
        self, tmp_path: Path
    ) -> None:
        """SQLITE backend but no persistence manager → file backend fallback."""
        app_config = MagicMock()
        app_config.persistence.backend = PersistenceBackend.SQLITE

        scope = RecordScope()
        tree, node, track = build_session_tree_stores(
            app_config, None, tmp_path, scope
        )

        assert isinstance(tree, LocalFileSessionTreeStore)
        assert isinstance(node, LocalFileTreeNodeStore)
        assert isinstance(track, LocalFileMessageTrackStore)

    def test_file_backend_config_selects_file(self, tmp_path: Path) -> None:
        """FILE backend with a persistence manager still uses file impls."""
        app_config = MagicMock()
        app_config.persistence.backend = PersistenceBackend.FILE
        persistence = MagicMock()

        scope = RecordScope()
        tree, node, track = build_session_tree_stores(
            app_config, persistence, tmp_path, scope
        )

        assert isinstance(tree, LocalFileSessionTreeStore)
        assert isinstance(node, LocalFileTreeNodeStore)
        assert isinstance(track, LocalFileMessageTrackStore)
