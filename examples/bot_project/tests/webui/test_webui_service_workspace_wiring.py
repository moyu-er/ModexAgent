"""Tests for WebUIService workspace wiring.

The old single-active workspace-switch wiring (``_wire_active_stores`` +
``_on_workspace_activate``) was removed in the CUTOVER to the multi-live
stack; these tests now cover the store-property fallbacks (``_home_resources``
not yet set) and the recent-workspaces dir resolution.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from bot.service.web_ui_service import WebUIService


class TestStoreProperties:
    """Regression: _transcript_store and _session_store property getters must
    work when ``_home_resources`` has not been set yet (as happens during
    ``WebUIService.__init__`` before ``super().__init__()`` runs initialize)."""

    def test_transcript_store_falls_back_before_home_materialized(
        self, tmp_path: Path
    ) -> None:
        service = object.__new__(WebUIService)
        service._home_resources = None

        initial_store = MagicMock()
        service._transcript_store = initial_store

        assert service._transcript_store is initial_store
        assert service._emitter_transcript_store is initial_store

    def test_session_store_falls_back_before_home_materialized(
        self, tmp_path: Path
    ) -> None:
        service = object.__new__(WebUIService)
        service._home_resources = None

        initial_store = MagicMock()
        object.__setattr__(service, "_session_store", initial_store)

        assert service._session_store is initial_store


class TestRecentWorkspacesDir:
    def test_uses_configured_data_dir_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RecentWorkspaces must honor AppConfig.paths.data_dir_name, not MODEX_DATA_DIR env var."""
        monkeypatch.delenv("MODEX_DATA_DIR", raising=False)

        service = object.__new__(WebUIService)
        service._app_config = MagicMock()
        service._app_config.paths.data_dir_name = "custom_data"

        with patch.object(
            WebUIService, "_project_dir", new_callable=PropertyMock
        ) as mock_project_dir:
            mock_project_dir.return_value = tmp_path
            recent = service._build_recent_workspaces()

        assert recent._path == tmp_path / "custom_data" / "recent_workspaces.json"
