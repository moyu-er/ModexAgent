"""Default core memory manager backed by scoped storage factories."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from modex_agent.core.scope import MemoryContext, Scope
from modex_agent.memory.core.consolidation import MemoryUpdate, MemoryUpdateMode
from modex_agent.memory.core.layers import CoreMemoryManager
from modex_agent.memory.core.models import CoreMemoryContents
from modex_agent.memory.core.store_metadata import StoreMetadata
from modex_agent.memory.core_memory_search import (
    FullDumpCoreMemoryStrategy,
    CoreMemorySearchStrategy,
)
from modex_agent.memory.layers.config import CoreMemoryConfig, StorageFactory
from modex_agent.memory.utils import estimate_text_tokens

logger = logging.getLogger(__name__)


class ScopedCoreMemoryManager(CoreMemoryManager):
    """Core memory layer manager that resolves storage through a StorageFactory.

    Supports automatic consolidation: when a file exceeds the token threshold
    after an update, the consolidation function is called to compress it.
    """

    def __init__(
        self,
        storage_factory: StorageFactory,
        config: CoreMemoryConfig | None = None,
        search_strategy: CoreMemorySearchStrategy | None = None,
        consolidation_fn: Callable[[str, str], Awaitable[str]] | None = None,
        consolidation_threshold_tokens: int = 2000,
    ) -> None:
        self._storage_factory = storage_factory
        self._config = config or CoreMemoryConfig()
        self._search_strategy = search_strategy or FullDumpCoreMemoryStrategy()
        self._consolidation_fn = consolidation_fn
        self._consolidation_threshold = consolidation_threshold_tokens

    def get_scope(self) -> Scope:
        return self._config.scope

    async def get_storage_path(self, context: MemoryContext) -> Path | None:
        """Return the absolute path to core memory storage, if file-backed."""
        bundle = await self._storage_factory(context)
        store = bundle.messages
        if isinstance(store, StoreMetadata):
            return store.base_path
        return None

    async def ensure_defaults(
        self,
        context: MemoryContext,
        defaults: Mapping[str, str] | None = None,
    ) -> None:
        bundle = await self._storage_factory(context)
        defaults = defaults or {}
        for key, file_name in self._config.default_files.items():
            existing = await bundle.kv.get(file_name)
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
                        "Core memory template not found: %s (default_templates_dir=%s)",
                        template_path,
                        template_dir,
                    )

            # Fallback to defaults dict
            if not content and key in defaults:
                content = defaults[key]

            if not content:
                logger.warning(
                    "Skipping empty default for core memory file %s: "
                    "no template and no fallback provided",
                    file_name,
                )
                continue

            await bundle.kv.set(file_name, content)

    async def retrieve(self, context: MemoryContext, query: str = "") -> CoreMemoryContents:
        """Retrieve core memory using the configured search strategy."""
        full = await self.get_all(context)
        return await self._search_strategy.retrieve(full, query=query)

    async def get_all(self, context: MemoryContext) -> CoreMemoryContents:
        await self.ensure_defaults(context)
        bundle = await self._storage_factory(context)
        files = self._config.default_files
        custom: dict[str, str] = {}
        default_names = set(files.values())
        for key in await bundle.kv.list_keys():
            if key in default_names or key.endswith("._meta"):
                continue
            value = await bundle.kv.get(key)
            if isinstance(value, str):
                custom[key] = value
            elif isinstance(value, dict) and "value" in value:
                custom[key] = str(value.get("value") or "")
        return CoreMemoryContents(
            soul=await self.get_file(context, "soul") or "",
            user=await self.get_file(context, "user") or "",
            memory=await self.get_file(context, "memory") or "",
            custom=custom,
        )

    async def get_file(self, context: MemoryContext, file_key: str) -> str | None:
        bundle = await self._storage_factory(context)
        file_name = self._config.default_files.get(file_key, file_key)
        value = await bundle.kv.get(file_name)
        if isinstance(value, dict) and "value" in value:
            return str(value.get("value") or "")
        if isinstance(value, str):
            return value
        return None

    async def apply_update(self, context: MemoryContext, update: MemoryUpdate) -> str:
        bundle = await self._storage_factory(context)
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
        await bundle.kv.set(file_name, result)
        if bundle.archive is not None:
            await bundle.archive.append_log(
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
        """Manually trigger consolidation of a specific core memory file.

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
            "Core memory file %s exceeds threshold (%d > %d tokens), consolidating",
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
            bundle = await self._storage_factory(context)
            await bundle.kv.set(file_name, consolidated)
            if bundle.archive is not None:
                await bundle.archive.append_log(
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
        bundle = await self._storage_factory(context)
        if bundle.archive is None:
            return
        entries = await bundle.archive.read_logs(since_cursor=0)
        if len(entries) <= self._config.max_changelog_entries:
            return
        await bundle.archive.save_logs(entries[-self._config.max_changelog_entries :])

    async def clear(self, context: MemoryContext) -> None:
        bundle = await self._storage_factory(context)
        for key in await bundle.kv.list_keys():
            await bundle.kv.delete(key)

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
