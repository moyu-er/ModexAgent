"""Unit tests for core/skills/cache.py."""

import tempfile
from pathlib import Path

import pytest

from modex_agent.plugins.defaults.capabilities.skills.builder import DefaultSkillBuilder
from modex_agent.plugins.defaults.capabilities.skills.cache import (
    DirectorySkillCache,
    SkillCache,
)
from modex_agent.plugins.defaults.capabilities.skills.source import (
    FileSkillSource,
    InlineSkillSource,
    SkillLayout,
)


class TestSkillCacheABC:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            SkillCache()  # type: ignore[abstract]


class TestDirectorySkillCache:
    def test_rejects_unknown_layout(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not a valid SkillLayout"):
            DirectorySkillCache(directories=[tmp_path], layout="nested")

    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def _make_source(self, directories: list[Path], **kwargs) -> FileSkillSource:
        return FileSkillSource(
            directories=directories,
            cache=True,
            layout=SkillLayout.DIRECTORY,
            skill_filename="SKILL.md",
            **kwargs,
        )

    def _add_skill(self, parent: Path, name: str, content: str = "") -> Path:
        d = parent / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} skill\n---\n{content}", encoding="utf-8",
        )
        return d

    # -- new skill detected --------------------------------------------------

    @pytest.mark.asyncio
    async def test_new_skill_detected(self, tmp_dir):
        self._add_skill(tmp_dir, "alpha")
        source = self._make_source([tmp_dir])
        cache = DirectorySkillCache(
            directories=[tmp_dir], layout=SkillLayout.DIRECTORY
        )
        builder = DefaultSkillBuilder()

        skills = await cache.get_skills(source, builder, None, None)
        assert {s.name for s in skills} == {"alpha"}

        self._add_skill(tmp_dir, "beta")
        skills = await cache.get_skills(source, builder, None, None)
        assert {s.name for s in skills} == {"alpha", "beta"}

    # -- deleted skill detected ----------------------------------------------

    @pytest.mark.asyncio
    async def test_deleted_skill_detected(self, tmp_dir):
        d = self._add_skill(tmp_dir, "alpha")
        self._add_skill(tmp_dir, "beta")
        source = self._make_source([tmp_dir])
        cache = DirectorySkillCache(
            directories=[tmp_dir], layout=SkillLayout.DIRECTORY
        )
        builder = DefaultSkillBuilder()

        skills = await cache.get_skills(source, builder, None, None)
        assert {s.name for s in skills} == {"alpha", "beta"}

        import shutil
        shutil.rmtree(d)
        skills = await cache.get_skills(source, builder, None, None)
        assert {s.name for s in skills} == {"beta"}

    # -- same-name content changes refresh -----------------------------------

    @pytest.mark.asyncio
    async def test_same_name_content_change_refreshes_cache(self, tmp_dir):
        skill_dir = self._add_skill(tmp_dir, "alpha", "v1")
        source = self._make_source([tmp_dir])
        cache = DirectorySkillCache(
            directories=[tmp_dir], layout=SkillLayout.DIRECTORY
        )
        builder = DefaultSkillBuilder()

        first = await cache.get_skills(source, builder, None, None)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: alpha skill\n---\nv2 changed", encoding="utf-8"
        )
        second = await cache.get_skills(source, builder, None, None)

        assert first[0].content == "v1"
        assert second[0].content == "v2 changed"

    # -- metadata change rebuilds the prompt ---------------------------------

    @pytest.mark.asyncio
    async def test_metadata_change_rebuilds_prompt(self, tmp_dir):
        d = self._add_skill(tmp_dir, "alpha", "v1")
        source = self._make_source([tmp_dir])
        cache = DirectorySkillCache(
            directories=[tmp_dir], layout=SkillLayout.DIRECTORY
        )
        builder = DefaultSkillBuilder()

        prompt1 = await cache.build_prompt(source, builder, None, None)
        assert 'name="alpha"' in prompt1

        (d / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: refreshed\n---\nv2",
            encoding="utf-8",
        )
        prompt2 = await cache.build_prompt(source, builder, None, None)
        assert prompt2 != prompt1
        assert "refreshed" in prompt2

    # -- last-wins dedup -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_last_wins_dedup(self, tmp_dir):
        dir_a = tmp_dir / "a"
        dir_b = tmp_dir / "b"
        self._add_skill(dir_a, "shared", "from_a")
        self._add_skill(dir_a, "only-a")
        self._add_skill(dir_b, "shared", "from_b")
        self._add_skill(dir_b, "only-b")

        source = self._make_source([dir_a, dir_b])
        cache = DirectorySkillCache(
            directories=[dir_a, dir_b], layout=SkillLayout.DIRECTORY
        )
        builder = DefaultSkillBuilder()

        skills = await cache.get_skills(source, builder, None, None)
        names = [s.name for s in skills]
        assert names == ["only-a", "shared", "only-b"]
        shared = next(s for s in skills if s.name == "shared")
        assert shared.content == "from_b"

    # -- prompt concatenation order ------------------------------------------

    @pytest.mark.asyncio
    async def test_prompt_concatenation_order(self, tmp_dir):
        dir_a = tmp_dir / "a"
        dir_b = tmp_dir / "b"
        self._add_skill(dir_a, "first")
        self._add_skill(dir_b, "second")

        source = self._make_source([dir_a, dir_b])
        cache = DirectorySkillCache(
            directories=[dir_a, dir_b], layout=SkillLayout.DIRECTORY
        )
        builder = DefaultSkillBuilder()

        prompt = await cache.build_prompt(source, builder, None, None)
        idx_a = prompt.find("first")
        idx_b = prompt.find("second")
        assert idx_a != -1 and idx_b != -1
        assert idx_a < idx_b

    # -- empty directory -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_empty_directory(self, tmp_dir):
        source = self._make_source([tmp_dir])
        cache = DirectorySkillCache(
            directories=[tmp_dir], layout=SkillLayout.DIRECTORY
        )
        builder = DefaultSkillBuilder()

        skills = await cache.get_skills(source, builder, None, None)
        assert skills == ()
        prompt = await cache.build_prompt(source, builder, None, None)
        assert prompt == ""

    # -- missing directory ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_missing_directory(self, tmp_dir):
        missing = tmp_dir / "does_not_exist"
        source = self._make_source([missing])  # FileSkillSource handles missing dirs
        cache = DirectorySkillCache(
            directories=[missing], layout=SkillLayout.DIRECTORY
        )
        builder = DefaultSkillBuilder()

        skills = await cache.get_skills(source, builder, None, None)
        assert skills == ()

    # -- invalidate ----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_invalidate_forces_full_rebuild(self, tmp_dir):
        self._add_skill(tmp_dir, "alpha")
        source = self._make_source([tmp_dir])
        cache = DirectorySkillCache(
            directories=[tmp_dir], layout=SkillLayout.DIRECTORY
        )
        builder = DefaultSkillBuilder()

        await cache.get_skills(source, builder, None, None)
        cache.invalidate()

        skills = await cache.get_skills(source, builder, None, None)
        assert {s.name for s in skills} == {"alpha"}

    # -- works with non-FileSkillSource --------------------------------------

    @pytest.mark.asyncio
    async def test_works_with_any_skill_source(self):
        from modex_agent.plugins.defaults.capabilities.skills.models import Skill
        inline = InlineSkillSource(
            [Skill(name="x", content="xc")], name="test",
        )
        cache = DirectorySkillCache(
            directories=[Path("/nonexistent")], layout=SkillLayout.DIRECTORY
        )
        builder = DefaultSkillBuilder()

        skills = await cache.get_skills(inline, builder, None, None)
        assert skills == ()  # no dirs match skill locations
