"""The ``skills`` prompt-section provider (plan §11.5).

SkillsCapability
  -> SkillsSupply catalog_for(agent)
  -> SkillSectionProvider
  -> CapabilityWiring.prompt_providers
  -> TAIL capability-section anchor
"""

from __future__ import annotations

import hashlib

from modex_agent.core.prompt import SystemPromptProvider

from .catalog import SkillCatalog


class SkillSectionProvider(SystemPromptProvider):
    """Skill metadata XML rendered from the agent's live catalog view."""

    def __init__(self, catalog: SkillCatalog) -> None:
        super().__init__()
        self._catalog = catalog

    async def _fetch_version(self) -> str:
        content = await self._render_prompt()
        if not content:
            return "empty"
        return hashlib.md5(content.encode()).hexdigest()[:16]

    async def _fetch_content(self) -> str:
        return await self._render_prompt()

    async def _render_prompt(self) -> str:
        return await self._catalog.render_prompt()
