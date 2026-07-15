"""Scoped file storage."""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modex_agent.core.scope import MemoryLayerName
from modex_agent.memory.archive_models import (
    CONTEXT_ARCHIVE_FILENAME,
    KNOWLEDGE_ARCHIVE_FILENAME,
    ArchiveChannel,
)
from modex_agent.memory.core.lock import AioRWLock, StorageLock
from modex_agent.memory.core.models import StorageRevision
from modex_agent.memory.core.split_stores import (
    ArchiveStore,
    CursorStore,
    KVStore,
    MessageStore,
)
from modex_agent.memory.core.store_metadata import StoreMetadata
from modex_agent.memory.utils import safe_atomic_replace
from modex_agent.utils.file_io import read_json_robust, read_jsonl_robust

logger = logging.getLogger(__name__)

_KV_FILE = "kv.json"
_MESSAGES_FILE = "messages.jsonl"
_ARCHIVE_STATE_FILE = ".archive_state.json"
_CHANGELOG_FILE = "changelog.jsonl"


class DefaultScopedStorage(StoreMetadata, MessageStore, KVStore, CursorStore, ArchiveStore):
    """Local-file storage for one layer/scope directory.

    Implements all four split store ABCs (``MessageStore`` / ``KVStore`` /
    ``CursorStore`` / ``ArchiveStore``) plus :class:`StoreMetadata` — one
    concrete class, four focused data-access interfaces plus physical
    metadata, wired as a single instance by ``MemoryStoreBundle``.

    File layout in ``directory``:
    - ``messages.jsonl`` – conversation history; **this is the only data**
      injected into LLM context via ``history.to_messages()``.
    - ``kv.json`` – lightweight key-value metadata **not** fed to the LLM.
      Used for internal bookkeeping (``.last_activity``, ``.last_write_id``,
      ``.checkpoint``, etc.). It is intentionally separate from
      ``messages.jsonl`` so growing tool-call content does **not** bloat
      the context or require full-file rewrites of conversation state.
    - ``context_archive.jsonl`` / ``knowledge_archive.jsonl`` – archive channel logs.\n    - ``changelog.jsonl`` – layer-specific changelog.
    - ``.cursor_*`` – cursor tracking files.
    """

    def __init__(
        self,
        directory: Path,
        *,
        layer: MemoryLayerName,
        lock: StorageLock | None = None,
    ) -> None:
        self._lock = lock or AioRWLock()
        self.directory = Path(directory)
        self.layer = layer
        self._version = 0
        self._updated_at = datetime.now(UTC)

    def get_lock(self) -> StorageLock:
        """Return the shared read/write lock for this store instance."""
        return self._lock

    @property
    def base_path(self) -> Path | None:
        return self.directory.resolve()

    async def initialize(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        for tmp_file in self.directory.glob("*.tmp"):
            with contextlib.suppress(Exception):
                tmp_file.unlink()

    async def close(self) -> None:
        pass

    def _touch(self) -> None:
        self._version += 1
        self._updated_at = datetime.now(UTC)

    @property
    def _kv_path(self) -> Path:
        return self.directory / _KV_FILE

    @property
    def _messages_path(self) -> Path:
        return self.directory / _MESSAGES_FILE

    @property
    def _log_path(self) -> Path:
        if self.layer == MemoryLayerName.KNOWLEDGE:
            return self.directory / _CHANGELOG_FILE
        if self.layer == MemoryLayerName.ARCHIVE:
            return self.directory / CONTEXT_ARCHIVE_FILENAME
        return self.directory / _CHANGELOG_FILE

    def _channel_log_path(self, channel: str) -> Path:
        if channel == ArchiveChannel.CONTEXT.value:
            return self.directory / CONTEXT_ARCHIVE_FILENAME
        if channel == ArchiveChannel.KNOWLEDGE.value:
            return self.directory / KNOWLEDGE_ARCHIVE_FILENAME
        return self._log_path

    @property
    def _archive_state_path(self) -> Path:
        return self.directory / _ARCHIVE_STATE_FILE

    def _cursor_path(self, cursor_name: str) -> Path:
        return self.directory / f".cursor_{cursor_name}"

    def _atomic_json_write(self, path: Path, data: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        safe_atomic_replace(tmp_path, path)

    async def get(self, key: str) -> Any | None:
        async with self.get_lock().read():
            data = read_json_robust(self._kv_path)
            return data.get(key) if data else None

    async def set(self, key: str, value: Any) -> None:
        async with self.get_lock().write():
            data = read_json_robust(self._kv_path) or {}
            data[key] = value
            self._atomic_json_write(self._kv_path, data)
            self._touch()

    async def delete(self, key: str) -> bool:
        async with self.get_lock().write():
            data = read_json_robust(self._kv_path)
            if not data or key not in data:
                return False
            del data[key]
            self._atomic_json_write(self._kv_path, data)
            self._touch()
            return True

    async def list_keys(self, prefix: str = "") -> list[str]:
        async with self.get_lock().read():
            data = read_json_robust(self._kv_path)
            if not data:
                return []
            return [key for key in data if key.startswith(prefix)]

    async def load_messages(self) -> list[dict[str, Any]]:
        async with self.get_lock().read():
            return read_jsonl_robust(self._messages_path)

    async def load_all_messages(self) -> list[dict[str, Any]]:
        async with self.get_lock().read():
            return read_jsonl_robust(self._messages_path)

    async def save_messages(self, messages: list[dict[str, Any]]) -> StorageRevision:
        async with self.get_lock().write():
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp_path = self._messages_path.with_suffix(self._messages_path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                for message in messages:
                    handle.write(json.dumps(message, ensure_ascii=False) + "\n")
            safe_atomic_replace(tmp_path, self._messages_path)
            self._touch()
            return self._get_revision_unsafe()

    async def append_message(self, message: dict[str, Any]) -> StorageRevision:
        async with self.get_lock().write():
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._messages_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._touch()
            return self._get_revision_unsafe()

    async def get_revision(self) -> StorageRevision:
        async with self.get_lock().read():
            return self._get_revision_unsafe()

    def _get_revision_unsafe(self) -> StorageRevision:
        messages = read_jsonl_robust(self._messages_path)
        return StorageRevision(
            message_count=len(messages),
            updated_at=self._updated_at,
            version=self._version,
        )

    def _write_messages_unsafe(self, messages: list[dict[str, Any]]) -> None:
        """Atomically rewrite messages.jsonl without acquiring the lock.

        Callers must already hold ``get_lock().write()``.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp_path = self._messages_path.with_suffix(self._messages_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for message in messages:
                handle.write(json.dumps(message, ensure_ascii=False) + "\n")
        safe_atomic_replace(tmp_path, self._messages_path)
        self._touch()

    @staticmethod
    def _matches_message_id(message: dict[str, Any], message_id: str) -> bool:
        return message.get("id") == message_id or message.get("message_id") == message_id

    async def prune_messages(self, max_messages: int) -> tuple[int, list[dict[str, Any]]]:
        """Trim history to the newest ``max_messages``; return ``(pruned_count, pruned)``.

        Pinned messages (``_pinned: true``) always survive pruning even when
        they fall outside the ``max_messages`` window.
        """
        async with self.get_lock().write():
            messages = read_jsonl_robust(self._messages_path)
            if len(messages) <= max_messages:
                return 0, []
            keep_idx: set[int] = (
                set(range(len(messages))[-max_messages:]) if max_messages > 0 else set()
            )
            for i, message in enumerate(messages):
                if message.get("_pinned"):
                    keep_idx.add(i)
            pruned = [messages[i] for i in range(len(messages)) if i not in keep_idx]
            if not pruned:
                return 0, []
            keep = [messages[i] for i in range(len(messages)) if i in keep_idx]
            self._write_messages_unsafe(keep)
            return len(pruned), pruned

    async def pin_message(self, message_id: str) -> None:
        """Mark a message as pinned so it survives pruning."""
        async with self.get_lock().write():
            messages = read_jsonl_robust(self._messages_path)
            changed = False
            for message in messages:
                if self._matches_message_id(message, message_id):
                    message["_pinned"] = True
                    changed = True
            if changed:
                self._write_messages_unsafe(messages)

    async def unpin_message(self, message_id: str) -> None:
        """Remove the pin from a previously pinned message."""
        async with self.get_lock().write():
            messages = read_jsonl_robust(self._messages_path)
            changed = False
            for message in messages:
                if self._matches_message_id(message, message_id) and "_pinned" in message:
                    message.pop("_pinned", None)
                    changed = True
            if changed:
                self._write_messages_unsafe(messages)

    async def delete_message(self, message_id: str) -> bool:
        """Delete a single message by id; return whether it existed."""
        async with self.get_lock().write():
            messages = read_jsonl_robust(self._messages_path)
            remaining = [m for m in messages if not self._matches_message_id(m, message_id)]
            if len(remaining) == len(messages):
                return False
            self._write_messages_unsafe(remaining)
            return True

    async def cleanup_expired(self) -> int:
        return 0

    async def retain_messages(
        self,
        keep_messages: list[dict[str, Any]],
        expected_revision: StorageRevision | None = None,
    ) -> StorageRevision | None:
        async with self.get_lock().write():
            if expected_revision is not None:
                current = self._get_revision_unsafe()
                if (
                    current.message_count != expected_revision.message_count
                    or current.version != expected_revision.version
                ):
                    return None
            self._write_messages_unsafe(keep_messages)
            return self._get_revision_unsafe()

    async def append_log(self, entry: dict[str, Any]) -> dict[str, Any]:
        async with self.get_lock().write():
            cursor = self._get_last_cursor_unsafe("default") + 1
            stored = {
                **entry,
                "cursor": cursor,
                "entry_id": entry.get("entry_id") or cursor,
                "created_at": entry.get("created_at") or datetime.now(UTC).isoformat(),
            }
            self.directory.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(stored, ensure_ascii=False) + "\n")
            self._set_last_cursor_unsafe("default", cursor)
            self._touch()
            return stored

    async def append_channel_log(self, channel: str, entry: dict[str, Any]) -> dict[str, Any]:
        async with self.get_lock().write():
            path = self._channel_log_path(channel)
            archive_id = int(entry.get("archive_id", 0) or 0)
            stored = {
                **entry,
                "cursor": archive_id,
                "entry_id": entry.get("entry_id") or archive_id,
                "created_at": entry.get("created_at") or datetime.now(UTC).isoformat(),
            }
            self.directory.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(stored, ensure_ascii=False) + "\n")
            self._set_last_cursor_unsafe("archive", archive_id)
            self._touch()
            return stored

    async def read_logs(self, since_cursor: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        async with self.get_lock().read():
            all_entries = read_jsonl_robust(self._log_path)
            entries: list[dict[str, Any]] = []
            for entry in all_entries:
                if entry.get("cursor", 0) > since_cursor:
                    entries.append(entry)
                    if len(entries) >= limit:
                        break
            return entries

    async def read_channel_logs(
        self,
        channel: str,
        since_archive_id: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        async with self.get_lock().read():
            path = self._channel_log_path(channel)
            all_entries = read_jsonl_robust(path)
            entries: list[dict[str, Any]] = []
            for entry in all_entries:
                if int(entry.get("archive_id", 0) or 0) > since_archive_id:
                    entries.append(entry)
                    if limit and len(entries) >= limit:
                        break
            return entries

    async def save_logs(self, entries: list[dict[str, Any]]) -> None:
        async with self.get_lock().write():
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp_path = self._log_path.with_suffix(self._log_path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                for entry in entries:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            safe_atomic_replace(tmp_path, self._log_path)
            if entries:
                self._set_last_cursor_unsafe(
                    "default", max(int(entry.get("cursor", 0)) for entry in entries)
                )
            self._touch()

    async def save_channel_logs(self, channel: str, entries: list[dict[str, Any]]) -> None:
        async with self.get_lock().write():
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self._channel_log_path(channel)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                for entry in entries:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            safe_atomic_replace(tmp_path, path)
            self._touch()

    async def read_archive_state(self) -> dict[str, Any] | None:
        async with self.get_lock().read():
            return read_json_robust(self._archive_state_path)

    async def write_archive_state(self, state: dict[str, Any]) -> None:
        async with self.get_lock().write():
            self._atomic_json_write(self._archive_state_path, state)
            self._touch()

    async def get_last_cursor(self, cursor_name: str = "default") -> int:
        async with self.get_lock().read():
            return self._get_last_cursor_unsafe(cursor_name)

    async def set_last_cursor(self, cursor_name: str, cursor: int) -> None:
        async with self.get_lock().write():
            self._set_last_cursor_unsafe(cursor_name, cursor)
            self._touch()

    def _get_last_cursor_unsafe(self, cursor_name: str = "default") -> int:
        path = self._cursor_path(cursor_name)
        if not path.exists():
            return 0
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except ValueError:
            return 0

    def _set_last_cursor_unsafe(self, cursor_name: str, cursor: int) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._cursor_path(cursor_name)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(str(cursor), encoding="utf-8")
        safe_atomic_replace(tmp_path, path)

    async def prune_to_max(self, max_entries: int) -> int:
        """No-op for scoped file storage — pruning is handled per-layer."""
        _ = max_entries
        return 0

    async def cleanup_empty_dirs(self) -> int:
        """No-op for scoped file storage — no archive subdirectories."""
        return 0
