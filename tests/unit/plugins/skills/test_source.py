"""Contract tests for the Skills capability source adapters."""

import tempfile
from enum import StrEnum
from pathlib import Path

import pytest

from modex_agent.plugins.defaults.capabilities.skills.models import (
    Skill,
    SkillResource,
)
from modex_agent.plugins.defaults.capabilities.skills.source import (
    CompositeSkillSource,
    FileSkillSource,
    InlineSkillSource,
    SkillLayout,
    SkillMergeStrategy,
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
            "---\nname: demo-skill\ndescription: Demo\n---\nDemo content", encoding="utf-8"
        )
        source = FileSkillSource(directories=[tmp_dir])
        summaries = await source.list_skills()
        assert isinstance(summaries, tuple)
        assert len(summaries) == 1
        assert summaries[0].name == "demo-skill"
        assert summaries[0].location is not None
        assert Path(summaries[0].location).resolve() == (skill_dir / "SKILL.md").resolve()

    @pytest.mark.asyncio
    async def test_list_skills_skips_missing_description(self, tmp_dir, caplog):
        skill_dir = tmp_dir / "missing-description"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: missing-description\n---\nBody", encoding="utf-8"
        )

        summaries = await FileSkillSource(directories=[tmp_dir]).list_skills()

        assert summaries == ()
        assert "description" in caplog.text
        assert str(skill_dir / "SKILL.md") in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name",
        ["has_underscore", "Uppercase", "-leading", "trailing-", "two--hyphens"],
    )
    async def test_list_skills_skips_invalid_skill_name(self, tmp_dir, caplog, name):
        skill_dir = tmp_dir / "candidate"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Candidate\n---\nBody", encoding="utf-8"
        )

        summaries = await FileSkillSource(directories=[tmp_dir]).list_skills()

        assert summaries == ()
        assert "name" in caplog.text

    @pytest.mark.asyncio
    async def test_list_skills_skips_non_string_name(self, tmp_dir, caplog):
        skill_dir = tmp_dir / "candidate"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: 123\ndescription: Candidate\n---\nBody", encoding="utf-8"
        )

        summaries = await FileSkillSource(directories=[tmp_dir]).list_skills()

        assert summaries == ()
        assert "name" in caplog.text

    @pytest.mark.asyncio
    async def test_list_skills_skips_overlong_description(self, tmp_dir, caplog):
        skill_dir = tmp_dir / "candidate"
        skill_dir.mkdir()
        description = "x" * 1025
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: candidate\ndescription: {description}\n---\nBody", encoding="utf-8"
        )

        summaries = await FileSkillSource(directories=[tmp_dir]).list_skills()

        assert summaries == ()
        assert "description" in caplog.text

    @pytest.mark.asyncio
    async def test_load_skill_never_includes_bom_frontmatter(self, tmp_dir):
        skill_dir = tmp_dir / "weather"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "\ufeff---\r\nname: weather\r\ndescription: Forecasts\r\n---\r\n# Weather\r\nBody\r\n",
            encoding="utf-8",
        )
        source = FileSkillSource(directories=[tmp_dir], layout=SkillLayout.DIRECTORY)

        skill = await source.load_skill("weather")

        assert skill is not None
        assert skill.content == "# Weather\nBody\n"
        assert "name:" not in skill.content
        assert "description:" not in skill.content

    @pytest.mark.asyncio
    async def test_list_skills_auto_scans_resources(self, tmp_dir):
        skill_dir = tmp_dir / "auto"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: auto\ndescription: Auto resources\n---\ncontent", encoding="utf-8"
        )
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
            "---\nname: cached-skill\ndescription: orig\n---\nBody", encoding="utf-8"
        )
        source = FileSkillSource(directories=[tmp_dir], cache=True)
        await source.list_skills()
        skill = await source.load_skill("cached-skill")
        assert skill is not None
        assert skill.name == "cached-skill"
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
            "---\nname: loc-skill\ndescription: Located skill\nlocation: fake/path\n---\nBody", encoding="utf-8"
        )
        source = FileSkillSource(directories=[tmp_dir], cache=True)
        await source.list_skills()
        skill = await source.load_skill("loc-skill")
        assert skill is not None
        # location should come from summary (real path), not frontmatter
        assert skill.location is not None
        assert str(skill_dir / "SKILL.md") in skill.location

    @pytest.mark.asyncio
    async def test_directory_layout_ignores_loose_files(self, tmp_dir):
        (tmp_dir / "notes.md").write_text("Not a skill", encoding="utf-8")
        skill_dir = tmp_dir / "demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: Demo\n---\nBody", encoding="utf-8"
        )
        source = FileSkillSource(
            directories=[tmp_dir], layout=SkillLayout.DIRECTORY
        )
        summaries = await source.list_skills()
        assert len(summaries) == 1
        assert summaries[0].name == "demo"
        assert summaries[0].location is not None
        assert Path(summaries[0].location).resolve() == (skill_dir / "SKILL.md").resolve()

    @pytest.mark.asyncio
    async def test_directory_layout_uses_frontmatter_name(self, tmp_dir):
        skill_dir = tmp_dir / "my_skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: override-name\ndescription: Override\n---\nBody",
            encoding="utf-8",
        )
        source = FileSkillSource(
            directories=[tmp_dir], layout=SkillLayout.DIRECTORY
        )
        summaries = await source.list_skills()
        assert len(summaries) == 1
        assert summaries[0].name == "override-name"

    @pytest.mark.asyncio
    async def test_custom_skill_filename(self, tmp_dir):
        skill_dir = tmp_dir / "demo"
        skill_dir.mkdir()
        (skill_dir / "custom.md").write_text(
            "---\ndescription: Custom file\n---\nBody", encoding="utf-8"
        )
        source = FileSkillSource(
            directories=[tmp_dir],
            layout=SkillLayout.DIRECTORY,
            skill_filename="custom.md",
        )
        summaries = await source.list_skills()
        assert len(summaries) == 1
        assert summaries[0].name == "demo"

    @pytest.mark.asyncio
    async def test_custom_exclude_names(self, tmp_dir):
        (tmp_dir / "changelog.md").write_text("---\n---\nBody", encoding="utf-8")
        (tmp_dir / "real.md").write_text(
            "---\ndescription: Real\n---\nBody", encoding="utf-8"
        )
        source = FileSkillSource(directories=[tmp_dir], exclude_names=("changelog.md", "readme.md"))
        summaries = await source.list_skills()
        names = {s.name for s in summaries}
        assert "changelog" not in names
        assert "real" in names

    @pytest.mark.asyncio
    async def test_custom_resource_dirs(self, tmp_dir):
        skill_dir = tmp_dir / "demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: resources\ndescription: Resources\n---\nBody", encoding="utf-8"
        )
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
        source = FileSkillSource(
            directories=[tmp_dir], layout=SkillLayout.DIRECTORY
        )
        names = source.list_skill_names(tmp_dir)
        assert names == {"alpha", "beta"}

    @pytest.mark.asyncio
    async def test_list_skill_names_flat_layout(self, tmp_dir):
        (tmp_dir / "weather.md").write_text("content", encoding="utf-8")
        (tmp_dir / "cron.md").write_text("content", encoding="utf-8")
        (tmp_dir / "readme.md").write_text("content", encoding="utf-8")
        source = FileSkillSource(directories=[tmp_dir], layout=SkillLayout.FLAT)
        names = source.list_skill_names(tmp_dir)
        assert names == {"weather", "cron"}

    @pytest.mark.asyncio
    async def test_list_skill_names_missing_directory(self, tmp_dir):
        source = FileSkillSource(directories=[tmp_dir])
        names = source.list_skill_names(tmp_dir / "nope")
        assert names == set()

    @pytest.mark.asyncio
    async def test_invalidate_cache_then_reload(self, tmp_dir):
        skill_dir = tmp_dir / "demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: Demo\n---\nv1", encoding="utf-8"
        )
        source = FileSkillSource(directories=[tmp_dir], cache=True)
        skills = await source.load()
        assert skills[0].content == "v1"

        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo\ndescription: Demo\n---\nv2", encoding="utf-8"
        )
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
        source = FileSkillSource(directories=[tmp_dir], layout=SkillLayout.FLAT)
        assert source.layout is SkillLayout.FLAT

    def test_layout_is_typed_and_rejects_unknown_values(self, tmp_dir: Path) -> None:
        assert issubclass(SkillLayout, StrEnum)
        with pytest.raises(ValueError, match="not a valid SkillLayout"):
            FileSkillSource(directories=[tmp_dir], layout="nested")


