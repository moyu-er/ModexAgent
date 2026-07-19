"""Global-tier persistence — registry of known workspaces (under _registry/).

The set of known workspaces lives in the GLOBAL tier (not under any workspace's
data root): ``<home>/<data_dir_name>/_registry/workspaces.json``.
``_registry`` is a reserved name (``framework.workspace.paths.RESERVED_GLOBAL_DIR``);
per-workspace accessors never produce it.

T14: stores structured :class:`WorkspaceRecord` metadata in JSON (not a bare
path list).  Loads the legacy ``{"targets": [...]}`` format transparently and
migrates to the new format on next write.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from modex_agent.utils.time import now_ms
from modex_agent.workspace.paths import RESERVED_GLOBAL_DIR
from modex_agent.workspace.record import WorkspaceRecord
from modex_agent.workspace.registry import WorkspaceRegistryStore

_VALID_ORDER_BY: frozenset[str] = frozenset({"last_active", "created_at"})


class GlobalWorkspaceStore(WorkspaceRegistryStore):
    """File-backed WorkspaceRegistryStore: known workspaces on disk."""

    def __init__(self, *, home: Path, data_dir_name: str) -> None:
        self._dir: Path = Path(home).resolve() / data_dir_name / RESERVED_GLOBAL_DIR
        self._file: Path = self._dir / "workspaces.json"

    async def list_workspaces(
        self, order_by: str = "last_active", limit: int = 20
    ) -> list[WorkspaceRecord]:
        if order_by not in _VALID_ORDER_BY:
            raise ValueError(
                f"Unknown order_by {order_by!r}; expected one of {sorted(_VALID_ORDER_BY)}"
            )
        records = self._read_all()
        records.sort(key=lambda r: getattr(r, order_by), reverse=True)
        return records[:limit]

    async def upsert_workspace(self, record: WorkspaceRecord) -> None:
        records = self._read_all()
        key = str(Path(record.target_path).resolve())
        records = [r for r in records if str(Path(r.target_path).resolve()) != key]
        records.append(record.model_copy(update={"target_path": key}))
        self._write_all(records)

    async def delete_workspace(self, target_path: str) -> None:
        records = self._read_all()
        key = str(Path(target_path).resolve())
        filtered = [r for r in records if str(Path(r.target_path).resolve()) != key]
        if len(filtered) != len(records):
            self._write_all(filtered)

    async def get_workspace(self, target_path: str) -> WorkspaceRecord | None:
        key = str(Path(target_path).resolve())
        for record in self._read_all():
            if str(Path(record.target_path).resolve()) == key:
                return record
        return None

    def _read_all(self) -> list[WorkspaceRecord]:
        if not self._file.exists():
            return []
        data = json.loads(self._file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "targets" in data:
            return self._migrate_legacy(data)
        workspaces = data.get("workspaces", []) if isinstance(data, dict) else []
        return [WorkspaceRecord.model_validate(entry) for entry in workspaces]

    def _write_all(self, records: list[WorkspaceRecord]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "workspaces": [r.model_dump(mode="json") for r in records],
        }
        self._file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _migrate_legacy(data: dict[str, Any]) -> list[WorkspaceRecord]:
        targets = data.get("targets", [])
        now = now_ms()
        return [
            WorkspaceRecord(
                workspace_id=str(uuid.uuid4()),
                target_path=str(Path(target).resolve()),
                display_name=None,
                created_at=now,
                last_active=now,
                is_home=False,
                metadata_json={},
            )
            for target in targets
            if isinstance(target, str)
        ]
