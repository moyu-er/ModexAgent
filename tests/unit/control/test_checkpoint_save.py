"""Test runtime state save creates missing directory (TDD fix for FileNotFoundError)."""
import asyncio
import shutil
import pytest
from framework.control.checkpoint import JsonFileRuntimeStateStore


class TestRuntimeStateStoreSaveCreatesDirectory:
    """TDD: save() must create workspace directory if missing."""

    def test_save_creates_missing_workspace_directory(self, tmp_path):
        """Reproduce: workspace dir deleted after init → save must recreate it."""
        workspace = tmp_path / "data" / "approval" / "checkpoints"
        store = JsonFileRuntimeStateStore(workspace)
        assert workspace.exists()

        # Simulate workspace directory being cleaned up by external process
        shutil.rmtree(workspace)
        assert not workspace.exists()

        # save() must recreate the missing directory
        asyncio.run(store.save("test:id:latest", {"messages": [], "iteration": 0}))

        # File must exist
        expected = workspace / "test_id_latest.json"
        assert expected.exists()

    def test_save_with_existing_workspace_still_works(self, tmp_path):
        """save() works normally when workspace is intact."""
        workspace = tmp_path / "checkpoints"
        store = JsonFileRuntimeStateStore(workspace)

        asyncio.run(store.save("session:agent:latest", {"messages": []}))
        expected = workspace / "session_agent_latest.json"
        assert expected.exists()