class TestCompositeSkillSource:
    def test_merge_strategy_is_typed_and_rejects_unknown_values(self) -> None:
        assert issubclass(SkillMergeStrategy, StrEnum)
        with pytest.raises(ValueError, match="not a valid SkillMergeStrategy"):
            CompositeSkillSource([], merge_strategy="overlay")

    @pytest.mark.asyncio
    async def test_last_wins_deduplication(self):
        s1 = InlineSkillSource(
            [Skill(name="a", content="from_s1")], name="s1"
        )
        s2 = InlineSkillSource(
            [Skill(name="a", content="from_s2")], name="s2"
        )
        composite = CompositeSkillSource(
            [s1, s2], merge_strategy=SkillMergeStrategy.LAST_WINS
        )
        summaries = await composite.list_skills()
        assert isinstance(summaries, tuple)
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
        composite = CompositeSkillSource(
            [s1, s2], merge_strategy=SkillMergeStrategy.FIRST_WINS
        )
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
        composite = CompositeSkillSource(
            [s1, s2], merge_strategy=SkillMergeStrategy.ERROR
        )
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
        composite = CompositeSkillSource(
            [s1, s2], merge_strategy=SkillMergeStrategy.ERROR
        )
        with pytest.raises(ValueError, match="Duplicate skill 'a'"):
            await composite.load_skill("a")

    @pytest.mark.asyncio
    async def test_load_skill_ignores_missing(self):
        s1 = InlineSkillSource([], name="s1")
        composite = CompositeSkillSource([s1])
        assert await composite.load_skill("missing") is None

    @pytest.mark.asyncio
    async def test_list_resources_follows_winning_source(self):
        first = InlineSkillSource(
            [
                Skill(
                    name="a",
                    resources=(SkillResource(name="first", type="reference"),),
                )
            ],
            name="first",
        )
        last = InlineSkillSource(
            [
                Skill(
                    name="a",
                    resources=(SkillResource(name="last", type="reference"),),
                )
            ],
            name="last",
        )
        composite = CompositeSkillSource(
            [first, last], merge_strategy=SkillMergeStrategy.LAST_WINS
        )

        resources = await composite.list_resources("a")

        assert resources == (SkillResource(name="last", type="reference"),)


class TestInlineSkillSource:
    @pytest.mark.asyncio
    async def test_list_and_load(self):
        source = InlineSkillSource(
            [Skill(name="x", content="c")], name="inline"
        )
        summaries = await source.list_skills()
        assert isinstance(summaries, tuple)
        assert len(summaries) == 1
        skill = await source.load_skill("x")
        assert skill is not None
        assert skill.content == "c"

    @pytest.mark.asyncio
    async def test_list_resources_returns_skill_resources(self):
        resource = SkillResource(name="guide", type="reference")
        source = InlineSkillSource(
            [Skill(name="x", content="c", resources=(resource,))],
            name="inline",
        )

        assert await source.list_resources("x") == (resource,)
        assert await source.list_resources("missing") == ()
