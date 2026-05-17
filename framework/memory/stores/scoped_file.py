"""Scoped file storage."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from framework.memory.archive_models import (
    CONTEXT_ARCHIVE_FILENAME,
    KNOWLEDGE_ARCHIVE_FILENAME,
    ArchiveChannel,
)
from framework.memory.core.lock import AioRWLock, StorageLock
from framework.memory.core.models import StorageRevision
from framework.memory.core.scope import MemoryLayerName
from framework.memory.core.storage import MemoryStorage
from framework.memory.utils import safe_atomic_replace

_KV_FILE = "kv.json"
_MESSAGES_FILE = "messages.jsonl"
_ARCHIVE_STATE_FILE = ".archive_state.json"
_CHANGELOG_FILE = "changelog.jsonl"


class DefaultScopedStorage(MemoryStorage):
    """Local-file storage for one layer/scope directory.

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
        super().__init__(lock or AioRWLock())
        self.directory = Path(directory)
        self.layer = layer
        self._version = 0
        self._updated_at = datetime.now(UTC)

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
            if not self._kv_path.exists():
                return None
            try:
                data = json.loads(self._kv_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
            return data.get(key)

    async def set(self, key: str, value: Any) -> None:
        async with self.get_lock().write():
            data: dict[str, Any] = {}
            if self._kv_path.exists():
                with contextlib.suppress(json.JSONDecodeError):
                    data = json.loads(self._kv_path.read_text(encoding="utf-8"))
            data[key] = value
            self._atomic_json_write(self._kv_path, data)
            self._touch()

    async def delete(self, key: str) -> bool:
        async with self.get_lock().write():
            if not self._kv_path.exists():
                return False
            try:
                data = json.loads(self._kv_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return False
            if key not in data:
                return False
            del data[key]
            self._atomic_json_write(self._kv_path, data)
            self._touch()
            return True

    async def list_keys(self, prefix: str = "") -> list[str]:
        async with self.get_lock().read():
            if not self._kv_path.exists():
                return []
            try:
                data = json.loads(self._kv_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
            return [key for key in data if key.startswith(prefix)]

    async def load_messages(self) -> list[dict[str, Any]]:
        async with self.get_lock().read():
            if not self._messages_path.exists():
                return []
            messages: list[dict[str, Any]] = []
            with self._messages_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        messages.append(json.loads(line))
            return messages

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
        messages: list[dict[str, Any]] = []
        if self._messages_path.exists():
            with self._messages_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        messages.append(json.loads(line))
        return StorageRevision(
            message_count=len(messages),
            updated_at=self._updated_at,
            version=self._version,
        )

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
            cursor = self._get_last_cursor_unsafe(f"{channel}_archive") + 1
            stored = {
                **entry,
                "cursor": cursor,
                "entry_id": entry.get("entry_id") or entry.get("archive_id") or cursor,
                "created_at": entry.get("created_at") or datetime.now(UTC).isoformat(),
            }
            self.directory.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(stored, ensure_ascii=False) + "\n")
            self._set_last_cursor_unsafe(f"{channel}_archive", cursor)
            self._touch()
            return stored

    async def read_logs(self, since_cursor: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        async with self.get_lock().read():
            if not self._log_path.exists():
                return []
            entries: list[dict[str, Any]] = []
            with self._log_path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
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
            if not path.exists():
                return []
            entries: list[dict[str, Any]] = []
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
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
            if not self._archive_state_path.exists():
                return None
            try:
                data: dict[str, Any] = json.loads(
                    self._archive_state_path.read_text(encoding="utf-8")
                )
                return data
            except json.JSONDecodeError:
                return None

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
