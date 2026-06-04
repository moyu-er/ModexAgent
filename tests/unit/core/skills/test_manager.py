"""Unit tests for core/skills/manager.py."""

import tempfile
from pathlib import Path

import pytest

from framework.core.skills.cache import DirectorySkillCache
from framework.core.skills.filter import AllowListFilter
from framework.core.skills.builder import DefaultSkillBuilder
from framework.core.skills.manager import SkillManager
from framework.core.skills.models import ResolutionContext, Skill
from framework.core.skills.source import FileSkillSource, InlineSkillSource


class TestSkillManager:
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
    def manager(self, source):
        return SkillManager(source=source)

    @pytest.mark.asyncio
    async def test_default_builder_is_progressive(self, source):
        sm = SkillManager(source=source)
        assert isinstance(sm._builder, DefaultSkillBuilder)

    @pytest.mark.asyncio
    async def test_list_skills_no_cache_reloads_from_source(self, manager):
        skills = await manager.list_skills()
        assert len(skills) == 2
        skills2 = await manager.list_skills()
        assert len(skills2) == 2

    @pytest.mark.asyncio
    async def test_invalidate_is_noop_when_no_cache(self, manager):
        manager.invalidate()  # should not raise

    @pytest.mark.asyncio
    async def test_override_precedence(self, manager):
        override = Skill(name="a", content="overridden")
        await manager.register_skill(override)
        skills = await manager.list_skills()
        by_name = {s.name: s for s in skills}
        assert by_name["a"].content == "overridden"
        assert by_name["b"].content == "bc"

    @pytest.mark.asyncio
    async def test_clear_overrides(self, manager):
        await manager.register_skill(Skill(name="a", content="overridden"))
        manager.clear_overrides()
        skills = await manager.list_skills()
        by_name = {s.name: s for s in skills}
        assert by_name["a"].content == "ac"

    @pytest.mark.asyncio
    async def test_unregister_skill(self, manager):
        await manager.register_skill(Skill(name="a", content="overridden"))
        assert await manager.unregister_skill("a") is True
        skills = await manager.list_skills()
        by_name = {s.name: s for s in skills}
        assert by_name["a"].content == "ac"
        assert await manager.unregister_skill("z") is False

    @pytest.mark.asyncio
    async def test_list_skills_applies_filter(self, source):
        sm = SkillManager(source=source, skill_filter=AllowListFilter({"a"}))
        skills = await sm.list_skills()
        assert [s.name for s in skills] == ["a"]

    @pytest.mark.asyncio
    async def test_build_prompt_uses_builder(self, source):
        sm = SkillManager(source=source, builder=DefaultSkillBuilder())
        prompt = await sm.build_prompt()
        assert "<available_skills>" in prompt
        assert 'name="a"' in prompt
        assert 'name="b"' in prompt

    @pytest.mark.asyncio
    async def test_get_skill_from_source(self, manager):
        skill = await manager.get_skill("a")
        assert skill is not None
        assert skill.content == "ac"

    @pytest.mark.asyncio
    async def test_get_skill_from_overrides(self, manager):
        await manager.register_skill(Skill(name="a", content="overridden"))
        skill = await manager.get_skill("a")
        assert skill is not None
        assert skill.content == "overridden"

    @pytest.mark.asyncio
    async def test_get_skill_missing(self, manager):
        assert await manager.get_skill("z") is None

    @pytest.mark.asyncio
    async def test_deduplicate_last_wins(self):
        src = InlineSkillSource(
            [Skill(name="a", content="v1"), Skill(name="a", content="v2")],
            name="inline",
        )
        sm = SkillManager(source=src)
        skills = await sm.list_skills()
        assert len(skills) == 1
        assert skills[0].content == "v2"


class TestSkillManagerWithDirectoryCache:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def _add_skill(self, parent: Path, name: str, content: str = "") -> Path:
        d = parent / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\n---\n{content}", encoding="utf-8",
        )
        return d

    @pytest.mark.asyncio
    async def test_with_cache_detects_new_skill(self, tmp_dir):
        self._add_skill(tmp_dir, "alpha")
        source = FileSkillSource(directories=[tmp_dir], cache=True, layout="directory")
        cache = DirectorySkillCache(directories=[tmp_dir], layout="directory")
        sm = SkillManager(source=source, cache=cache)

        skills = await sm.list_skills()
        assert {s.name for s in skills} == {"alpha"}

        self._add_skill(tmp_dir, "beta")
        skills = await sm.list_skills()
        assert {s.name for s in skills} == {"alpha", "beta"}

    @pytest.mark.asyncio
    async def test_overrides_work_with_cache(self, tmp_dir):
        self._add_skill(tmp_dir, "alpha")
        source = FileSkillSource(directories=[tmp_dir], cache=True, layout="directory")
        cache = DirectorySkillCache(directories=[tmp_dir], layout="directory")
        sm = SkillManager(source=source, cache=cache)

        await sm.register_skill(Skill(name="alpha", content="overridden"))
        skills = await sm.list_skills()
        alpha = next(s for s in skills if s.name == "alpha")
        assert alpha.content == "overridden"

    @pytest.mark.asyncio
    async def test_invalidate_delegates_to_cache(self, tmp_dir):
        self._add_skill(tmp_dir, "alpha")
        source = FileSkillSource(directories=[tmp_dir], cache=True, layout="directory")
        cache = DirectorySkillCache(directories=[tmp_dir], layout="directory")
        sm = SkillManager(source=source, cache=cache)

        await sm.list_skills()
        assert len(cache._dir_states) > 0
        sm.invalidate()
        assert cache._dir_states == {}
