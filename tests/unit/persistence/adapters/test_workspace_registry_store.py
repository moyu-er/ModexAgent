"""T23 tests for SqliteWorkspaceRegistryStore — registry-DB-backed adapter.

Covers:
- Workspace CRUD: upsert / get / get_workspace_by_id / delete / list.
- ``list_workspaces`` ordering (``last_active``, ``created_at``), limit, and
  the recent-workspaces query (``order_by="last_active"`` default).
- ``metadata_json`` / ``display_name`` / ``is_home`` roundtrip.
- ``session_workspace_map`` CRUD: set / get / delete / list_session_prefixes.
- FK CASCADE: deleting a workspace cascades to its session map rows.
- Upsert preserves session-map rows (uses ON CONFLICT, not INSERT OR REPLACE).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.workspace_registry_store import (
    SqliteWorkspaceRegistryStore,
)
from modex_agent.workspace.record import WorkspaceRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(
    target_path: str,
    *,
    workspace_id: str | None = None,
    display_name: str | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
    last_active: str = "2026-01-01T00:00:00Z",
    is_home: bool = False,
    metadata_json: dict[str, object] | None = None,
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


async def _open_store(tmp_path: Path) -> tuple[ConnectionManager, SqliteWorkspaceRegistryStore]:
    """Open a registry DB and return (manager, store) ready for use."""
    manager = ConnectionManager(tmp_path / "registry.db", DatabaseKind.REGISTRY)
    await manager.open()
    store = SqliteWorkspaceRegistryStore(manager)
    return manager, store


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------


class TestWorkspaceUpsertAndGet:
    @pytest.mark.asyncio
    async def test_upsert_and_get_workspace(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        record = _record(str(tmp_path / "proj-a"), display_name="Proj A")
        try:
            await store.upsert_workspace(record)
            got = await store.get_workspace(str(tmp_path / "proj-a"))
            assert got is not None
            assert got.workspace_id == record.workspace_id
            assert got.target_path == str((tmp_path / "proj-a").resolve())
            assert got.display_name == "Proj A"
            assert got.created_at == record.created_at
            assert got.last_active == record.last_active
            assert got.is_home is False
            assert got.metadata_json == {}
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_get_workspace_returns_none_when_absent(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            assert await store.get_workspace(str(tmp_path / "absent")) is None
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_get_workspace_resolves_target_path(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        record = _record(str(tmp_path / "proj"))
        try:
            await store.upsert_workspace(record)
            # Query with a non-canonical (relative-ish) form still resolves.
            got = await store.get_workspace(str(tmp_path / "proj"))
            assert got is not None
            assert got.target_path == str((tmp_path / "proj").resolve())
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_get_workspace_by_id_returns_record(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        record = _record(str(tmp_path / "proj"), workspace_id="ws-xyz")
        try:
            await store.upsert_workspace(record)
            got = await store.get_workspace_by_id("ws-xyz")
            assert got is not None
            assert got.workspace_id == "ws-xyz"
            assert got.target_path == str((tmp_path / "proj").resolve())
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_get_workspace_by_id_returns_none_when_absent(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            assert await store.get_workspace_by_id("nope") is None
        finally:
            await manager.close()


class TestWorkspaceUpsertReplace:
    @pytest.mark.asyncio
    async def test_upsert_replaces_existing_by_target_path(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        target = str(tmp_path / "proj")
        try:
            r1 = _record(target, display_name="Old", last_active="2026-01-01T00:00:00Z")
            await store.upsert_workspace(r1)
            r2 = _record(
                target,
                workspace_id=r1.workspace_id,
                display_name="New",
                last_active="2026-06-01T00:00:00Z",
            )
            await store.upsert_workspace(r2)
            got = await store.get_workspace(target)
            assert got is not None
            assert got.display_name == "New"
            assert got.last_active == "2026-06-01T00:00:00Z"
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_upsert_preserves_session_map_on_replace(self, tmp_path: Path) -> None:
        """A re-upsert of the same workspace must NOT wipe its session map rows.

        This guards the ON CONFLICT(target_path) DO UPDATE choice against a
        regression to INSERT OR REPLACE (which DELETEs the row, firing the
        ON DELETE CASCADE on session_workspace_map).
        """
        manager, store = await _open_store(tmp_path)
        target = str(tmp_path / "proj")
        ws_id = "ws-stable"
        try:
            await store.upsert_workspace(_record(target, workspace_id=ws_id))
            await store.set_session_workspace("sess-1", ws_id)
            await store.upsert_workspace(
                _record(
                    target,
                    workspace_id="ws-replacement",
                    last_active="2026-12-01T00:00:00Z",
                )
            )
            workspace = await store.get_workspace(target)
            mapped = await store.get_session_workspace("sess-1")
            assert workspace is not None
            assert workspace.workspace_id == ws_id
            assert mapped == ws_id
        finally:
            await manager.close()


class TestWorkspaceDelete:
    @pytest.mark.asyncio
    async def test_delete_workspace_removes_record(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        target = str(tmp_path / "proj")
        try:
            await store.upsert_workspace(_record(target))
            assert await store.get_workspace(target) is not None
            await store.delete_workspace(target)
            assert await store.get_workspace(target) is None
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_delete_workspace_noop_when_absent(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            await store.delete_workspace(str(tmp_path / "nonexistent"))
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_delete_workspace_cascades_session_map(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        target = str(tmp_path / "proj")
        ws_id = "ws-cascade"
        try:
            await store.upsert_workspace(_record(target, workspace_id=ws_id))
            await store.set_session_workspace("sess-a", ws_id)
            await store.set_session_workspace("sess-b", ws_id)
            await store.delete_workspace(target)
            assert await store.get_session_workspace("sess-a") is None
            assert await store.get_session_workspace("sess-b") is None
        finally:
            await manager.close()


# ---------------------------------------------------------------------------
# list_workspaces — ordering, limit, recent-workspaces query
# ---------------------------------------------------------------------------


class TestListWorkspaces:
    @pytest.mark.asyncio
    async def test_list_empty_returns_empty(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            assert await store.list_workspaces() == []
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_list_orders_by_last_active_desc(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            await store.upsert_workspace(
                _record(str(tmp_path / "old"), last_active="2026-01-01T00:00:00Z")
            )
            await store.upsert_workspace(
                _record(str(tmp_path / "new"), last_active="2026-06-01T00:00:00Z")
            )
            await store.upsert_workspace(
                _record(str(tmp_path / "mid"), last_active="2026-03-01T00:00:00Z")
            )
            records = await store.list_workspaces(order_by="last_active")
            assert len(records) == 3
            assert records[0].target_path == str((tmp_path / "new").resolve())
            assert records[1].target_path == str((tmp_path / "mid").resolve())
            assert records[2].target_path == str((tmp_path / "old").resolve())
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_list_orders_by_created_at_desc(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            await store.upsert_workspace(
                _record(
                    str(tmp_path / "first"),
                    created_at="2026-01-01T00:00:00Z",
                    last_active="2026-06-01T00:00:00Z",
                )
            )
            await store.upsert_workspace(
                _record(
                    str(tmp_path / "second"),
                    created_at="2026-05-01T00:00:00Z",
                    last_active="2026-06-01T00:00:00Z",
                )
            )
            records = await store.list_workspaces(order_by="created_at")
            assert len(records) == 2
            assert records[0].target_path == str((tmp_path / "second").resolve())
            assert records[1].target_path == str((tmp_path / "first").resolve())
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_list_respects_limit(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            for i in range(5):
                await store.upsert_workspace(
                    _record(
                        str(tmp_path / f"ws{i}"),
                        last_active=f"2026-01-0{i + 1}T00:00:00Z",
                    )
                )
            records = await store.list_workspaces(order_by="last_active", limit=3)
            assert len(records) == 3
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_list_default_limit_20(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            for i in range(25):
                await store.upsert_workspace(
                    _record(
                        str(tmp_path / f"ws{i}"),
                        last_active=f"2026-01-0{(i % 9) + 1}T00:00:00Z",
                    )
                )
            records = await store.list_workspaces()
            assert len(records) == 20
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_list_unknown_order_by_raises(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            await store.upsert_workspace(_record(str(tmp_path / "ws")))
            with pytest.raises(ValueError):
                await store.list_workspaces(order_by="unknown_field")
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_recent_workspaces_query_returns_most_recent_first(self, tmp_path: Path) -> None:
        """The WebUI 'recent workspaces' query: default list_workspaces()."""
        manager, store = await _open_store(tmp_path)
        try:
            await store.upsert_workspace(
                _record(str(tmp_path / "a"), last_active="2026-01-01T00:00:00Z")
            )
            await store.upsert_workspace(
                _record(str(tmp_path / "b"), last_active="2026-07-01T00:00:00Z")
            )
            recent = await store.list_workspaces()
            assert recent[0].target_path == str((tmp_path / "b").resolve())
            assert recent[1].target_path == str((tmp_path / "a").resolve())
        finally:
            await manager.close()


# ---------------------------------------------------------------------------
# Field roundtrip — metadata_json, display_name, is_home
# ---------------------------------------------------------------------------


class TestFieldRoundtrip:
    @pytest.mark.asyncio
    async def test_metadata_json_roundtrip(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            await store.upsert_workspace(
                _record(
                    str(tmp_path / "proj"),
                    metadata_json={"origin": "webui", "tags": ["a", "b"]},
                )
            )
            got = await store.get_workspace(str(tmp_path / "proj"))
            assert got is not None
            assert got.metadata_json == {"origin": "webui", "tags": ["a", "b"]}
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_display_name_none_roundtrip(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            await store.upsert_workspace(_record(str(tmp_path / "proj")))
            got = await store.get_workspace(str(tmp_path / "proj"))
            assert got is not None
            assert got.display_name is None
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_is_home_true_roundtrip(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            await store.upsert_workspace(_record(str(tmp_path), is_home=True))
            got = await store.get_workspace(str(tmp_path))
            assert got is not None
            assert got.is_home is True
        finally:
            await manager.close()


# ---------------------------------------------------------------------------
# session_workspace_map CRUD
# ---------------------------------------------------------------------------


class TestSessionWorkspaceMap:
    @pytest.mark.asyncio
    async def test_set_and_get_session_workspace(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            await store.upsert_workspace(_record(str(tmp_path / "proj"), workspace_id="ws-1"))
            await store.set_session_workspace("sess-abc", "ws-1")
            assert await store.get_session_workspace("sess-abc") == "ws-1"
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_get_session_workspace_returns_none_when_absent(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            assert await store.get_session_workspace("nope") is None
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_set_session_workspace_replaces_existing(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            await store.upsert_workspace(_record(str(tmp_path / "a"), workspace_id="ws-a"))
            await store.upsert_workspace(_record(str(tmp_path / "b"), workspace_id="ws-b"))
            await store.set_session_workspace("sess-1", "ws-a")
            await store.set_session_workspace("sess-1", "ws-b")
            assert await store.get_session_workspace("sess-1") == "ws-b"
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_delete_session_workspace_removes_mapping(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            await store.upsert_workspace(_record(str(tmp_path / "proj"), workspace_id="ws-1"))
            await store.set_session_workspace("sess-1", "ws-1")
            await store.delete_session_workspace("sess-1")
            assert await store.get_session_workspace("sess-1") is None
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_delete_session_workspace_noop_when_absent(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            await store.delete_session_workspace("never-existed")
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_list_session_prefixes_for_workspace(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            await store.upsert_workspace(_record(str(tmp_path / "proj"), workspace_id="ws-1"))
            await store.set_session_workspace("sess-a", "ws-1")
            await store.set_session_workspace("sess-b", "ws-1")
            await store.set_session_workspace("sess-c", "ws-1")
            prefixes = await store.list_session_prefixes("ws-1")
            assert sorted(prefixes) == ["sess-a", "sess-b", "sess-c"]
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_list_session_prefixes_empty_when_none(self, tmp_path: Path) -> None:
        manager, store = await _open_store(tmp_path)
        try:
            assert await store.list_session_prefixes("ws-none") == []
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_set_session_workspace_rejects_unknown_workspace(self, tmp_path: Path) -> None:
        """FK constraint: mapping to a non-existent workspace_id must fail."""
        import sqlite3

        manager, store = await _open_store(tmp_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                await store.set_session_workspace("sess-x", "ws-missing")
        finally:
            await manager.close()
