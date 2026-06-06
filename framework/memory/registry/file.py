"""Default local-file memory store registry."""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Collection
from pathlib import Path

from framework.memory.archive_models import (
    CONTEXT_ARCHIVE_FILE_KEY,
    CONTEXT_ARCHIVE_FILENAME,
    KNOWLEDGE_ARCHIVE_FILE_KEY,
    KNOWLEDGE_ARCHIVE_FILENAME,
)
from framework.memory.core.scope import (
    MemoryAgentRole,
    MemoryContext,
    MemoryLayerName,
    MemoryScope,
    ScopeRecord,
    infer_agent_role,
)
from framework.memory.core.storage import MemoryStorage
from framework.memory.registry.base import MemoryStoreRegistry
from framework.memory.stores.scoped_file import DefaultScopedStorage
from framework.memory.stores.utils import sanitize_scope_key
from framework.utils.file_io import read_json_robust

_SCOPE_FILE = ".scope.json"


class DefaultMemoryStoreRegistry(MemoryStoreRegistry):
    """Default registry backed by local layer-first file storage."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._stores: dict[tuple[MemoryLayerName, str], MemoryStorage] = {}

    async def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        for storage in self._stores.values():
            await storage.close()

    def _scope_dir(self, layer: MemoryLayerName, scope_key: str) -> Path:
        return self.root / str(layer) / sanitize_scope_key(scope_key)

    def _metadata_path(self, layer: MemoryLayerName, scope_key: str) -> Path:
        return self._scope_dir(layer, scope_key) / _SCOPE_FILE

    def _write_scope_metadata(
        self,
        *,
        layer: MemoryLayerName,
        scope_key: str,
        context: MemoryContext,
        storage_path: Path,
    ) -> None:
        path = self._metadata_path(layer, scope_key)
        now = time.time()
        created_at = now
        if path.exists():
            existing = self._read_scope_record(path.parent)
            if existing and existing.created_at is not None:
                created_at = existing.created_at
        data = {
            "scope_key": scope_key,
            "layer": str(layer),
            "context": context.to_dict(),
            "storage_path": str(storage_path),
            "agent_role": str(infer_agent_role(context)),
            "agent_id": context.agent_id,
            "created_at": created_at,
            "updated_at": now,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.replace(str(tmp_path), str(path))
        except OSError:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            with contextlib.suppress(OSError):
                tmp_path.unlink()

    def _read_scope_record(self, scope_dir: Path) -> ScopeRecord | None:
        path = scope_dir / _SCOPE_FILE
        data = read_json_robust(path)
        if not data:
            return None
        try:
            return ScopeRecord(
                scope_key=data["scope_key"],
                layer=MemoryLayerName(data["layer"]),
                context=MemoryContext.from_dict(data.get("context")),
                storage_path=data.get("storage_path") or str(scope_dir),
                agent_role=data.get("agent_role", MemoryAgentRole.MAIN),
                agent_id=data.get("agent_id"),
                created_at=data.get("created_at"),
                updated_at=data.get("updated_at"),
            )
        except Exception:
            return None

    async def resolve(
        self,
        *,
        layer: MemoryLayerName,
        scope: MemoryScope,
        context: MemoryContext,
    ) -> MemoryStorage:
        await self.initialize()
        scope_key = scope.get_scope_key(context)
        cache_key = (layer, scope_key)
        storage = self._stores.get(cache_key)
        if storage is None:
            scope_dir = self._scope_dir(layer, scope_key)
            if layer == MemoryLayerName.KNOWLEDGE:
                from framework.memory.stores.markdown_knowledge import MarkdownKnowledgeStorage
                storage = MarkdownKnowledgeStorage(scope_dir, layer=layer)
            elif layer == MemoryLayerName.ARCHIVE:
                from framework.memory.stores.dir_archive import DirArchiveStorage
                storage: MemoryStorage = DirArchiveStorage(scope_dir)  # type: ignore[assignment]
            else:
                storage = DefaultScopedStorage(scope_dir, layer=layer)
            await storage.initialize()
            self._stores[cache_key] = storage
        self._write_scope_metadata(
            layer=layer,
            scope_key=scope_key,
            context=context,
            storage_path=storage.directory,
        )
        return storage

    async def list_records(
        self,
        *,
        layer: MemoryLayerName | None = None,
        agent_roles: Collection[str | MemoryAgentRole] | None = frozenset(
            {MemoryAgentRole.MAIN}
        ),
        has_file: str | None = None,
    ) -> list[ScopeRecord]:
        if not self.root.exists():
            return []
        allowed_roles = {str(role) for role in agent_roles} if agent_roles is not None else None
        layer_dirs = [self.root / str(layer)] if layer is not None else [
            path for path in self.root.iterdir() if path.is_dir()
        ]
        records: list[ScopeRecord] = []
        for layer_dir in layer_dirs:
            if not layer_dir.exists():
                continue
            for scope_dir in layer_dir.iterdir():
                if not scope_dir.is_dir():
                    continue
                record = self._read_scope_record(scope_dir)
                if record is None:
                    continue
                if layer is not None and record.layer != layer:
                    continue
                if allowed_roles is not None and str(record.agent_role) not in allowed_roles:
                    continue
                if has_file is not None and not self._has_file(scope_dir, has_file):
                    continue
                records.append(record)
        return records

    def _has_file(self, scope_dir: Path, has_file: str) -> bool:
        file_map = {
            CONTEXT_ARCHIVE_FILE_KEY: CONTEXT_ARCHIVE_FILENAME,
            KNOWLEDGE_ARCHIVE_FILE_KEY: KNOWLEDGE_ARCHIVE_FILENAME,
            "messages": "messages.jsonl",
            "history": CONTEXT_ARCHIVE_FILENAME,
            "archive": CONTEXT_ARCHIVE_FILENAME,
            "logs": CONTEXT_ARCHIVE_FILENAME,
            "kv": "kv.json",
        }
        return (scope_dir / file_map.get(has_file, has_file)).exists()

    async def evict(
        self,
        *,
        layer: MemoryLayerName | None = None,
        scope: MemoryScope | None = None,
    ) -> None:
        _ = scope
        keys = [
            key
            for key in self._stores
            if layer is None or key[0] == layer
        ]
        for key in keys:
            await self._stores[key].close()
            self._stores.pop(key, None)
