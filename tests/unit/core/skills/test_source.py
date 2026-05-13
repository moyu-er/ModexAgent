"""Unit tests for core/skills/source.py."""

import tempfile
from pathlib import Path

import pytest

from framework.core.skills.models import Skill, SkillMetadata, SkillResource
from framework.core.skills.source import (
    CompositeSkillSource,
    FileSkillSource,
    InlineSkillSource,
)


class TestFileSkillSource:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    @pytest.mark.asyncio
    async def test_list_skills_finds_markdown_files(self, tmp_dir):
        skill_dir = tmp_dir / "demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo_skill\n---\nDemo content", encoding="utf-8"
        )
        source = FileSkillSource(directories=[tmp_dir])
        summaries = await source.list_skills()
        assert len(summaries) == 1
        assert summaries[0].name == "demo_skill"
        assert Path(summaries[0].location).resolve() == (skill_dir / "SKILL.md").resolve()

    @pytest.mark.asyncio
    async def test_list_skills_auto_scans_resources(self, tmp_dir):
        skill_dir = tmp_dir / "auto"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\n---\ncontent", encoding="utf-8")
        (skill_dir / "scripts").mkdir()
        (skill_dir / "references").mkdir()
        source = FileSkillSource(directories=[tmp_dir])
        summaries = await source.list_skills()
        names = {r.name for r in summaries[0].resources}
        assert "scripts" in names
        assert "references" in names

    @pytest.mark.asyncio
    async def test_load_skill_uses_cached_summary(self, tmp_dir):
        skill_dir = tmp_dir / "cached"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: cached_skill\ndescription: orig\n---\nBody", encoding="utf-8"
        )
        source = FileSkillSource(directories=[tmp_dir], cache=True)
        await source.list_skills()
        skill = await source.load_skill("cached_skill")
        assert skill is not None
        assert skill.name == "cached_skill"
        assert skill.description == "orig"
        assert skill.content == "Body"

    @pytest.mark.asyncio
    async def test_load_skill_returns_none_for_missing(self, tmp_dir):
        source = FileSkillSource(directories=[tmp_dir], cache=True)
        skill = await source.load_skill("missing")
        assert skill is None

    @pytest.mark.asyncio
    async def test_load_skill_location_from_summary_not_frontmatter(self, tmp_dir):
        skill_dir = tmp_dir / "loc"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: loc_skill\nlocation: fake/path\n---\nBody", encoding="utf-8"
        )
        source = FileSkillSource(directories=[tmp_dir], cache=True)
        await source.list_skills()
        skill = await source.load_skill("loc_skill")
        assert skill is not None
        # location should come from summary (real path), not frontmatter
        assert skill.location is not None
        assert str(skill_dir / "SKILL.md") in skill.location

    @pytest.mark.asyncio
    async def test_directory_layout_ignores_loose_files(self, tmp_dir):
        (tmp_dir / "notes.md").write_text("Not a skill", encoding="utf-8")
        skill_dir = tmp_dir / "demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\n---\nBody", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir], layout="directory")
        summaries = await source.list_skills()
        assert len(summaries) == 1
        assert summaries[0].name == "demo"
        assert Path(summaries[0].location).resolve() == (skill_dir / "SKILL.md").resolve()

    @pytest.mark.asyncio
    async def test_directory_layout_uses_frontmatter_name(self, tmp_dir):
        skill_dir = tmp_dir / "my_skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: override_name\n---\nBody", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir], layout="directory")
        summaries = await source.list_skills()
        assert len(summaries) == 1
        assert summaries[0].name == "override_name"

    @pytest.mark.asyncio
    async def test_custom_skill_filename(self, tmp_dir):
        skill_dir = tmp_dir / "demo"
        skill_dir.mkdir()
        (skill_dir / "custom.md").write_text("---\n---\nBody", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir], layout="directory", skill_filename="custom.md")
        summaries = await source.list_skills()
        assert len(summaries) == 1
        assert summaries[0].name == "demo"

    @pytest.mark.asyncio
    async def test_custom_exclude_names(self, tmp_dir):
        (tmp_dir / "changelog.md").write_text("---\n---\nBody", encoding="utf-8")
        (tmp_dir / "real.md").write_text("---\n---\nBody", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir], exclude_names=("changelog.md", "readme.md"))
        summaries = await source.list_skills()
        names = {s.name for s in summaries}
        assert "changelog" not in names
        assert "real" in names

    @pytest.mark.asyncio
    async def test_custom_resource_dirs(self, tmp_dir):
        skill_dir = tmp_dir / "demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\n---\nBody", encoding="utf-8")
        (skill_dir / "scripts").mkdir()
        (skill_dir / "data").mkdir()
        source = FileSkillSource(directories=[tmp_dir], resource_dirs=("scripts", "data"))
        summaries = await source.list_skills()
        names = {r.name for r in summaries[0].resources}
        assert "scripts" in names
        assert "data" in names
        assert "references" not in names


    @pytest.mark.asyncio
    async def test_list_skill_names_directory_layout(self, tmp_dir):
        skill_dir = tmp_dir / "alpha"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("content", encoding="utf-8")
        (tmp_dir / "beta").mkdir()
        (tmp_dir / "beta" / "SKILL.md").write_text("content", encoding="utf-8")
        (tmp_dir / "no_skill").mkdir()  # no SKILL.md
        source = FileSkillSource(directories=[tmp_dir], layout="directory")
        names = source.list_skill_names(tmp_dir)
        assert names == {"alpha", "beta"}

    @pytest.mark.asyncio
    async def test_list_skill_names_flat_layout(self, tmp_dir):
        (tmp_dir / "weather.md").write_text("content", encoding="utf-8")
        (tmp_dir / "cron.md").write_text("content", encoding="utf-8")
        (tmp_dir / "readme.md").write_text("content", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir], layout="flat")
        names = source.list_skill_names(tmp_dir)
        assert names == {"weather", "cron"}

    @pytest.mark.asyncio
    async def test_list_skill_names_missing_directory(self, tmp_dir):
        source = FileSkillSource(directories=[tmp_dir])
        names = source.list_skill_names(tmp_dir / "nope")
        assert names == set()

    @pytest.mark.asyncio
    async def test_invalidate_cache_clears_listing(self, tmp_dir):
        skill_dir = tmp_dir / "demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\n---\nBody", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir], cache=True)
        await source.list_skills()
        assert source._listing is not None

        source.invalidate_cache()
        assert source._listing is None

    @pytest.mark.asyncio
    async def test_invalidate_cache_then_reload(self, tmp_dir):
        skill_dir = tmp_dir / "demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\n---\nv1", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir], cache=True)
        skills = await source.load()
        assert skills[0].content == "v1"

        (skill_dir / "SKILL.md").write_text("---\n---\nv2", encoding="utf-8")
        # Without invalidate, cache returns v1
        skills = await source.load()
        assert skills[0].content == "v1"

        source.invalidate_cache()
        skills = await source.load()
        assert skills[0].content == "v2"

    def test_directories_property(self, tmp_dir):
        source = FileSkillSource(directories=[tmp_dir])
        assert source.directories == [tmp_dir.resolve()]

    def test_layout_property(self, tmp_dir):
        source = FileSkillSource(directories=[tmp_dir], layout="flat")
        assert source.layout == "flat"


