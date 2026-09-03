"""Unit tests for the skills capability catalog (plan §11.1, §18.8)."""

import shutil
import tempfile
from pathlib import Path

import pytest

from modex_agent.plugins.defaults.capabilities.skills.builder import DefaultSkillBuilder
from modex_agent.plugins.defaults.capabilities.skills.cache import DirectorySkillCache
from modex_agent.plugins.defaults.capabilities.skills.catalog import SkillCatalog
from modex_agent.plugins.defaults.capabilities.skills.filter import AllowListFilter
from modex_agent.plugins.defaults.capabilities.skills.models import Skill
from modex_agent.plugins.defaults.capabilities.skills.section import SkillSectionProvider
from modex_agent.plugins.defaults.capabilities.skills.source import (
    FileSkillSource,
    InlineSkillSource,
    SkillLayout,
)


class TestSkillCatalog:
    @pytest.fixture
    def source(self):
        return InlineSkillSource(
            [
                Skill(name="a", content="ac"),
                Skill(name="b", content="bc"),
            ],
            name="inline",
        )

    @pytest.fixture
    def catalog(self, source):
        return SkillCatalog(source=source)

    @pytest.mark.asyncio
    async def test_default_builder_renders_compact_xml(self, source):
        catalog = SkillCatalog(source=source)
        prompt = await catalog.render_prompt()
        assert "<available_skills>" in prompt
        assert "<skill>\nac" not in prompt

    @pytest.mark.asyncio
    async def test_list_skills_no_cache_reloads_from_source(self, catalog):
        skills = await catalog.list_skills()
        assert isinstance(skills, tuple)
        assert len(skills) == 2
        skills2 = await catalog.list_skills()
        assert len(skills2) == 2

    @pytest.mark.asyncio
    async def test_invalidate_is_noop_when_no_cache(self, catalog):
        catalog.invalidate()  # should not raise

    @pytest.mark.asyncio
    async def test_list_skills_applies_filter(self, source):
        catalog = SkillCatalog(source=source, skill_filter=AllowListFilter({"a"}))
        skills = await catalog.list_skills()
        assert [s.name for s in skills] == ["a"]

    @pytest.mark.asyncio
    async def test_render_prompt_uses_builder(self, source):
        catalog = SkillCatalog(source=source, builder=DefaultSkillBuilder())
        prompt = await catalog.render_prompt()
        assert "<available_skills>" in prompt
        assert 'name="a"' in prompt
        assert 'name="b"' in prompt

    @pytest.mark.asyncio
    async def test_render_prompt_empty_source_is_empty(self):
        catalog = SkillCatalog(source=InlineSkillSource([], name="inline"))
        assert await catalog.render_prompt() == ""

    @pytest.mark.asyncio
    async def test_get_skill_from_source(self, catalog):
        skill = await catalog.get_skill("a")
        assert skill is not None
        assert skill.content == "ac"

    @pytest.mark.asyncio
    async def test_get_skill_missing(self, catalog):
        assert await catalog.get_skill("z") is None

    @pytest.mark.asyncio
    async def test_deduplicate_last_wins(self):
        src = InlineSkillSource(
            [Skill(name="a", content="v1"), Skill(name="a", content="v2")],
            name="inline",
        )
        catalog = SkillCatalog(source=src)
        skills = await catalog.list_skills()
        assert len(skills) == 1
        assert skills[0].content == "v2"

    @pytest.mark.asyncio
    async def test_resolve_command_renders_canonical_xml(self, catalog):
        resolved = await catalog.resolve_command("a", "some args")
        assert resolved is not None
        assert resolved.skill_name == "a"
        assert "<command_context type=\"skill\" name=\"a\">" in resolved.xml
        assert "<user_input>\nsome args\n</user_input>" in resolved.xml

    @pytest.mark.asyncio
    async def test_resolve_command_unknown_skill_is_none(self, catalog):
        assert await catalog.resolve_command("z", "") is None

    @pytest.mark.asyncio
    async def test_resolve_command_inline_skill_has_no_directory_attr(self, catalog):
        resolved = await catalog.resolve_command("a", "")
        assert resolved is not None
        assert "directory=" not in resolved.xml
        assert resolved.skill_location is None

    @pytest.mark.asyncio
    async def test_list_resources_proxies_source(self):
        skill = Skill(name="a", content="ac")
        catalog = SkillCatalog(source=InlineSkillSource([skill], name="inline"))
        assert await catalog.list_resources("a") == ()


