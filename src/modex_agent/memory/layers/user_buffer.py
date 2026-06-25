"""User retention buffer layer — storage-backed lifecycle for pruned user context."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

logger = logging.getLogger(__name__)

from modex_agent.memory.core.layers import UserRetentionBuffer as _CoreURB
from modex_agent.core.scope import MemoryContext
from modex_agent.memory.layers.config import StorageFactory, UserRetentionBufferConfig
from modex_agent.memory.user_buffer import UserBufferEntry


class UserRetentionBuffer(_CoreURB):
    """Preserve pruned user/agent messages and track completion by plain assistant responses.

    ``mark_all_completed`` and ``upsert_pruned_user`` accept an explicit
    ``MemoryContext`` and operate on the scoped storage backing that context.
    ``get_entries`` and ``clear`` also accept an explicit ``MemoryContext``
    for injection and reset call-sites.
    """

    pass


class ScopedUserRetentionBuffer(UserRetentionBuffer):
    """Storage-backed ``UserRetentionBuffer`` resolved through a ``StorageFactory``.

    Entries are persisted under the key ``".user_retention_entries"`` in the
    scoped storage.
    """

    _STORAGE_KEY = ".user_retention_entries"

    def __init__(
        self,
        storage_factory: StorageFactory,
        config: UserRetentionBufferConfig | None = None,
    ) -> None:
        self._storage_factory = storage_factory
        self._config = config or UserRetentionBufferConfig()

    # ------------------------------------------------------------------
    # UserRetentionBuffer interface (context-aware, async)
    # ------------------------------------------------------------------

    async def append_entries(
        self,
        context: MemoryContext,
        entries: Sequence[Any],
    ) -> None:
        """Append entries to the buffer for *context*."""
        if not entries:
            return
        existing = await self._load_entries(context)
        for entry in entries:
            if not isinstance(entry, UserBufferEntry):
                continue
            existing.append(entry)
        await self._save_entries(context, self._enforce_limits(existing))

    async def get_entries(self, context: MemoryContext) -> list[UserBufferEntry]:
        """Load and return entries from storage for *context*."""
        return await self._load_entries(context)

    async def replace_entries(
        self,
        context: MemoryContext,
        entries: Sequence[Any],
    ) -> None:
        """Replace all entries for *context*."""
        typed: list[UserBufferEntry] = [e for e in entries if isinstance(e, UserBufferEntry)]
        await self._save_entries(context, typed)

    async def clear(self, context: MemoryContext) -> None:
        """Remove all entries from storage for *context*."""
        storage = await self._storage_factory(context)
        async with storage.get_lock().write():
            await storage.set(self._STORAGE_KEY, [])

    async def mark_all_completed(
        self,
        context: MemoryContext,
        assistant_content: str,
    ) -> None:
        """Load, mark every unfinished entry as completed, and save.

        The *assistant_content* is written into every unfinished entry.
        Afterwards each entry runs through ``_enforce_limits`` so
        ``completing_assistant_content`` respects the configured size cap.
        """
        entries = await self._load_entries(context)
        entries = [
            replace(entry, completing_assistant_content=assistant_content)
            if not entry.is_completed
            else entry
            for entry in entries
        ]
        entries = self._enforce_limits(entries)
        await self._save_entries(context, entries)

    async def upsert_pruned_user(
        self,
        context: MemoryContext,
        entry: Any,
    ) -> None:
        """Dedup by fingerprint, append, FIFO-evict, and persist."""
        if not isinstance(entry, UserBufferEntry):
            return
        entries = await self._load_entries(context)
        # Remove any unfinished entry with the same fingerprint
        entries = [
            existing
            for existing in entries
            if not (not existing.is_completed and existing.fingerprint == entry.fingerprint)
        ]
        entries.append(entry)
        entries = self._enforce_limits(entries)
        await self._save_entries(context, entries)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    async def _load_entries(self, context: MemoryContext) -> list[UserBufferEntry]:
        """Load entries from scoped storage.

        Resilient to JSON parse errors, malformed entries, and I/O failures:
        returns an empty list instead of propagating exceptions so that URB
        injection is safely skipped on corruption.
        """
        try:
            storage = await self._storage_factory(context)
            raw = await storage.get(self._STORAGE_KEY)
            if raw is None:
                return []
            if not isinstance(raw, list):
                return []
            entries: list[UserBufferEntry] = []
            for item in raw:
                try:
                    if isinstance(item, dict):
                        parsed = UserBufferEntry.from_dict(item)
                        if parsed is not None:
                            entries.append(parsed)
                except Exception:
                    logger.debug("Skipping malformed URB entry", exc_info=True)
                    continue
            return entries
        except Exception:
            logger.warning("Failed to load URB entries", exc_info=True)
            return []

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
        """Apply FIFO eviction and per-entry content truncation.

        XML-formatted entries use ``truncate_xml_safe`` with the entry's
        ``truncatable_paths`` to preserve structure.  Plain entries use
        head truncation.
        """
        from modex_agent.core.message import ContentFormat
        from modex_agent.memory.xml_truncate import truncate_xml_safe

        max_entries = self._config.max_entries
        max_user = self._config.max_user_chars
        max_assistant = self._config.max_assistant_chars

        # FIFO: drop oldest entries from the front
        if len(entries) > max_entries:
            entries = entries[len(entries) - max_entries :]

        # Per-entry content truncation (deferred imports to avoid circular deps)
        is_xml = str(ContentFormat.XML)
        result: list[UserBufferEntry] = []
        for entry in entries:
            user = entry.pruned_user_content
            if len(user) > max_user:
                if entry.content_format == is_xml:
                    user = truncate_xml_safe(user, max_user, entry.truncatable_paths or [])
                else:
                    user = user[:max_user]

            assistant = entry.completing_assistant_content
            if assistant is not None and len(assistant) > max_assistant:
                if entry.content_format == is_xml:
                    assistant = truncate_xml_safe(
                        assistant, max_assistant, entry.truncatable_paths or []
                    )
                else:
                    assistant = assistant[:max_assistant]

            if user != entry.pruned_user_content or assistant != entry.completing_assistant_content:
                result.append(
                    replace(entry, pruned_user_content=user, completing_assistant_content=assistant)
                )
            else:
                result.append(entry)
        return result
