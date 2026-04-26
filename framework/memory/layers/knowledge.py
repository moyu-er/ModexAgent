"""Default knowledge memory manager backed by scoped storage factories."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from framework.memory.core.consolidation import MemoryUpdate, MemoryUpdateMode
from framework.memory.core.layers import KnowledgeMemoryManager
from framework.memory.core.models import LongTermMemory
from framework.memory.core.scope import MemoryContext, MemoryScope
from framework.memory.layers.config import KnowledgeMemoryConfig, StorageFactory

logger = logging.getLogger(__name__)


class ScopedKnowledgeMemoryManager(KnowledgeMemoryManager):
    """Knowledge layer manager that resolves storage through a StorageFactory."""

    def __init__(
        self,
        storage_factory: StorageFactory,
        config: KnowledgeMemoryConfig | None = None,
    ) -> None:
        self._storage_factory = storage_factory
        self._config = config or KnowledgeMemoryConfig()

    def get_scope(self) -> MemoryScope:
        return self._config.scope

    async def ensure_defaults(
        self,
        context: MemoryContext,
        defaults: Mapping[str, str] | None = None,
    ) -> None:
        storage = await self._storage_factory(context)
        defaults = defaults or {}
        for key, file_name in self._config.default_files.items():
            if await storage.get(file_name) is None:
                await storage.set(file_name, defaults.get(key, ""))

    async def get_all(self, context: MemoryContext) -> LongTermMemory:
        await self.ensure_defaults(context)
        storage = await self._storage_factory(context)
        files = self._config.default_files
        custom: dict[str, str] = {}
        default_names = set(files.values())
        for key in await storage.list_keys():
            if key in default_names or key.endswith("._meta"):
                continue
            value = await storage.get(key)
            if isinstance(value, str):
                custom[key] = value
            elif isinstance(value, dict) and "value" in value:
                custom[key] = str(value.get("value") or "")
        return LongTermMemory(
            soul=await self.get_file(context, "soul") or "",
            user=await self.get_file(context, "user") or "",
            memory=await self.get_file(context, "memory") or "",
            custom=custom,
        )

    async def get_file(self, context: MemoryContext, file_key: str) -> str | None:
        storage = await self._storage_factory(context)
        file_name = self._config.default_files.get(file_key, file_key)
        value = await storage.get(file_name)
        if isinstance(value, dict) and "value" in value:
            return str(value.get("value") or "")
        if isinstance(value, str):
            return value
        return None

    async def apply_update(self, context: MemoryContext, update: MemoryUpdate) -> str:
        storage = await self._storage_factory(context)
        file_name = self._config.default_files.get(update.file_name, update.file_name)
        existing = await self.get_file(context, file_name) or ""
        mode = update.mode.lower()
        if mode == str(MemoryUpdateMode.SECTION_REPLACE):
            result = update.content
        elif mode == str(MemoryUpdateMode.REPLACE_TEXT):
            if update.search_text and update.search_text in existing:
                result = existing.replace(update.search_text, update.content, 1)
            elif update.content and update.content in existing:
                logger.debug(
                    "replace_text update already applied for file %s",
                    file_name,
                )
                result = existing
            else:
                logger.warning(
                    "replace_text fallback append for file %s because search_text was not found",
                    file_name,
                )
                result = self._append(existing, update.content)
        elif mode == str(MemoryUpdateMode.REMOVE):
            if update.search_text and update.search_text in existing:
                result = existing.replace(update.search_text, "", 1)
            elif update.content and update.content in existing:
                result = existing.replace(update.content, "", 1)
            else:
                logger.warning(
                    "remove update skipped for file %s: search_text/content not found",
                    file_name,
                )
                result = existing
        else:
            result = self._append(existing, update.content)
        await storage.set(file_name, result)
        await storage.append_log(
            {
                "file": file_name,
                "mode": update.mode,
                "reason": update.reason,
            }
        )
        await self._maybe_prune_changelog(context)
        return result

    async def _maybe_prune_changelog(self, context: MemoryContext) -> None:
        if self._config.max_changelog_entries is None:
            return
        storage = await self._storage_factory(context)
        entries = await storage.read_logs(since_cursor=0)
        if len(entries) <= self._config.max_changelog_entries:
            return
        await storage.save_logs(entries[-self._config.max_changelog_entries :])

    async def clear(self, context: MemoryContext) -> None:
        storage = await self._storage_factory(context)
        for key in await storage.list_keys():
            await storage.delete(key)

    def _append(self, existing: str, content: str) -> str:
        if not content:
            return existing
        if self._contains_block(existing, content):
            return existing
        if not existing:
            return content
        return existing + ("\n" if not existing.endswith("\n") else "") + content

    @staticmethod
    def _contains_block(existing: str, content: str) -> bool:
        normalized_existing = existing.strip()
        normalized_content = content.strip()
        if not normalized_content:
            return True
        return normalized_content in {
            block.strip()
            for block in normalized_existing.splitlines()
            if block.strip()
        } or normalized_content in normalized_existing
