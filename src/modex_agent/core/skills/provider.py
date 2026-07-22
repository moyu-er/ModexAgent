"""``SkillProvider`` — the ``SystemPromptProvider`` adapter for skills.

Lives in ``core.skills`` (not ``memory.prompt_pipeline``) so that
``SkillManager.build_provider()`` can construct it without a ``core → memory``
reverse import. The memory prompt pipeline re-exports it from
``memory.prompt_pipeline.providers`` for backward compatibility.
"""

from __future__ import annotations

from modex_agent.core.prompt import SystemPromptProvider


class SkillProvider(SystemPromptProvider):
    """Skill metadata XML. Never refreshes during react."""

    def __init__(self, skill_xml: str) -> None:
        super().__init__()
        self._skill_xml = skill_xml

    async def _fetch_version(self) -> str:
        return "static"

    async def _fetch_content(self) -> str:
        return self._skill_xml
