"""Default knowledge memory manager backed by scoped storage factories."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from framework.memory.core.consolidation import MemoryUpdate, MemoryUpdateMode
from framework.memory.core.layers import KnowledgeMemoryManager
from framework.memory.core.models import LongTermMemory
from framework.memory.core.scope import MemoryContext, MemoryScope
from framework.memory.knowledge_search import (
    FullDumpKnowledgeStrategy,
    KnowledgeSearchStrategy,
)
from framework.memory.layers.config import KnowledgeMemoryConfig, StorageFactory
from framework.memory.utils import estimate_text_tokens

logger = logging.getLogger(__name__)


class ScopedKnowledgeMemoryManager(KnowledgeMemoryManager):
    """Knowledge layer manager that resolves storage through a StorageFactory.

    Supports automatic consolidation: when a file exceeds the token threshold
    after an update, the consolidation function is called to compress it.
    """

    def __init__(
        self,
        storage_factory: StorageFactory,
        config: KnowledgeMemoryConfig | None = None,
        search_strategy: KnowledgeSearchStrategy | None = None,
        consolidation_fn: Callable[[str, str], Awaitable[str]] | None = None,
        consolidation_threshold_tokens: int = 2000,
    ) -> None:
        self._storage_factory = storage_factory
        self._config = config or KnowledgeMemoryConfig()
        self._search_strategy = search_strategy or FullDumpKnowledgeStrategy()
        self._consolidation_fn = consolidation_fn
        self._consolidation_threshold = consolidation_threshold_tokens

    def get_scope(self) -> MemoryScope:
        return self._config.scope

    async def get_storage_path(self, context: MemoryContext) -> Path | None:
        """Return the absolute path to knowledge storage, if file-backed."""
        storage = await self._storage_factory(context)
        return storage.base_path

    async def ensure_defaults(
        self,
        context: MemoryContext,
        defaults: Mapping[str, str] | None = None,
    ) -> None:
        storage = await self._storage_factory(context)
        defaults = defaults or {}
        for key, file_name in self._config.default_files.items():
            existing = await storage.get(file_name)
            if existing is not None and (isinstance(existing, str) and existing.strip()):
                continue  # Don't overwrite existing non-empty content

            # Try to load from template
            content = ""
            template_dir = self._config.default_templates_dir
            if template_dir:
                from pathlib import Path

                template_path = Path(template_dir) / file_name
                if template_path.exists():
                    content = template_path.read_text(encoding="utf-8")
                else:
                    logger.warning(
                        "Knowledge template not found: %s (default_templates_dir=%s)",
                        template_path,
                        template_dir,
                    )

            # Fallback to defaults dict
            if not content and key in defaults:
                content = defaults[key]

            if not content:
                logger.warning(
                    "Skipping empty default for knowledge file %s: "
                    "no template and no fallback provided",
                    file_name,
                )
                continue

            await storage.set(file_name, content)

    async def retrieve(self, context: MemoryContext, query: str = "") -> LongTermMemory:
        """Retrieve knowledge using the configured search strategy."""
        full = await self.get_all(context)
        return await self._search_strategy.retrieve(full, query=query, max_tokens=2000)

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

        # Auto-consolidate if file exceeds threshold
        await self._maybe_consolidate(context, file_name, result)
        return result

    async def consolidate_file(self, context: MemoryContext, file_key: str) -> str | None:
        """Manually trigger consolidation of a specific knowledge file.

        Returns the consolidated content, or None if consolidation was skipped.
        """
        if self._consolidation_fn is None:
            return None
        content = await self.get_file(context, file_key)
        if not content:
            return None
        return await self._do_consolidate(context, file_key, content)

    async def _maybe_consolidate(
        self, context: MemoryContext, file_name: str, content: str
    ) -> None:
        """Check file size and consolidate if over threshold."""
        if self._consolidation_fn is None:
            return
        tokens = estimate_text_tokens(content)
        if tokens <= self._consolidation_threshold:
            return
        logger.info(
            "Knowledge file %s exceeds threshold (%d > %d tokens), consolidating",
            file_name,
            tokens,
            self._consolidation_threshold,
        )
        await self._do_consolidate(context, file_name, content)

    async def _do_consolidate(
        self, context: MemoryContext, file_name: str, content: str
    ) -> str | None:
        """Run consolidation on a file and persist the result."""
        try:
            if self._consolidation_fn is None:
                return None
            consolidated = await self._consolidation_fn(content, file_name)
            if not consolidated or not consolidated.strip():
                logger.warning("Consolidation returned empty for %s, skipping", file_name)
                return None
            storage = await self._storage_factory(context)
            await storage.set(file_name, consolidated)
            await storage.append_log(
                {
                    "file": file_name,
                    "mode": "consolidation",
                    "reason": f"auto-consolidated ({estimate_text_tokens(content)} -> {estimate_text_tokens(consolidated)} tokens)",
                }
            )
            logger.info(
                "Consolidated %s: %d -> %d tokens",
                file_name,
                estimate_text_tokens(content),
                estimate_text_tokens(consolidated),
            )
            return consolidated
        except Exception:
            logger.exception("Consolidation failed for %s", file_name)
            return None

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
        return (
            normalized_content
            in {block.strip() for block in normalized_existing.splitlines() if block.strip()}
            or normalized_content in normalized_existing
        )