class TestCompositeSkillSource:
    @pytest.mark.asyncio
    async def test_last_wins_deduplication(self):
        s1 = InlineSkillSource(
            [Skill(name="a", content="from_s1")], name="s1"
        )
        s2 = InlineSkillSource(
            [Skill(name="a", content="from_s2")], name="s2"
        )
        composite = CompositeSkillSource([s1, s2], merge_strategy="last_wins")
        summaries = await composite.list_skills()
        assert len(summaries) == 1
        skill = await composite.load_skill("a")
        assert skill is not None
        assert skill.content == "from_s2"

    @pytest.mark.asyncio
    async def test_first_wins_deduplication(self):
        s1 = InlineSkillSource(
            [Skill(name="a", content="from_s1")], name="s1"
        )
        s2 = InlineSkillSource(
            [Skill(name="a", content="from_s2")], name="s2"
        )
        composite = CompositeSkillSource([s1, s2], merge_strategy="first_wins")
        skill = await composite.load_skill("a")
        assert skill is not None
        assert skill.content == "from_s1"

    @pytest.mark.asyncio
    async def test_error_strategy_raises_on_duplicate(self):
        s1 = InlineSkillSource(
            [Skill(name="a", content="from_s1")], name="s1"
        )
        s2 = InlineSkillSource(
            [Skill(name="a", content="from_s2")], name="s2"
        )
        composite = CompositeSkillSource([s1, s2], merge_strategy="error")
        with pytest.raises(ValueError, match="Duplicate skill 'a'"):
            await composite.list_skills()

    @pytest.mark.asyncio
    async def test_error_strategy_raises_on_load_skill_duplicate(self):
        s1 = InlineSkillSource(
            [Skill(name="a", content="from_s1")], name="s1"
        )
        s2 = InlineSkillSource(
            [Skill(name="a", content="from_s2")], name="s2"
        )
        composite = CompositeSkillSource([s1, s2], merge_strategy="error")
        with pytest.raises(ValueError, match="Duplicate skill 'a'"):
            await composite.load_skill("a")

    @pytest.mark.asyncio
    async def test_load_skill_ignores_missing(self):
        s1 = InlineSkillSource([], name="s1")
        composite = CompositeSkillSource([s1])
        assert await composite.load_skill("missing") is None


class TestInlineSkillSource:
    @pytest.mark.asyncio
    async def test_list_and_load(self):
        source = InlineSkillSource(
            [Skill(name="x", content="c")], name="inline"
        )
        summaries = await source.list_skills()
        assert len(summaries) == 1
        skill = await source.load_skill("x")
        assert skill is not None
        assert skill.content == "c"
