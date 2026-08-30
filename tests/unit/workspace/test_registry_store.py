"""TDD tests for ScopeRegistryStore ABC + WorkspaceRecord (T14).

Tests the deepened registry store seam:

- ``WorkspaceRecord`` — frozen Pydantic model carrying workspace identity +
  metadata (workspace_id, target_path, display_name, created_at, last_active,
  is_home, metadata_json).
- ``ScopeRegistryStore`` — ABC with enriched methods:
  ``list_workspaces(order_by, limit)``, ``upsert_workspace(record)``,
  ``delete_workspace(target_path)``, ``get_workspace(target_path)``.
  Legacy ``load_known_targets`` / ``save_known_targets`` are retained as
  concrete (deprecated) compat methods derived from the new abstract ones.
- ``GlobalWorkspaceStore`` — file-backed adapter storing JSON metadata (not a
  bare path list), with backward-compat migration from the legacy format.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.workspace.record import WorkspaceRecord
from modex_agent.workspace.registry import (
    RegistryStore,
    ScopeRegistryStore,
)
from modex_agent.workspace.store import GlobalWorkspaceStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(
    target_path: str,
    *,
    workspace_id: str | None = None,
    display_name: str | None = None,
    created_at: int = 1735689600000,
    last_active: int = 1735689600000,
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


# ---------------------------------------------------------------------------
# WorkspaceRecord model
# ---------------------------------------------------------------------------


class TestWorkspaceRecordModel:
    def test_all_required_fields_accepted(self) -> None:
        r = WorkspaceRecord(
            workspace_id="ws-1",
            target_path="/home/user/proj",
            display_name="proj",
            created_at=1735689600000,
            last_active=1735776000000,
        )
        assert r.workspace_id == "ws-1"
        assert r.target_path == "/home/user/proj"
        assert r.display_name == "proj"
        assert r.created_at == 1735689600000
        assert r.last_active == 1735776000000
        assert r.is_home is False
        assert r.metadata_json == {}

    def test_display_name_defaults_to_none(self) -> None:
        r = WorkspaceRecord(
            workspace_id="ws-1",
            target_path="/p",
            created_at=1735689600000,
            last_active=1735689600000,
        )
        assert r.display_name is None

    def test_is_home_default_false(self) -> None:
        r = WorkspaceRecord(
            workspace_id="ws-1",
            target_path="/p",
            created_at=1735689600000,
            last_active=1735689600000,
        )
        assert r.is_home is False

    def test_metadata_json_default_empty_dict(self) -> None:
        r = WorkspaceRecord(
            workspace_id="ws-1",
            target_path="/p",
            created_at=1735689600000,
            last_active=1735689600000,
        )
        assert r.metadata_json == {}

    def test_metadata_json_default_factory_independent(self) -> None:
        r1 = WorkspaceRecord(
            workspace_id="ws-1",
            target_path="/p",
            created_at=1735689600000,
            last_active=1735689600000,
        )
        r2 = WorkspaceRecord(
            workspace_id="ws-2",
            target_path="/p2",
            created_at=1735689600000,
            last_active=1735689600000,
        )
        r1.metadata_json["key"] = "value"
        assert r2.metadata_json == {}

    def test_frozen_cannot_mutate(self) -> None:
        r = WorkspaceRecord(
            workspace_id="ws-1",
            target_path="/p",
            created_at=1735689600000,
            last_active=1735689600000,
        )
        with pytest.raises(ValidationError):
            r.target_path = "/other"  # type: ignore[misc]

    def test_frozen_cannot_add_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            WorkspaceRecord(
                workspace_id="ws-1",
                target_path="/p",
                created_at=1735689600000,
                last_active=1735689600000,
                unknown_field="x",  # type: ignore[call-arg]
            )

    def test_workspace_id_required(self) -> None:
        with pytest.raises(ValidationError):
            WorkspaceRecord(
                target_path="/p",
                created_at=1735689600000,
                last_active=1735689600000,
            )

    def test_target_path_required(self) -> None:
        with pytest.raises(ValidationError):
            WorkspaceRecord(
                workspace_id="ws-1",
                created_at=1735689600000,
                last_active=1735689600000,
            )

    def test_created_at_required(self) -> None:
        with pytest.raises(ValidationError):
            WorkspaceRecord(
                workspace_id="ws-1",
                target_path="/p",
                last_active=1735689600000,
            )

    def test_last_active_required(self) -> None:
        with pytest.raises(ValidationError):
            WorkspaceRecord(
                workspace_id="ws-1",
                target_path="/p",
                created_at=1735689600000,
            )

    def test_metadata_json_accepts_arbitrary_dict(self) -> None:
        r = WorkspaceRecord(
            workspace_id="ws-1",
            target_path="/p",
            created_at=1735689600000,
            last_active=1735689600000,
            metadata_json={"custom": "value", "nested": {"a": 1}},
        )
        assert r.metadata_json["custom"] == "value"
        assert r.metadata_json["nested"]["a"] == 1


# ---------------------------------------------------------------------------
# ScopeRegistryStore ABC
# ---------------------------------------------------------------------------


class TestScopeRegistryStoreABC:
    def test_cannot_instantiate_abstract(self) -> None:
        with pytest.raises(TypeError):
            ScopeRegistryStore()

    def test_subclass_must_implement_all_four_methods(self) -> None:
        class Incomplete(ScopeRegistryStore):
            async def list_workspaces(
                self, order_by: str = "last_active", limit: int = 20
            ) -> list[WorkspaceRecord]:
                return []

        with pytest.raises(TypeError):
            Incomplete()

    def test_registry_store_is_deprecated_alias(self) -> None:
        assert RegistryStore is ScopeRegistryStore

    def test_full_implementation_instantiates(self) -> None:
        class Complete(ScopeRegistryStore):
            async def list_workspaces(
                self, order_by: str = "last_active", limit: int = 20
            ) -> list[WorkspaceRecord]:
                return []

            async def upsert_workspace(self, record: WorkspaceRecord) -> None:
                pass

            async def delete_workspace(self, target_path: str) -> None:
                pass

            async def get_workspace(self, target_path: str) -> WorkspaceRecord | None:
                return None

        store = Complete()
        assert isinstance(store, ScopeRegistryStore)


# ---------------------------------------------------------------------------
# GlobalWorkspaceStore — new ABC interface
# ---------------------------------------------------------------------------


class TestGlobalWorkspaceStoreNewInterface:
    async def test_upsert_and_get_workspace(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        record = _record(str(tmp_path / "proj-a"), display_name="Proj A")
        await store.upsert_workspace(record)
        got = await store.get_workspace(str(tmp_path / "proj-a"))
        assert got is not None
        assert got.workspace_id == record.workspace_id
        assert got.target_path == str((tmp_path / "proj-a").resolve())
        assert got.display_name == "Proj A"

    async def test_get_workspace_returns_none_when_absent(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        assert await store.get_workspace(str(tmp_path / "absent")) is None

    async def test_upsert_replaces_existing_by_target_path(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        target = str(tmp_path / "proj")
        r1 = _record(target, display_name="Old", last_active=1735689600000)
        await store.upsert_workspace(r1)
        r2 = _record(target, display_name="New", last_active=1748736000000)
        await store.upsert_workspace(r2)
        got = await store.get_workspace(target)
        assert got is not None
        assert got.display_name == "New"
        assert got.last_active == 1748736000000

    async def test_delete_workspace_removes_record(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        target = str(tmp_path / "proj")
        await store.upsert_workspace(_record(target))
        assert await store.get_workspace(target) is not None
        await store.delete_workspace(target)
        assert await store.get_workspace(target) is None

    async def test_delete_workspace_noop_when_absent(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        await store.delete_workspace(str(tmp_path / "nonexistent"))

    async def test_list_workspaces_empty_returns_empty(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        assert await store.list_workspaces() == []

    async def test_list_workspaces_orders_by_last_active_desc(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        await store.upsert_workspace(
            _record(str(tmp_path / "old"), last_active=1735689600000)
        )
        await store.upsert_workspace(
            _record(str(tmp_path / "new"), last_active=1748736000000)
        )
        await store.upsert_workspace(
            _record(str(tmp_path / "mid"), last_active=1740787200000)
        )
        records = await store.list_workspaces(order_by="last_active")
        assert len(records) == 3
        assert records[0].target_path == str((tmp_path / "new").resolve())
        assert records[1].target_path == str((tmp_path / "mid").resolve())
        assert records[2].target_path == str((tmp_path / "old").resolve())

    async def test_list_workspaces_orders_by_created_at_desc(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        await store.upsert_workspace(
            _record(
                str(tmp_path / "first"),
                created_at=1735689600000,
                last_active=1748736000000,
            )
        )
        await store.upsert_workspace(
            _record(
                str(tmp_path / "second"),
                created_at=1746057600000,
                last_active=1748736000000,
            )
        )
        records = await store.list_workspaces(order_by="created_at")
        assert len(records) == 2
        assert records[0].target_path == str((tmp_path / "second").resolve())
        assert records[1].target_path == str((tmp_path / "first").resolve())

    async def test_list_workspaces_respects_limit(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        for i in range(5):
            await store.upsert_workspace(
                _record(
                    str(tmp_path / f"ws{i}"),
                    last_active=1735689600000 + i * 86400000,
                )
            )
        records = await store.list_workspaces(order_by="last_active", limit=3)
        assert len(records) == 3

    async def test_list_workspaces_unknown_order_by_raises(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        await store.upsert_workspace(_record(str(tmp_path / "ws")))
        with pytest.raises(ValueError):
            await store.list_workspaces(order_by="unknown_field")

    async def test_list_workspaces_default_limit_20(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        for i in range(25):
            await store.upsert_workspace(
                _record(
                    str(tmp_path / f"ws{i}"),
                    last_active=1735689600000 + (i % 9) * 86400000,
                )
            )
        records = await store.list_workspaces()
        assert len(records) == 20

    async def test_persists_metadata_json_under_registry_dir(
        self, tmp_path: Path
    ) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        await store.upsert_workspace(
            _record(
                str(tmp_path / "proj"),
                display_name="My Project",
                metadata_json={"origin": "webui"},
            )
        )
        f = tmp_path / ".modex" / "_registry" / "workspaces.json"
        assert f.exists()
        data = json.loads(f.read_text(encoding="utf-8"))
        assert "workspaces" in data
        assert len(data["workspaces"]) == 1
        entry = data["workspaces"][0]
        assert entry["display_name"] == "My Project"
        assert entry["metadata_json"] == {"origin": "webui"}
        assert entry["is_home"] is False

    async def test_survives_reload(self, tmp_path: Path) -> None:
        s1 = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        await s1.upsert_workspace(
            _record(str(tmp_path / "a"), display_name="A"),
        )
        await s1.upsert_workspace(
            _record(str(tmp_path / "b"), display_name="B"),
        )
        s2 = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        records = await s2.list_workspaces()
        assert len(records) == 2
        names = {r.display_name for r in records}
        assert names == {"A", "B"}


class TestGlobalWorkspaceStoreLegacyCompat:
    """Legacy load_known_targets / save_known_targets route through new ABC."""

    async def test_load_known_targets_returns_non_home_paths(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        await store.upsert_workspace(_record(str(tmp_path / "a")))
        await store.upsert_workspace(_record(str(tmp_path / "b")))
        await store.upsert_workspace(
            _record(str(tmp_path), is_home=True)
        )
        targets = await store.load_known_targets()
        target_set = {t.resolve() for t in targets}
        assert target_set == {
            (tmp_path / "a").resolve(),
            (tmp_path / "b").resolve(),
        }

    async def test_load_known_targets_empty(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        assert await store.load_known_targets() == []

    async def test_save_known_targets_upserts_new(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        await store.save_known_targets([tmp_path / "a", tmp_path / "b"])
        records = await store.list_workspaces()
        assert len(records) == 2
        target_paths = {r.target_path for r in records}
        assert str((tmp_path / "a").resolve()) in target_paths
        assert str((tmp_path / "b").resolve()) in target_paths

    async def test_save_known_targets_deletes_removed(self, tmp_path: Path) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        await store.upsert_workspace(_record(str(tmp_path / "a")))
        await store.upsert_workspace(_record(str(tmp_path / "b")))
        # Save only "a" — "b" should be deleted
        await store.save_known_targets([tmp_path / "a"])
        records = await store.list_workspaces()
        assert len(records) == 1
        assert records[0].target_path == str((tmp_path / "a").resolve())

    async def test_save_known_targets_preserves_existing_metadata(
        self, tmp_path: Path
    ) -> None:
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        target = str(tmp_path / "a")
        await store.upsert_workspace(
            _record(target, display_name="Original", metadata_json={"k": "v"})
        )
        # Re-save with the same target — metadata should be preserved
        await store.save_known_targets([tmp_path / "a"])
        got = await store.get_workspace(target)
        assert got is not None
        assert got.display_name == "Original"
        assert got.metadata_json == {"k": "v"}


class TestGlobalWorkspaceStoreLegacyFormatMigration:
    """The store migrates the old ``{"targets": [...]}`` format on load."""

    async def test_loads_legacy_targets_format(self, tmp_path: Path) -> None:
        registry_dir = tmp_path / ".modex" / "_registry"
        registry_dir.mkdir(parents=True)
        legacy_file = registry_dir / "workspaces.json"
        legacy_file.write_text(
            json.dumps(
                {
                    "targets": [
                        str((tmp_path / "a").resolve()),
                        str((tmp_path / "b").resolve()),
                    ]
                }
            ),
            encoding="utf-8",
        )
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        records = await store.list_workspaces()
        assert len(records) == 2
        target_paths = {r.target_path for r in records}
        assert str((tmp_path / "a").resolve()) in target_paths
        assert str((tmp_path / "b").resolve()) in target_paths

    async def test_legacy_load_known_targets_on_legacy_format(
        self, tmp_path: Path
    ) -> None:
        registry_dir = tmp_path / ".modex" / "_registry"
        registry_dir.mkdir(parents=True)
        legacy_file = registry_dir / "workspaces.json"
        legacy_file.write_text(
            json.dumps({"targets": [str((tmp_path / "a").resolve())]}),
            encoding="utf-8",
        )
        store = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
        targets = await store.load_known_targets()
        assert len(targets) == 1
        assert targets[0].resolve() == (tmp_path / "a").resolve()

