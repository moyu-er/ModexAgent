from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.workspace_registry_store import (
    SqliteWorkspaceRegistryStore,
)
from modex_agent.workspace.record import WorkspaceRecord
from modex_agent.workspace.registry import WorkspaceRegistryStore
from modex_agent.workspace.store import GlobalWorkspaceStore


def _record(
    target_path: str,
    *,
    workspace_id: str | None = None,
    display_name: str | None = None,
    created_at: int = 1767225600000,  # 2026-01-01T00:00:00Z in ms epoch
    last_active: int = 1767312000000,  # 2026-01-02T00:00:00Z in ms epoch
    is_home: bool = False,
    metadata_json: dict[str, Any] | None = None,
) -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=workspace_id or str(uuid.uuid4()),
        target_path=target_path,
        display_name=display_name,
        created_at=created_at,
        last_active=last_active,
        is_home=is_home,
        metadata_json=metadata_json or {},
    )


@pytest.fixture(params=["file", "sqlite"])
async def registry_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncGenerator[WorkspaceRegistryStore]:
    """Yield each production adapter through the shared async interface."""
    if request.param == "file":
        yield GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
    else:
        mgr = ConnectionManager(tmp_path / "registry.db", DatabaseKind.REGISTRY)
        await mgr.open()
        yield SqliteWorkspaceRegistryStore(mgr)
        await mgr.close()


class TestWorkspaceRegistryStoreConformance:
    """Same behavior on both backends."""

    async def test_list_empty_returns_empty(self, registry_store: WorkspaceRegistryStore) -> None:
        assert await registry_store.list_workspaces() == []

    async def test_upsert_then_get(
        self, registry_store: WorkspaceRegistryStore, tmp_path: Path
    ) -> None:
        target = str(tmp_path / "proj-a")
        record = _record(target, display_name="Proj A")
        await registry_store.upsert_workspace(record)
        got = await registry_store.get_workspace(target)
        assert got is not None
        assert got.workspace_id == record.workspace_id
        assert got.display_name == "Proj A"

    async def test_upsert_replaces_existing(
        self, registry_store: WorkspaceRegistryStore, tmp_path: Path
    ) -> None:
        target = str(tmp_path / "proj-a")
        await registry_store.upsert_workspace(_record(target, display_name="Old"))
        await registry_store.upsert_workspace(_record(target, display_name="New", is_home=True))
        got = await registry_store.get_workspace(target)
        assert got is not None
        assert got.display_name == "New"
        assert got.is_home is True

    async def test_delete_removes_workspace(
        self, registry_store: WorkspaceRegistryStore, tmp_path: Path
    ) -> None:
        target = str(tmp_path / "proj-a")
        await registry_store.upsert_workspace(_record(target))
        await registry_store.delete_workspace(target)
        assert await registry_store.get_workspace(target) is None

    async def test_delete_missing_is_noop(
        self, registry_store: WorkspaceRegistryStore, tmp_path: Path
    ) -> None:
        await registry_store.delete_workspace(str(tmp_path / "nope"))  # must not raise

    async def test_list_returns_all(
        self, registry_store: WorkspaceRegistryStore, tmp_path: Path
    ) -> None:
        await registry_store.upsert_workspace(_record(str(tmp_path / "a")))
        await registry_store.upsert_workspace(_record(str(tmp_path / "b")))
        records = await registry_store.list_workspaces()
        assert len(records) == 2

    async def test_get_missing_returns_none(
        self, registry_store: WorkspaceRegistryStore, tmp_path: Path
    ) -> None:
        assert await registry_store.get_workspace(str(tmp_path / "nope")) is None
