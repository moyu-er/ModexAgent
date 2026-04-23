"""Unit tests for core/skills/manager.py."""

import pytest

from framework.core.skills.builder import InlineBuilder, ProgressiveBuilder
from framework.core.skills.filter import AllowListFilter
from framework.core.skills.manager import SkillManager
from framework.core.skills.models import ResolutionContext, Skill
from framework.core.skills.source import InlineSkillSource


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
        assert isinstance(sm._builder, ProgressiveBuilder)

    @pytest.mark.asyncio
    async def test_list_skills_lazy_cache(self, manager):
        skills = await manager.list_skills()
        assert len(skills) == 2
        # second call should use cache
        skills2 = await manager.list_skills()
        assert len(skills2) == 2

    @pytest.mark.asyncio
    async def test_refresh_clears_cache(self, manager):
        await manager.list_skills()
        assert isinstance(manager._cache, dict)
        manager.refresh()
        assert manager._cache is None

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
        sm = SkillManager(source=source, builder=InlineBuilder())
        prompt = await sm.build_prompt()
        assert "## Skills" in prompt
        assert "ac" in prompt
        assert "bc" in prompt

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
