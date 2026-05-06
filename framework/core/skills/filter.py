from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from .models import ResolutionContext, Skill
from .manager import SkillManager

logger = logging.getLogger(__name__)


class SkillWhitelistFilter:
    """基于白名单过滤的 SkillManager 包装器。"""

    def __init__(self, base: Any, allowed_skills: list[str] | None = None):
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
            from .builder import ProgressiveBuilder
            builder = ProgressiveBuilder()
        return await builder.build(skills, ctx)

    async def list_skills(self, ctx: ResolutionContext | None = None):
        skills = await self._base.list_skills(ctx)
        if self._allowed is None:
            return skills
        return [s for s in skills if s.name in self._allowed]


class SkillFilter(ABC):
    """Abstract strategy for filtering a list of skills.

    .. note::
       The input ``skills`` are full ``Skill`` objects (including ``content``),
       loaded by ``SkillManager.list_skills()`` before filtering.
    """

    @abstractmethod
    async def filter(
        self,
        skills: list[Skill],
        context: ResolutionContext | None = None,
    ) -> list[Skill]:
        """Return the subset of skills that should be active."""


class AlwaysFilter(SkillFilter):
    """Pass all skills through unmodified."""

    async def filter(
        self,
        skills: list[Skill],
        context: ResolutionContext | None = None,
    ) -> list[Skill]:
        return list(skills)


class AllowListFilter(SkillFilter):
    """Only allow skills whose names are in the allow-list."""

    def __init__(self, names: set[str]) -> None:
        self._names = set(names)

    async def filter(
        self,
        skills: list[Skill],
        context: ResolutionContext | None = None,
    ) -> list[Skill]:
        return [s for s in skills if s.name in self._names]


class DenyListFilter(SkillFilter):
    """Exclude skills whose names are in the deny-list."""

    def __init__(self, names: set[str]) -> None:
        self._names = set(names)

    async def filter(
        self,
        skills: list[Skill],
        context: ResolutionContext | None = None,
    ) -> list[Skill]:
        return [s for s in skills if s.name not in self._names]


class CompositeFilter(SkillFilter):
    """Apply multiple filters in sequence."""

    def __init__(self, filters: list[SkillFilter]) -> None:
        self._filters = list(filters)

    async def filter(
        self,
        skills: list[Skill],
        context: ResolutionContext | None = None,
    ) -> list[Skill]:
        result = list(skills)
        for f in self._filters:
            result = await f.filter(result, context)
        return result
