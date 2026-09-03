"""Skill filters (plan §11) — strategy hierarchy over ``Skill`` values."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from .models import ResolutionContext, Skill

logger = logging.getLogger(__name__)


class SkillFilter(ABC):
    """Abstract strategy for filtering a list of skills."""

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
