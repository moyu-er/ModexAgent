"""Unit tests for core/skills/builder.py."""

from pathlib import Path

import pytest

from framework.core.skills.builder import (
    HybridBuilder,
    InlineBuilder,
    ProgressiveBuilder,
    _render_skill_table,
)
from framework.core.skills.models import ResolutionContext, Skill, SkillMetadata


class MockToolManager:
    def __init__(self, tools):
        self._tools = set(tools)

    def has_tool(self, name):
        return name in self._tools


class TestInlineBuilder:
    @pytest.mark.asyncio
    async def test_empty_skills_returns_empty(self):
        b = InlineBuilder()
        assert await b.build([]) == ""

    @pytest.mark.asyncio
    async def test_inlines_full_content(self):
        skills = [Skill(name="s1", description="d1", content="body1")]
        b = InlineBuilder()
        out = await b.build(skills)
        assert "## Skills" in out
        assert "### s1" in out
        assert "*d1*" in out
        assert "body1" in out


class TestProgressiveBuilder:
    @pytest.mark.asyncio
    async def test_empty_skills_returns_empty(self):
        b = ProgressiveBuilder()
        assert await b.build([]) == ""

    @pytest.mark.asyncio
    async def test_directory_output_with_read_tool(self):
        tm = MockToolManager(["read_file"])
        ctx = ResolutionContext(tool_manager=tm)
        skills = [Skill(name="s1", description="d1", location="/tmp/s1.md")]
        b = ProgressiveBuilder()
        out = await b.build(skills, ctx)
        assert "## Skills" in out
        # Location is absolute path (platform-native format)
        assert "| s1 | d1 |" in out
        assert "s1.md" in out
        assert "| Skill | Description | Location |" in out

    @pytest.mark.asyncio
    async def test_silent_downgrade_without_read_tool(self):
        tm = MockToolManager([])
        ctx = ResolutionContext(tool_manager=tm)
        skills = [Skill(name="s1", content="body")]
        b = ProgressiveBuilder()
        out = await b.build(skills, ctx)
        assert "## Skills" in out
        assert "body" in out
        assert "| Skill | Description | Location |" not in out

    @pytest.mark.asyncio
    async def test_downgrade_with_none_context(self):
        skills = [Skill(name="s1", content="body")]
        b = ProgressiveBuilder()
        out = await b.build(skills, None)
        assert "body" in out
        assert "| Skill | Description | Location |" not in out

    @pytest.mark.asyncio
    async def test_custom_prompt_template(self):
        tm = MockToolManager(["read_file"])
        ctx = ResolutionContext(tool_manager=tm)
        skills = [Skill(name="s1", description="d1", location="/tmp/s1.md")]
        b = ProgressiveBuilder(prompt_template="Custom prompt text")
        out = await b.build(skills, ctx)
        assert "Custom prompt text" in out
        # Location is absolute path (platform-native format)
        assert "| s1 | d1 |" in out
        assert "s1.md" in out


class TestHybridBuilder:
    @pytest.mark.asyncio
    async def test_all_mode_inlines_everything(self):
        skills = [
            Skill(name="a", content="ac", metadata=SkillMetadata(always=True)),
            Skill(name="b", content="bc"),
        ]
        b = HybridBuilder(inline_mode="all")
        out = await b.build(skills)
        assert "### a" in out
        assert "### b" in out
        assert "ac" in out
        assert "bc" in out

    @pytest.mark.asyncio
    async def test_none_mode_lists_everything_with_read_tool(self):
        tm = MockToolManager(["read_file"])
        ctx = ResolutionContext(tool_manager=tm)
        skills = [
            Skill(name="a", content="xyz123", metadata=SkillMetadata(always=True)),
        ]
        b = HybridBuilder(inline_mode="none")
        out = await b.build(skills, ctx)
        assert "| a |" in out
        assert "xyz123" not in out

    @pytest.mark.asyncio
    async def test_none_mode_downgrades_without_read_tool(self):
        tm = MockToolManager([])
        ctx = ResolutionContext(tool_manager=tm)
        skills = [Skill(name="a", content="xyz123")]
        b = HybridBuilder(inline_mode="none")
        out = await b.build(skills, ctx)
        assert "xyz123" in out
        assert "| Skill | Description | Location |" not in out

    @pytest.mark.asyncio
    async def test_always_mode_splits_inline_and_directory(self):
        tm = MockToolManager(["read_file"])
        ctx = ResolutionContext(tool_manager=tm)
        skills = [
            Skill(name="inline_skill", content="ic", metadata=SkillMetadata(always=True)),
            Skill(name="dir_skill", content="dc", location="/x.md"),
        ]
        b = HybridBuilder(inline_mode="always")
        out = await b.build(skills, ctx)
        assert "### inline_skill" in out
        assert "ic" in out
        assert "| dir_skill |" in out
        assert "dc" not in out

    @pytest.mark.asyncio
    async def test_always_mode_falls_back_to_inline_when_no_read_tool(self):
        tm = MockToolManager([])
        ctx = ResolutionContext(tool_manager=tm)
        skills = [
            Skill(name="inline_skill", content="ic", metadata=SkillMetadata(always=True)),
            Skill(name="dir_skill", content="dc"),
        ]
        b = HybridBuilder(inline_mode="always")
        out = await b.build(skills, ctx)
        assert "### inline_skill" in out
        assert "### dir_skill" in out
        assert "ic" in out
        assert "dc" in out
        assert "| Skill | Description | Location |" not in out

    @pytest.mark.asyncio
    async def test_empty_skills_returns_empty(self):
        b = HybridBuilder()
        assert await b.build([]) == ""


class TestRenderSkillTable:
    def test_uses_absolute_paths(self):
        skills = [Skill(name="s1", description="d1", location="/base/skills/s1.md")]
        out = _render_skill_table(skills, base_path=Path("/base"))
        # Location and Resources are absolute paths (platform-native format)
        assert "| s1 | d1 |" in out
        assert "s1.md" in out
        # Resources column contains the parent directory
        assert "skills" in out
