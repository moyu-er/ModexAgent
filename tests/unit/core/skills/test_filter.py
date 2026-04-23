"""Unit tests for core/skills/filter.py."""

import pytest

from framework.core.skills.filter import (
    AllowListFilter,
    CompositeFilter,
    DenyListFilter,
    DependencyFilter,
)
from framework.core.skills.models import ResolutionContext, Skill, SkillMetadata


class MockToolManager:
    def __init__(self, tools):
        self._tools = set(tools)

    def has_tool(self, name):
        return name in self._tools


class TestDependencyFilter:
    @pytest.mark.asyncio
    async def test_filter_mode_drops_missing_tool(self):
        tm = MockToolManager(["has_it"])
        ctx = ResolutionContext(tool_manager=tm)
        skills = [
            Skill(name="ok", metadata=SkillMetadata(requires_tools=["has_it"])),
            Skill(name="bad", metadata=SkillMetadata(requires_tools=["missing"])),
        ]
        f = DependencyFilter(mode="filter")
        result = await f.filter(skills, ctx)
        assert len(result) == 1
        assert result[0].name == "ok"

    @pytest.mark.asyncio
    async def test_warn_mode_keeps_but_warns(self, caplog):
        tm = MockToolManager([])
        ctx = ResolutionContext(tool_manager=tm)
        skills = [Skill(name="bad", metadata=SkillMetadata(requires_tools=["missing"]))]
        f = DependencyFilter(mode="warn")
        with caplog.at_level("WARNING"):
            result = await f.filter(skills, ctx)
        assert len(result) == 1
        assert "missing dependencies" in caplog.text
        assert "kept in warn mode" in caplog.text

    @pytest.mark.asyncio
    async def test_missing_bin_filter(self):
        skills = [
            Skill(name="needs_gh", metadata=SkillMetadata(requires_bins=["nonexistent_bin_12345"])),
        ]
        f = DependencyFilter(mode="filter")
        result = await f.filter(skills)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_missing_env_filter(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_ENV_12345", raising=False)
        skills = [
            Skill(name="needs_env", metadata=SkillMetadata(requires_env=["NONEXISTENT_ENV_12345"])),
        ]
        f = DependencyFilter(mode="filter")
        result = await f.filter(skills)
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_env_present_passes(self, monkeypatch):
        monkeypatch.setenv("TEST_SKILL_V2_VAR", "1")
        skills = [
            Skill(name="ok", metadata=SkillMetadata(requires_env=["TEST_SKILL_V2_VAR"])),
        ]
        f = DependencyFilter(mode="filter")
        result = await f.filter(skills)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_context_none_skips_tools_but_checks_bins_env(self, monkeypatch):
        monkeypatch.setenv("TEST_SKILL_V2_VAR2", "1")
        skills = [
            Skill(name="a", metadata=SkillMetadata(requires_tools=["unknown"])),
            Skill(name="b", metadata=SkillMetadata(requires_bins=["nonexistent_bin_12345"])),
            Skill(name="c", metadata=SkillMetadata(requires_env=["TEST_SKILL_V2_VAR2"])),
        ]
        f = DependencyFilter(mode="filter")
        result = await f.filter(skills, None)
        # tools check skipped => a passes; b fails bins; c passes env
        assert {s.name for s in result} == {"a", "c"}


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