class TestSkillCatalogWithDirectoryCache:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def _add_skill(
        self,
        parent: Path,
        name: str,
        content: str = "",
        description: str = "",
    ) -> Path:
        d = parent / name
        d.mkdir(parents=True, exist_ok=True)
        description_line = f"description: {description}\n" if description else ""
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\n{description_line}---\n{content}",
            encoding="utf-8",
        )
        return d

    def _make_catalog(self, root: Path) -> SkillCatalog:
        source = FileSkillSource(
            directories=[root], cache=True, layout=SkillLayout.DIRECTORY
        )
        cache = DirectorySkillCache(
            directories=[root], layout=SkillLayout.DIRECTORY
        )
        return SkillCatalog(source=source, cache=cache)

    @pytest.mark.asyncio
    async def test_with_cache_detects_new_skill(self, tmp_dir):
        self._add_skill(tmp_dir, "alpha")
        catalog = self._make_catalog(tmp_dir)

        skills = await catalog.list_skills()
        assert {s.name for s in skills} == {"alpha"}

        self._add_skill(tmp_dir, "beta")
        skills = await catalog.list_skills()
        assert {s.name for s in skills} == {"alpha", "beta"}

    @pytest.mark.asyncio
    async def test_prompt_section_refreshes_on_new_skill(self, tmp_dir):
        self._add_skill(tmp_dir, "alpha")
        catalog = self._make_catalog(tmp_dir)

        first = await catalog.render_prompt()
        assert 'name="alpha"' in first

        self._add_skill(tmp_dir, "beta")
        second = await catalog.render_prompt()
        assert 'name="beta"' in second

    async def test_section_provider_keeps_prompt_and_commands_aligned_with_disk(
        self,
        tmp_dir: Path,
    ) -> None:
        alpha_dir = self._add_skill(tmp_dir, "alpha", "alpha instructions")
        catalog = self._make_catalog(tmp_dir)
        provider = SkillSectionProvider(catalog)

        first = await provider.get_or_refresh()
        first_version = provider.last_version
        assert 'name="alpha"' in first
        assert await catalog.resolve_command("alpha", "run") is not None

        self._add_skill(tmp_dir, "beta", "beta instructions")
        second = await provider.get_or_refresh()
        second_version = provider.last_version
        assert 'name="beta"' in second
        assert second_version != first_version
        assert await catalog.resolve_command("beta", "run") is not None

        shutil.rmtree(alpha_dir)
        third = await provider.get_or_refresh()
        assert 'name="alpha"' not in third
        assert provider.last_version != second_version
        assert await catalog.resolve_command("alpha", "run") is None

    @pytest.mark.asyncio
    async def test_invalidate_clears_cached_prompt(self, tmp_dir):
        self._add_skill(tmp_dir, "alpha")
        catalog = self._make_catalog(tmp_dir)

        assert await catalog.render_prompt()
        catalog.invalidate()
        # A fresh render after invalidate still sees the same disk state.
        assert 'name="alpha"' in await catalog.render_prompt()

    @pytest.mark.asyncio
    async def test_get_skill_detects_newly_added_skill(self, tmp_dir):
        """get_skill must see a skill file added after the first call,
        reusing the same name-set freshness check render_prompt uses."""
        self._add_skill(tmp_dir, "alpha")
        catalog = self._make_catalog(tmp_dir)

        assert await catalog.get_skill("alpha") is not None
        assert await catalog.get_skill("beta") is None  # not yet added

        self._add_skill(tmp_dir, "beta", "beta content")
        beta = await catalog.get_skill("beta")
        assert beta is not None
        assert beta.content == "beta content"

    @pytest.mark.asyncio
    async def test_get_skill_detects_removed_skill(self, tmp_dir):
        """get_skill must return None after a skill file is deleted."""
        d = self._add_skill(tmp_dir, "alpha")
        catalog = self._make_catalog(tmp_dir)

        assert await catalog.get_skill("alpha") is not None

        shutil.rmtree(d)
        assert await catalog.get_skill("alpha") is None

    @pytest.mark.asyncio
    async def test_resolve_command_on_disk_skill_carries_directory(self, tmp_dir):
        d = self._add_skill(tmp_dir, "alpha", "instructions here")
        catalog = self._make_catalog(tmp_dir)

        resolved = await catalog.resolve_command("alpha", "do it")
        assert resolved is not None
        assert 'directory=' in resolved.xml
        assert str(d) == resolved.skill_location or resolved.skill_location is not None

    @pytest.mark.parametrize("with_cache", [False, True])
    async def test_public_views_share_filter_and_duplicate_precedence(
        self,
        tmp_dir: Path,
        with_cache: bool,
    ) -> None:
        first = tmp_dir / "first"
        second = tmp_dir / "second"
        first_shared = self._add_skill(
            first, "shared", "first body", "first description"
        )
        (first_shared / "scripts").mkdir()
        second_shared = self._add_skill(
            second, "shared", "second body", "second description"
        )
        (second_shared / "references").mkdir()
        self._add_skill(second, "hidden", "hidden body", "hidden description")

        source = FileSkillSource(
            directories=[first, second],
            cache=True,
            layout=SkillLayout.DIRECTORY,
        )
        cache = (
            DirectorySkillCache(
                directories=[first, second], layout=SkillLayout.DIRECTORY
            )
            if with_cache
            else None
        )
        catalog = SkillCatalog(
            source=source,
            skill_filter=AllowListFilter({"shared"}),
            cache=cache,
        )

        skills = await catalog.list_skills()
        assert [(skill.name, skill.content) for skill in skills] == [
            ("shared", "second body")
        ]

        prompt = await catalog.render_prompt()
        assert "second description" in prompt
        assert "first description" not in prompt
        assert "hidden description" not in prompt

        shared = await catalog.get_skill("shared")
        assert shared is not None
        assert shared.content == "second body"
        assert await catalog.get_skill("hidden") is None

        resolved = await catalog.resolve_command("shared", "run")
        assert resolved is not None
        assert "second body" in resolved.xml
        assert await catalog.resolve_command("hidden", "run") is None

        resources = await catalog.list_resources("shared")
        assert [resource.name for resource in resources] == ["references"]
        assert await catalog.list_resources("hidden") == ()
