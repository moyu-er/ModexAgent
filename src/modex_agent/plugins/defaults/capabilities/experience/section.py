"""The ``experience.injection`` prompt-section provider (plan §10.2 §10.3).

The retired ``_ExperienceInjectionProvider`` from the flat capability
module — manager-driven (version = content hash), byte-identical content
to the retired ``MemorySystemContextManager._experience_manager`` special
case, now reading through the catalog.
"""

from __future__ import annotations

import hashlib

from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.plugins.defaults.capabilities.experience.catalog import ExperienceCatalog
from modex_agent.plugins.defaults.capabilities.experience.paths import (
    MAX_INJECTED_EXPERIENCES,
)


class ExperienceInjectionProvider(SystemPromptProvider):
    """``experience.injection`` — the catalog-driven injection section.

    Version = content hash: the provider instance is REUSED across
    ``load()`` calls (the capability-section channel contract), so a
    stable hash keeps the KV-cache prefix stable within a session while a
    mid-session EXPERIENCE.md write (the review hook) refreshes exactly
    once. The catalog's source is scope-less, so the context-free
    ``render_index()`` equals the retired ``build_prompt(context=ctx)``
    byte-for-byte.
    """

    def __init__(self, catalog: ExperienceCatalog) -> None:
        super().__init__()
        self._catalog = catalog

    async def _fetch_version(self) -> str:
        content = await self._catalog.render_index(limit=MAX_INJECTED_EXPERIENCES)
        if not content:
            return "empty"
        return hashlib.md5(content.encode()).hexdigest()[:16]

    async def _fetch_content(self) -> str:
        return await self._catalog.render_index(limit=MAX_INJECTED_EXPERIENCES)
