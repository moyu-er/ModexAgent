"""Edge-case unit tests for the skill system."""

import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.plugins.defaults.capabilities.skills.builder import DefaultSkillBuilder
from modex_agent.plugins.defaults.capabilities.skills.catalog import SkillCatalog
from modex_agent.plugins.defaults.capabilities.skills.models import (
    Skill,
    SkillMetadata,
    SkillResource,
    SkillSummary,
)
from modex_agent.plugins.defaults.capabilities.skills.source import (
    FileSkillSource,
    InlineSkillSource,
)


class TestFileSkillSourceEdgeCases:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    @pytest.mark.asyncio
    async def test_ignores_readme_and_index(self, tmp_dir):
        (tmp_dir / "README.md").write_text("---\nname: readme\n---\nBody", encoding="utf-8")
        (tmp_dir / "index.md").write_text("---\nname: index\n---\nBody", encoding="utf-8")
        (tmp_dir / "SKILL.md").write_text(
            "---\nname: real\ndescription: Real skill\n---\nBody", encoding="utf-8"
        )
        source = FileSkillSource(directories=[tmp_dir])
        summaries = await source.list_skills()
        assert len(summaries) == 1
        assert summaries[0].name == "real"

    @pytest.mark.asyncio
    async def test_malformed_frontmatter_is_not_discovered(self, tmp_dir, caplog):
        path = tmp_dir / "bad.md"
        path.write_text("---\nnot_yaml: [\n---\nBody", encoding="utf-8")

        summaries = await FileSkillSource(directories=[tmp_dir]).list_skills()

        assert summaries == ()
        assert str(path) in caplog.text
        assert "description" in caplog.text

    @pytest.mark.asyncio
    async def test_no_frontmatter_is_not_discovered(self, tmp_dir, caplog):
        path = tmp_dir / "implicit.md"
        path.write_text("No frontmatter here", encoding="utf-8")

        summaries = await FileSkillSource(directories=[tmp_dir]).list_skills()

        assert summaries == ()
        assert str(path) in caplog.text
        assert "description" in caplog.text

    @pytest.mark.asyncio
    async def test_nested_directory_discovery(self, tmp_dir):
        nested = tmp_dir / "deep" / "nested"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text("---\nname: nested-skill\ndescription: Nested skill\n---\nBody", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir])
        summaries = await source.list_skills()
        assert len(summaries) == 1
        assert summaries[0].name == "nested-skill"

    @pytest.mark.asyncio
    async def test_multiple_directories_merge(self, tmp_dir):
        d1 = tmp_dir / "dir1"
        d2 = tmp_dir / "dir2"
        d1.mkdir()
        d2.mkdir()
        (d1 / "a.md").write_text("---\nname: a\ndescription: A skill\n---\nA", encoding="utf-8")
        (d2 / "b.md").write_text("---\nname: b\ndescription: B skill\n---\nB", encoding="utf-8")
        source = FileSkillSource(directories=[d1, d2])
        summaries = await source.list_skills()
        names = {s.name for s in summaries}
        assert names == {"a", "b"}

    @pytest.mark.asyncio
    async def test_cache_refresh_after_file_change(self, tmp_dir):
        path = tmp_dir / "mutant.md"
        path.write_text("---\nname: mutant\ndescription: Mutable skill\n---\nV1", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir], cache=True)
        await source.list_skills()
        skill = await source.load_skill("mutant")
        assert skill is not None
        assert skill.content == "V1"
        path.write_text("---\nname: mutant\ndescription: Mutable skill\n---\nV2", encoding="utf-8")
        source.invalidate_cache()
        await source.list_skills()
        skill2 = await source.load_skill("mutant")
        assert skill2 is not None
        assert skill2.content == "V2"

    @pytest.mark.asyncio
    async def test_load_skill_without_cache_reads_disk(self, tmp_dir):
        path = tmp_dir / "dynamic.md"
        path.write_text("---\nname: dynamic\ndescription: Dynamic skill\n---\nOld", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir], cache=False)
        skill1 = await source.load_skill("dynamic")
        assert skill1 is not None
        assert skill1.content == "Old"
        path.write_text("---\nname: dynamic\ndescription: Dynamic skill\n---\nNew", encoding="utf-8")
        skill2 = await source.load_skill("dynamic")
        assert skill2 is not None
        assert skill2.content == "New"

    @pytest.mark.asyncio
    async def test_frontmatter_resources_override_auto_resources(self, tmp_dir):
        skill_dir = tmp_dir / "override"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: override\ndescription: Override resources\nresources:\n  - name: scripts\n    type: custom\n    path: /custom\n---\nBody",
            encoding="utf-8",
        )
        (skill_dir / "scripts").mkdir()
        source = FileSkillSource(directories=[tmp_dir])
        summaries = await source.list_skills()
        res = {r.name: r for r in summaries[0].resources}
        assert res["scripts"].type == "custom"
        assert res["scripts"].path == "/custom"


class TestSkillCatalogEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_source_returns_empty_prompt(self):
        sm = SkillCatalog(source=InlineSkillSource([]))
        prompt = await sm.render_prompt()
        assert prompt == ""

    @pytest.mark.asyncio
    async def test_build_prompt_with_empty_source(self):
        # Empty source -> empty prompt
        sm = SkillCatalog(source=InlineSkillSource([]))
        prompt = await sm.render_prompt()
        assert prompt == ""

    @pytest.mark.asyncio
    async def test_inline_duplicate_last_wins_get_skill(self):
        src = InlineSkillSource(
            [Skill(name="x", content="first"), Skill(name="x", content="second")]
        )
        sm = SkillCatalog(source=src)
        skill = await sm.get_skill("x")
        assert skill is not None
        assert skill.content == "second"

    @pytest.mark.asyncio
    async def test_list_skills_deduplicates_by_name_last_wins(self):
        src = InlineSkillSource(
            [
                Skill(name="dup", content="first"),
                Skill(name="dup", content="second"),
            ]
        )
        sm = SkillCatalog(source=src)
        skills = await sm.list_skills()
        assert len(skills) == 1
        assert skills[0].content == "second"

    @pytest.mark.asyncio
    async def test_list_skills_with_none_context(self):
        src = InlineSkillSource(
            [
                Skill(name="a", content="A"),
                Skill(name="b", content="B"),
                Skill(name="c", content="C"),
            ]
        )
        sm = SkillCatalog(source=src)
        skills = await sm.list_skills(context=None)
        names = {s.name for s in skills}
        assert names == {"a", "b", "c"}


class TestSkillMetadataEdgeCases:
    def test_empty_frontmatter_uses_defaults(self) -> None:
        metadata = SkillMetadata.from_frontmatter({})

        assert metadata.disable_model_invocation is False
        assert metadata.extra == {}

    def test_nested_unknown_fields_are_preserved_without_interpretation(self) -> None:
        requires = {"tools": ["explicit"], "env": ["API_KEY"]}

        metadata = SkillMetadata.from_frontmatter({"requires": requires})

        assert metadata.extra == {"requires": requires}


class TestBuilderEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_skills_returns_empty(self):
        assert await DefaultSkillBuilder().build([]) == ""

    @pytest.mark.asyncio
    async def test_never_inlines_body_content(self):
        """Content must never appear in the metadata-only system section."""
        skills = [Skill(name="s", content="SECRET_BODY")]
        out = await DefaultSkillBuilder().build(skills)
        assert "<available_skills>" in out
        assert "SECRET_BODY" not in out

    @pytest.mark.asyncio
    async def test_context_is_ignored(self):
        """Context parameter is accepted for backward compat but has no effect."""
        skills = [Skill(name="s", content="body", location="/tmp/s.md")]
        out = await DefaultSkillBuilder().build(skills, None)
        assert 'name="s"' in out
        assert "body" not in out

    @pytest.mark.asyncio
    async def test_skill_without_location(self):
        skills = [Skill(name="s", description="d", content="body")]
        out = await DefaultSkillBuilder().build(skills)
        assert 'name="s"' in out
        assert "<description>d</description>" in out
        assert "body" not in out


class TestSkillSummaryEdgeCases:
    def test_to_skill_preserves_all_fields(self):
        summary = SkillSummary(
            name="s",
            description="d",
            metadata=SkillMetadata(disable_model_invocation=True),
            source="src",
            location="loc",
            resources=(SkillResource(name="r", type="t"),),
        )
        skill = summary.to_skill("content")
        assert skill.name == "s"
        assert skill.description == "d"
        assert skill.metadata.disable_model_invocation is True
        assert skill.source == "src"
        assert skill.location == "loc"
        assert len(skill.resources) == 1
        assert skill.content == "content"

    def test_to_skill_resources_are_frozen(self):
        summary = SkillSummary(name="s", resources=(SkillResource(name="r", type="t"),))
        skill = summary.to_skill("c")
        # Pydantic frozen model: the resources tuple is immutable; a new
        # skill cannot be mutated through the shared summary.
        with pytest.raises(ValidationError):
            skill.resources[0].name = "mutated"  # type: ignore[misc]
        assert len(summary.resources) == 1
