from __future__ import annotations

import logging
import os
import shutil
from abc import ABC, abstractmethod
from typing import Any

from .models import ResolutionContext, Skill

logger = logging.getLogger(__name__)


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


class DependencyFilter(SkillFilter):
    """Filter (or warn) based on whether required tools, binaries, or env vars are available.

    .. note::
       When ``context=None`` (or ``context.tool_manager`` is None),
       ``requires_tools`` checks are skipped. ``requires_bins`` and
       ``requires_env`` are always evaluated against the host environment.
    """

    def __init__(self, mode: str = "filter") -> None:
        self._mode = mode

    async def filter(
        self,
        skills: list[Skill],
        context: ResolutionContext | None = None,
    ) -> list[Skill]:
        if context is not None:
            tool_manager = context.tool_manager
            env_vars = context.env_vars or os.environ
        else:
            tool_manager = None
            env_vars = os.environ
        result: list[Skill] = []
        for skill in skills:
            missing: list[str] = []
            # requires_tools (skipped when no tool_manager available)
            if tool_manager is not None:
                for tool_name in skill.metadata.requires_tools:
                    has = False
                    if hasattr(tool_manager, "has_tool"):
                        try:
                            has = bool(tool_manager.has_tool(tool_name))
                        except Exception:  # pragma: no cover
                            has = False
                    if not has:
                        missing.append(f"tool:{tool_name}")
            # requires_bins
            for bin_name in skill.metadata.requires_bins:
                if not shutil.which(bin_name):
                    missing.append(f"bin:{bin_name}")
            # requires_env
            for env_name in skill.metadata.requires_env:
                if not env_vars.get(env_name):
                    missing.append(f"env:{env_name}")
            if missing:
                msg = f"Skill '{skill.name}' requires missing dependencies: {missing}"
                if self._mode == "filter":
                    logger.warning(msg)
                    continue
                else:
                    logger.warning(msg + " (kept in warn mode)")
            result.append(skill)
        return result


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
