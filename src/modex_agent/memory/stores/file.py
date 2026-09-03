"""File-based storage backend for cross-platform persistence."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Collection
from pathlib import Path
from typing import Any

from modex_agent.core.scope import MemoryAgentRole, MemoryContext, ScopeRecord
from modex_agent.memory.core.lock import AioRWLock, StorageLock
from modex_agent.memory.core.models import StorageRevision
from modex_agent.memory.stores.utils import ensure_scope_dir
from modex_agent.utils.file_io import read_json_robust, read_jsonl_robust, safe_atomic_replace
from modex_agent.utils.time import now_ms

logger = logging.getLogger(__name__)


def _atomic_json_write(path: Path, data: dict[str, Any]) -> None:
    """原子写入 JSON 文件（使用临时文件替换）"""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    safe_atomic_replace(tmp_path, path)


_MESSAGES_FILE = "messages.jsonl"
_HISTORY_FILE = "history.jsonl"
_CHANGELOG_FILE = "changelog.jsonl"
_KV_FILE = "kv.json"
_SCOPE_FILE = ".scope.json"


class FileStorage:
    """基于文件的跨平台存储后端。

    目录结构:
        workspace/
        └── <sanitized_scope_key>/
            ├── messages.jsonl
            ├── history.jsonl
            ├── kv.json
            └── .cursor_default
            └── .cursor_dream
    """

    def __init__(self, workspace: Path, lock: StorageLock | None = None) -> None:
        # Default to AioRWLock for single-process use.
        # Pass FileStorageLock explicitly for cross-process safety.
        ws = Path(workspace)
        if lock is None:
            lock = AioRWLock()
        self._lock = lock
        self.workspace = ws

    def get_lock(self, lock_key: str | None = None) -> StorageLock:
        _ = lock_key
        return self._lock

    async def initialize(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        for tmp_file in self.workspace.rglob("*.tmp"):
            with contextlib.suppress(Exception):
                tmp_file.unlink()
                logger.debug("Cleaned up residual tmp file: %s", tmp_file)

    def list_scopes(self) -> list[str]:
        """返回 workspace 下所有已存在的 scope 目录名列表。"""
        if not self.workspace.exists():
            return []
        return [d.name for d in self.workspace.iterdir() if d.is_dir()]

    async def close(self) -> None:
        pass

    async def get_revision(self, scope_key: str = "default") -> StorageRevision:
        messages = await self.load_messages(scope_key)
        return StorageRevision(
            message_count=len(messages),
            updated_at=now_ms(),
            version=await self.get_last_cursor(scope_key, "default"),
        )

    def _scope_dir(self, scope_key: str) -> Path:
        return ensure_scope_dir(self.workspace, scope_key)

    def _messages_path(self, scope_dir: Path) -> Path:
        return scope_dir / _MESSAGES_FILE

    def _history_path(self, scope_dir: Path) -> Path:
        return scope_dir / _HISTORY_FILE

    def _changelog_path(self, scope_dir: Path) -> Path:
        return scope_dir / _CHANGELOG_FILE

    def _kv_path(self, scope_dir: Path) -> Path:
        return scope_dir / _KV_FILE

    def _cursor_path(self, scope_dir: Path, cursor_name: str) -> Path:
        return scope_dir / f".cursor_{cursor_name}"

    def _scope_metadata_path(self, scope_dir: Path) -> Path:
        return scope_dir / _SCOPE_FILE

    def _has_data_file(self, scope_dir: Path, has_file: str | None) -> bool:
        if has_file is None:
            return True
        file_map = {
            "messages": _MESSAGES_FILE,
            "history": _HISTORY_FILE,
            "kv": _KV_FILE,
        }
        filename = file_map.get(has_file, has_file)
        return (scope_dir / filename).exists()

    def _read_scope_record(self, scope_dir: Path) -> ScopeRecord | None:
        path = self._scope_metadata_path(scope_dir)
        data = read_json_robust(path)
        if not data:
            return None
        try:
            return ScopeRecord(
                scope_key=data["scope_key"],
                layer=data["layer"],
                context=MemoryContext.from_dict(data.get("context")),
                storage_path=data.get("storage_path") or str(scope_dir),
                agent_role=data.get("agent_role", "main"),
                agent_id=data.get("agent_id"),
                created_at=data.get("created_at"),
                updated_at=data.get("updated_at"),
            )
        except Exception as e:
            logger.warning("Failed to read scope metadata from %s: %s", path, e)
            return None

    async def ensure_scope_metadata(
        self,
        scope_key: str,
        *,
        layer: str,
        context: MemoryContext,
        agent_role: str | MemoryAgentRole = MemoryAgentRole.MAIN,
        agent_id: str | None = None,
    ) -> None:
        async with self.get_lock(scope_key).write():
            scope_dir = self._scope_dir(scope_key)
            path = self._scope_metadata_path(scope_dir)
            now = now_ms()
            created_at: float = now
            if path.exists():
                existing = self._read_scope_record(scope_dir)
                if existing and existing.created_at is not None:
                    created_at = existing.created_at
            data = {
                "scope_key": scope_key,
                "layer": layer,
                "context": context.to_dict(),
                "storage_path": str(scope_dir),
                "agent_role": str(agent_role),
                "agent_id": agent_id,
                "created_at": created_at,
                "updated_at": now,
            }
            _atomic_json_write(path, data)

    async def list_scope_records(
        self,
        *,
        layer: str | None = None,
        has_file: str | None = None,
        agent_roles: Collection[str | MemoryAgentRole] | None = frozenset({MemoryAgentRole.MAIN}),
    ) -> list[ScopeRecord]:
        if not self.workspace.exists():
            return []
        records: list[ScopeRecord] = []
        allowed_roles = {str(role) for role in agent_roles} if agent_roles is not None else None
        # Collect directory list outside the lock; only protect individual reads
        scope_dirs = [d for d in self.workspace.iterdir() if d.is_dir()]
        for scope_dir in scope_dirs:
            record = self._read_scope_record(scope_dir)
            if record is None:
                # Log warning for legacy directories without .scope.json
                logger.warning(
                    "Scope directory %s has no .scope.json metadata; "
                    "it may contain legacy data that is not accessible. "
                    "Consider running a migration tool.",
                    scope_dir.name,
                )
                continue
            if layer is not None and record.layer != layer:
                continue
            if allowed_roles is not None and str(record.agent_role) not in allowed_roles:
                continue
            if not self._has_data_file(scope_dir, has_file):
                continue
            records.append(record)
        return records

    # --- KV ---
    async def get(self, scope_key: str, key: str) -> Any | None:
        async with self.get_lock().read():
            scope_dir = self._scope_dir(scope_key)
            data = read_json_robust(self._kv_path(scope_dir))
            return data.get(key) if data else None

    async def set(self, scope_key: str, key: str, value: Any) -> None:
        async with self.get_lock().write():
            scope_dir = self._scope_dir(scope_key)
            kv_path = self._kv_path(scope_dir)
            scope_dir.mkdir(parents=True, exist_ok=True)
            data = read_json_robust(kv_path) or {}
            data[key] = value
            _atomic_json_write(kv_path, data)

    async def delete(self, scope_key: str, key: str) -> bool:
        async with self.get_lock().write():
            scope_dir = self._scope_dir(scope_key)
            kv_path = self._kv_path(scope_dir)
            data = read_json_robust(kv_path)
            if not data or key not in data:
                return False
            data.pop(key)
            _atomic_json_write(kv_path, data)
            return True

    async def list_keys(self, scope_key: str, prefix: str = "") -> list[str]:
        async with self.get_lock().read():
            scope_dir = self._scope_dir(scope_key)
            data = read_json_robust(self._kv_path(scope_dir))
            if not data:
                return []
            return [k for k in data if k.startswith(prefix)]

    # --- Messages ---
    async def load_messages(self, scope_key: str) -> list[dict[str, Any]]:
        async with self.get_lock().read():
            scope_dir = self._scope_dir(scope_key)
            return read_jsonl_robust(self._messages_path(scope_dir))

    async def save_messages(self, scope_key: str, messages: list[dict[str, Any]]) -> None:
        """原子覆盖写入 messages.jsonl（使用临时文件 + replace）"""
        async with self.get_lock().write():
            scope_dir = self._scope_dir(scope_key)
            path = self._messages_path(scope_dir)
            scope_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    for msg in messages:
                        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                safe_atomic_replace(tmp_path, path)
            except Exception as e:
                logger.error(f"Failed to save messages for {scope_key}: {e}")
                if tmp_path.exists():
                    with contextlib.suppress(Exception):
                        tmp_path.unlink()
                raise

    async def append_message(self, scope_key: str, message: dict[str, Any]) -> None:
        """追加写入 messages.jsonl"""
        async with self.get_lock().write():
            scope_dir = self._scope_dir(scope_key)
            path = self._messages_path(scope_dir)
            scope_dir.mkdir(parents=True, exist_ok=True)
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(message, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"Failed to append message for {scope_key}: {e}")
                raise

    # --- Logs ---
    async def append_log(self, scope_key: str, entry: dict[str, Any]) -> int:
        async with self.get_lock().write():
            scope_dir = self._scope_dir(scope_key)
            path = self._history_path(scope_dir)
            cursor = await self.get_last_cursor(scope_key, "default") + 1
            entry = {**entry, "cursor": cursor}

            scope_dir.mkdir(parents=True, exist_ok=True)
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"Failed to append log for {scope_key}: {e}")
                raise
            await self.set_last_cursor(scope_key, "default", cursor)
            return cursor

    async def append_changelog(self, scope_key: str, entry: dict[str, Any]) -> int:
        """Append a changelog entry to a separate file from history archive."""
        async with self.get_lock().write():
            scope_dir = self._scope_dir(scope_key)
            path = self._changelog_path(scope_dir)
            cursor = await self.get_last_cursor(scope_key, "changelog") + 1
            entry = {**entry, "cursor": cursor}

            scope_dir.mkdir(parents=True, exist_ok=True)
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"Failed to append changelog for {scope_key}: {e}")
                raise
            await self.set_last_cursor(scope_key, "changelog", cursor)
            return cursor

    async def read_logs(
        self,
        scope_key: str,
        since_cursor: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        async with self.get_lock().read():
            scope_dir = self._scope_dir(scope_key)
            all_entries = read_jsonl_robust(self._history_path(scope_dir))
            logs: list[dict[str, Any]] = []
            for entry in all_entries:
                if entry.get("cursor", 0) > since_cursor:
                    logs.append(entry)
                    if len(logs) >= limit:
                        break
            return logs

    async def save_logs(self, scope_key: str, entries: list[dict[str, Any]]) -> None:
        async with self.get_lock().write():
            scope_dir = self._scope_dir(scope_key)
            path = self._history_path(scope_dir)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    for entry in entries:
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                safe_atomic_replace(tmp_path, path)
            except Exception as e:
                logger.error(f"Failed to save logs for {scope_key}: {e}")
                if tmp_path.exists():
                    with contextlib.suppress(Exception):
                        tmp_path.unlink()
                raise

    async def get_last_cursor(self, scope_key: str, cursor_name: str = "default") -> int:
        """获取最后 cursor。若 cursor 文件丢失/损坏，从历史日志末尾恢复。"""
        async with self.get_lock().read():
            scope_dir = self._scope_dir(scope_key)
            path = self._cursor_path(scope_dir, cursor_name)
            cursor_val = 0
            if path.exists():
                try:
                    cursor_val = int(path.read_text(encoding="utf-8").strip())
                except Exception:
                    cursor_val = 0

            # 若 cursor 文件缺失或损坏，尝试从 history.jsonl 最后一行恢复
            if cursor_val == 0 and cursor_name == "default":
                entries = read_jsonl_robust(self._history_path(scope_dir))
                if entries:
                    cursor_val = entries[-1].get("cursor", 0)
            return cursor_val

    async def set_last_cursor(self, scope_key: str, cursor_name: str, cursor: int) -> None:
        """原子写入 cursor 文件（临时文件 + replace），避免崩溃时产生空文件或半写文件。"""
        async with self.get_lock().write():
            scope_dir = self._scope_dir(scope_key)
            path = self._cursor_path(scope_dir, cursor_name)
            scope_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(str(cursor), encoding="utf-8")
            safe_atomic_replace(tmp_path, path)
