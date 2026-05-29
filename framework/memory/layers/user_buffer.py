"""User retention buffer layer — storage-backed lifecycle for pruned user context."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any

from framework.memory.core.scope import MemoryContext, MemoryScope, SessionScope
from framework.memory.layers.config import StorageFactory
from framework.memory.user_buffer import UserBufferEntry


class UserRetentionBuffer(ABC):
    """Preserve pruned user/agent messages and track completion by plain assistant responses.

    ``mark_all_completed`` and ``upsert_pruned_user`` operate on the
    currently-active scope set via the hook lifecycle.  ``get_entries`` and
    ``clear`` accept an explicit ``MemoryContext`` for injection and reset
    call-sites.
    """

    @abstractmethod
    def mark_all_completed(self, assistant_content: str) -> None:
        """Mark every unfinished entry as completed with *assistant_content*.

        Called synchronously (no I/O needed — the buffer operates in-memory
        after the hook has loaded entries for the active scope).
        """
        ...

    @abstractmethod
    def upsert_pruned_user(self, entry: UserBufferEntry) -> None:
        """Dedup by fingerprint then append; FIFO-evict oldest when over limit.

        Called synchronously (no I/O needed — the buffer operates in-memory
        after the hook has loaded entries for the active scope).
        """
        ...

    @abstractmethod
    async def get_entries(self, context: MemoryContext) -> list[UserBufferEntry]:
        """Return all entries for *context* (used by governance injection)."""
        ...

    @abstractmethod
    async def clear(self, context: MemoryContext) -> None:
        """Remove all entries for *context*."""
        ...


@dataclass(frozen=True)
class UserRetentionBufferConfig:
    """Configuration for the UserRetentionBuffer layer."""

    enabled: bool = True
    max_entries: int = 5
    max_user_chars: int = 4000
    max_assistant_chars: int = 4000
    scope: MemoryScope = field(default_factory=SessionScope)


class ScopedUserRetentionBuffer(UserRetentionBuffer):
    """Storage-backed ``UserRetentionBuffer`` resolved through a ``StorageFactory``.

    Entries are persisted under the key ``".user_retention_entries"`` in the
    scoped storage.  ``mark_all_completed`` and ``upsert_pruned_user`` work
    against an in-memory cache populated by the hook lifecycle through
    ``set_active_context()``.
    """

    _STORAGE_KEY = ".user_retention_entries"

    def __init__(
        self,
        storage_factory: StorageFactory,
        config: UserRetentionBufferConfig | None = None,
    ) -> None:
        self._storage_factory = storage_factory
        self._config = config or UserRetentionBufferConfig()
        self._active_context: MemoryContext | None = None
        self._cached_entries: list[UserBufferEntry] = []

    # ------------------------------------------------------------------
    # hook-lifecycle helpers
    # ------------------------------------------------------------------

    def set_active_context(self, context: MemoryContext) -> None:
        """Set the active scope context for subsequent in-memory operations."""
        self._active_context = context

    async def load_active_entries(self) -> None:
        """Load entries from storage for the active context into the in-memory cache."""
        if self._active_context is None:
            raise RuntimeError("No active context set — call set_active_context() first")
        self._cached_entries = await self._load_entries(self._active_context)

    async def flush_active_entries(self) -> None:
        """Write the in-memory cache back to storage for the active context."""
        if self._active_context is None:
            raise RuntimeError("No active context set — call set_active_context() first")
        await self._save_entries(self._active_context, self._cached_entries)

    # ------------------------------------------------------------------
    # UserRetentionBuffer interface
    # ------------------------------------------------------------------

    def mark_all_completed(self, assistant_content: str) -> None:
        """Mark every unfinished cached entry as completed (in-memory)."""
        self._cached_entries = [
            replace(entry, completing_assistant_content=assistant_content)
            if not entry.is_completed
            else entry
            for entry in self._cached_entries
        ]

    def upsert_pruned_user(self, entry: UserBufferEntry) -> None:
        """Dedup by fingerprint, append, and FIFO-evict (in-memory)."""
        # Remove any unfinished entry with the same fingerprint
        self._cached_entries = [
            existing
            for existing in self._cached_entries
            if not (not existing.is_completed and existing.fingerprint == entry.fingerprint)
        ]
        # Append new entry
        self._cached_entries.append(entry)
        # Enforce limits
        self._cached_entries = self._enforce_limits(self._cached_entries)

    async def get_entries(self, context: MemoryContext) -> list[UserBufferEntry]:
        """Load and return entries from storage for *context*."""
        return await self._load_entries(context)

    async def clear(self, context: MemoryContext) -> None:
        """Remove all entries from storage for *context*."""
        storage = await self._storage_factory(context)
        async with storage.get_lock().write():
            await storage.set(self._STORAGE_KEY, [])

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    async def _load_entries(self, context: MemoryContext) -> list[UserBufferEntry]:
        """Load entries from scoped storage."""
        storage = await self._storage_factory(context)
        raw = await storage.get(self._STORAGE_KEY)
        if raw is None:
            return []
        if not isinstance(raw, list):
            return []
        entries: list[UserBufferEntry] = []
        for item in raw:
            if isinstance(item, dict):
                parsed = UserBufferEntry.from_dict(item)
                if parsed is not None:
                    entries.append(parsed)
        return entries

    async def _save_entries(
        self,
        context: MemoryContext,
        entries: list[UserBufferEntry],
    ) -> None:
        """Persist entries to scoped storage."""
        storage = await self._storage_factory(context)
        async with storage.get_lock().write():
            await storage.set(
                self._STORAGE_KEY,
                [entry.to_dict() for entry in entries],
            )

    def _enforce_limits(self, entries: list[UserBufferEntry]) -> list[UserBufferEntry]:
        """Apply FIFO eviction and per-entry content truncation."""
        max_entries = self._config.max_entries
        max_user = self._config.max_user_chars
        max_assistant = self._config.max_assistant_chars

        # FIFO: drop oldest entries from the front
        if len(entries) > max_entries:
            entries = entries[len(entries) - max_entries:]

        # Per-entry content truncation
        result: list[UserBufferEntry] = []
        for entry in entries:
            truncated_user = entry.pruned_user_content[:max_user] if len(entry.pruned_user_content) > max_user else entry.pruned_user_content
            truncated_assistant = None
            if entry.completing_assistant_content is not None:
                truncated_assistant = entry.completing_assistant_content[:max_assistant] if len(entry.completing_assistant_content) > max_assistant else entry.completing_assistant_content
            if truncated_user != entry.pruned_user_content or truncated_assistant != entry.completing_assistant_content:
                result.append(
                    replace(
                        entry,
                        pruned_user_content=truncated_user,
                        completing_assistant_content=truncated_assistant,
                    )
                )
            else:
                result.append(entry)
        return result
