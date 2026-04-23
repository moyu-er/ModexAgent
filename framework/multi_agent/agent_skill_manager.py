from __future__ import annotations

from framework.core.skills import ResolutionContext, SkillManager


class AgentSkillManager:
    """基于白名单过滤的 SkillManager 包装器。"""

    def __init__(self, base: SkillManager, allowed_skills: list[str] | None = None):
        self._base = base
        self._allowed = set(allowed_skills) if allowed_skills is not None else None

    def is_skill_allowed(self, name: str) -> bool:
        if self._allowed is None:
            return True
        return name in self._allowed

    async def build_prompt(self, ctx: ResolutionContext | None = None) -> str:
        if self._allowed is None:
            return await self._base.build_prompt(ctx)
        skills = await self.list_skills(ctx)
        builder = getattr(self._base, "_builder", None)
        if builder is None:
            from framework.core.skills import ProgressiveBuilder
            builder = ProgressiveBuilder()
        return await builder.build(skills, ctx)

    async def list_skills(self, ctx: ResolutionContext | None = None):
        skills = await self._base.list_skills(ctx)
        if self._allowed is None:
            return skills
        return [s for s in skills if s.name in self._allowed]
