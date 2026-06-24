"""Unit tests for core/skills/filter.py."""

import pytest

from modex_agent.core.skills.filter import (
    AllowListFilter,
    CompositeFilter,
    DenyListFilter,
)
from modex_agent.core.skills.models import Skill


class TestAllowListFilter:
    @pytest.mark.asyncio
    async def test_only_allowed_names(self):
        f = AllowListFilter({"a", "c"})
        skills = [Skill(name="a"), Skill(name="b"), Skill(name="c")]
        result = await f.filter(skills)
        assert [s.name for s in result] == ["a", "c"]


class TestDenyListFilter:
    @pytest.mark.asyncio
    async def test_excludes_denied_names(self):
        f = DenyListFilter({"b"})
        skills = [Skill(name="a"), Skill(name="b"), Skill(name="c")]
        result = await f.filter(skills)
        assert [s.name for s in result] == ["a", "c"]


class TestCompositeFilter:
    @pytest.mark.asyncio
    async def test_applies_in_sequence(self):
        skills = [Skill(name="a"), Skill(name="b"), Skill(name="c")]
        f = CompositeFilter([
            DenyListFilter({"b"}),
            AllowListFilter({"a", "c"}),
        ])
        result = await f.filter(skills)
        assert [s.name for s in result] == ["a", "c"]
