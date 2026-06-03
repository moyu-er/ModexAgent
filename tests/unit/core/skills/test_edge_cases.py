"""Edge-case unit tests for the skill system."""

import tempfile
from pathlib import Path

import pytest

from framework.core.skills.builder import (
    HybridBuilder,
    InlineBuilder,
    ProgressiveBuilder,
)
from framework.core.skills.manager import SkillManager
from framework.core.skills.models import (
    ResolutionContext,
    Skill,
    SkillMetadata,
    SkillResource,
    SkillSummary,
)
from framework.core.skills.source import FileSkillSource, InlineSkillSource


class TestFileSkillSourceEdgeCases:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    @pytest.mark.asyncio
    async def test_ignores_readme_and_index(self, tmp_dir):
        (tmp_dir / "README.md").write_text("---\nname: readme\n---\nBody", encoding="utf-8")
        (tmp_dir / "index.md").write_text("---\nname: index\n---\nBody", encoding="utf-8")
        (tmp_dir / "SKILL.md").write_text("---\nname: real\n---\nBody", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir])
        summaries = await source.list_skills()
        assert len(summaries) == 1
        assert summaries[0].name == "real"

    @pytest.mark.asyncio
    async def test_malformed_frontmatter_gracefully_degrades(self, tmp_dir):
        (tmp_dir / "bad.md").write_text("---\nnot_yaml: [\n---\nBody", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir])
        summaries = await source.list_skills()
        # Malformed frontmatter is caught and treated as empty; file is still discovered
        assert len(summaries) == 1
        assert summaries[0].name == "bad"
        assert summaries[0].description == ""

    @pytest.mark.asyncio
    async def test_no_frontmatter_uses_filename_as_name(self, tmp_dir):
        (tmp_dir / "implicit.md").write_text("No frontmatter here", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir])
        summaries = await source.list_skills()
        assert len(summaries) == 1
        assert summaries[0].name == "implicit"

    @pytest.mark.asyncio
    async def test_nested_directory_discovery(self, tmp_dir):
        nested = tmp_dir / "deep" / "nested"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text("---\nname: nested_skill\n---\nBody", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir])
        summaries = await source.list_skills()
        assert len(summaries) == 1
        assert summaries[0].name == "nested_skill"

    @pytest.mark.asyncio
    async def test_multiple_directories_merge(self, tmp_dir):
        d1 = tmp_dir / "dir1"
        d2 = tmp_dir / "dir2"
        d1.mkdir()
        d2.mkdir()
        (d1 / "a.md").write_text("---\nname: a\n---\nA", encoding="utf-8")
        (d2 / "b.md").write_text("---\nname: b\n---\nB", encoding="utf-8")
        source = FileSkillSource(directories=[d1, d2])
        summaries = await source.list_skills()
        names = {s.name for s in summaries}
        assert names == {"a", "b"}

    @pytest.mark.asyncio
    async def test_cache_refresh_after_file_change(self, tmp_dir):
        path = tmp_dir / "mutant.md"
        path.write_text("---\nname: mutant\n---\nV1", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir], cache=True)
        await source.list_skills()
        skill = await source.load_skill("mutant")
        assert skill is not None
        assert skill.content == "V1"
        # Manually clear cache to simulate refresh
        source._listing = None
        source._summary_map.clear()
        source._contents.clear()
        path.write_text("---\nname: mutant\n---\nV2", encoding="utf-8")
        await source.list_skills()
        skill2 = await source.load_skill("mutant")
        assert skill2 is not None
        assert skill2.content == "V2"

    @pytest.mark.asyncio
    async def test_load_skill_without_cache_reads_disk(self, tmp_dir):
        path = tmp_dir / "dynamic.md"
        path.write_text("---\nname: dynamic\n---\nOld", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir], cache=False)
        skill1 = await source.load_skill("dynamic")
        assert skill1 is not None
        assert skill1.content == "Old"
        path.write_text("---\nname: dynamic\n---\nNew", encoding="utf-8")
        skill2 = await source.load_skill("dynamic")
        assert skill2 is not None
        assert skill2.content == "New"

    @pytest.mark.asyncio
    async def test_frontmatter_resources_override_auto_resources(self, tmp_dir):
        skill_dir = tmp_dir / "override"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: override\nresources:\n  - name: scripts\n    type: custom\n    path: /custom\n---\nBody",
            encoding="utf-8",
        )
        (skill_dir / "scripts").mkdir()
        source = FileSkillSource(directories=[tmp_dir])
        summaries = await source.list_skills()
        res = {r.name: r for r in summaries[0].resources}
        assert res["scripts"].type == "custom"
        assert res["scripts"].path == "/custom"


class TestSkillManagerEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_source_returns_empty_prompt(self):
        sm = SkillManager(source=InlineSkillSource([]))
        prompt = await sm.build_prompt()
        assert prompt == ""

    @pytest.mark.asyncio
    async def test_build_prompt_with_empty_source(self):
        # Empty source -> empty prompt
        sm = SkillManager(source=InlineSkillSource([]))
        prompt = await sm.build_prompt()
        assert prompt == ""

    @pytest.mark.asyncio
    async def test_register_duplicate_overrides_previous(self):
        sm = SkillManager(source=InlineSkillSource([]))
        await sm.register_skill(Skill(name="x", content="first"))
        await sm.register_skill(Skill(name="x", content="second"))
        skill = await sm.get_skill("x")
        assert skill is not None
        assert skill.content == "second"

    @pytest.mark.asyncio
    async def test_override_then_clear_restores_source(self):
        src = InlineSkillSource([Skill(name="x", content="orig")])
        sm = SkillManager(source=src)
        await sm.register_skill(Skill(name="x", content="ovr"))
        sm.clear_overrides()
        skill = await sm.get_skill("x")
        assert skill is not None
        assert skill.content == "orig"

    @pytest.mark.asyncio
    async def test_list_skills_deduplicates_by_name_last_wins(self):
        src = InlineSkillSource(
            [
                Skill(name="dup", content="first"),
                Skill(name="dup", content="second"),
            ]
        )
        sm = SkillManager(source=src)
        skills = await sm.list_skills()
        assert len(skills) == 1
        assert skills[0].content == "second"

    @pytest.mark.asyncio
    async def test_override_takes_precedence_over_source_duplicate(self):
        src = InlineSkillSource(
            [
                Skill(name="dup", content="first"),
                Skill(name="dup", content="second"),
            ]
        )
        sm = SkillManager(source=src)
        await sm.register_skill(Skill(name="dup", content="override"))
        skills = await sm.list_skills()
        assert len(skills) == 1
        assert skills[0].content == "override"

    @pytest.mark.asyncio
    async def test_list_skills_with_none_context(self):
        src = InlineSkillSource(
            [
                Skill(name="a", content="A"),
                Skill(name="b", content="B"),
                Skill(name="c", content="C"),
            ]
        )
        sm = SkillManager(source=src)
        skills = await sm.list_skills(context=None)
        names = {s.name for s in skills}
        assert names == {"a", "b", "c"}


class TestSkillMetadataEdgeCases:
    def test_from_dict_with_none_input(self):
        meta = SkillMetadata.from_dict(None)  # type: ignore[arg-type]
        assert meta.requires_tools == []
        assert meta.extra == {}

    def test_from_dict_with_empty_string_metadata(self):
        meta = SkillMetadata.from_dict({"metadata": ""})
        assert meta.extra == {}

    def test_from_dict_with_nanobot_and_requires(self):
        data = {
            "metadata": '{"nanobot": {"requires": {"tools": ["t1"], "env": ["E1"]}}}',
        }
        meta = SkillMetadata.from_dict(data)
        assert meta.requires_tools == ["t1"]
        assert meta.requires_env == ["E1"]

    def test_from_dict_prefers_explicit_over_nested(self):
        data = {
            "requires_tools": ["explicit"],
            "requires": {"tools": ["nested"]},
        }
        meta = SkillMetadata.from_dict(data)
        assert meta.requires_tools == ["explicit"]

    def test_from_dict_collects_unknown_to_extra(self):
        data = {"custom_field": {"nested": 1}, "number": 42}
        meta = SkillMetadata.from_dict(data)
        assert meta.extra["custom_field"] == {"nested": 1}
        assert meta.extra["number"] == 42


class TestBuilderEdgeCases:
    @pytest.mark.asyncio
    async def test_inline_builder_empty_skills(self):
        b = InlineBuilder()
        assert await b.build([]) == ""

    @pytest.mark.asyncio
    async def test_progressive_builder_empty_skills(self):
        b = ProgressiveBuilder()
        assert await b.build([]) == ""

    @pytest.mark.asyncio
    async def test_progressive_builder_downgrade_when_no_read_tool(self):
        tm = object()  # no has_tool method
        ctx = ResolutionContext(tool_manager=tm)
        skills = [Skill(name="s", content="body")]
        b = ProgressiveBuilder()
        out = await b.build(skills, ctx)
        assert "body" in out
        assert "| Skill | Description | Location |" not in out

    @pytest.mark.asyncio
    async def test_hybrid_builder_all_inline_mode(self):
        skills = [
            Skill(name="a", content="A", metadata=SkillMetadata(always=False)),
            Skill(name="b", content="B", metadata=SkillMetadata(always=True)),
        ]
        b = HybridBuilder(inline_mode="all")
        out = await b.build(skills)
        assert "### a" in out and "### b" in out

    @pytest.mark.asyncio
    async def test_hybrid_builder_none_mode_with_read_tool(self):
        class FakeTM:
            def has_tool(self, _name):
                return True
        ctx = ResolutionContext(tool_manager=FakeTM())
        skills = [Skill(name="x", content="body", location="/x.md")]
        b = HybridBuilder(inline_mode="none")
        out = await b.build(skills, ctx)
        assert '<skill name="x"' in out
        assert "body" not in out

    @pytest.mark.asyncio
    async def test_hybrid_builder_none_mode_downgrades_without_read_tool(self):
        skills = [Skill(name="x", content="body")]
        b = HybridBuilder(inline_mode="none")
        out = await b.build(skills, None)
        assert "body" in out
        assert "| Skill | Description | Location |" not in out

    @pytest.mark.asyncio
    async def test_hybrid_builder_always_mode_no_always_skills(self):
        skills = [Skill(name="x", content="body", location="/x.md")]
        b = HybridBuilder(inline_mode="always")
        out = await b.build(skills, None)
        assert "### x" in out
        assert "body" in out

    @pytest.mark.asyncio
    async def test_hybrid_builder_always_mode_all_always_skills(self):
        skills = [Skill(name="x", content="body", metadata=SkillMetadata(always=True))]
        b = HybridBuilder(inline_mode="always")
        out = await b.build(skills)
        assert "### x" in out
        assert "body" in out


class TestSkillSummaryEdgeCases:
    def test_to_skill_preserves_all_fields(self):
        summary = SkillSummary(
            name="s",
            description="d",
            metadata=SkillMetadata(always=True),
            source="src",
            location="loc",
            resources=[SkillResource(name="r", type="t")],
        )
        skill = summary.to_skill("content")
        assert skill.name == "s"
        assert skill.description == "d"
        assert skill.metadata.always is True
        assert skill.source == "src"
        assert skill.location == "loc"
        assert len(skill.resources) == 1
        assert skill.content == "content"

    def test_to_skill_creates_independent_resources_copy(self):
        res = [SkillResource(name="r", type="t")]
        summary = SkillSummary(name="s", resources=res)
        skill = summary.to_skill("c")
        skill.resources.append(SkillResource(name="x", type="y"))
        assert len(summary.resources) == 1
