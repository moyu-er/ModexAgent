"""Regression tests for junction/symlink-linked skills.

When a skill directory is a Windows junction (or POSIX symlink),
``DirectorySkillCache._refresh_if_stale()`` must NOT resolve the link
target via ``Path.resolve()``.  Resolving follows the link to an external
path that is NOT a sub-path of the monitored directory, causing
``relative_to()`` to fail and the skill to be silently dropped.

The fix removes ``.resolve()`` from two call sites:
- ``cache.py`` — ``_refresh_if_stale()`` grouping by monitored directory
- ``builder.py`` — ``_render_skill_xml()`` ``directory=`` attribute

These tests verify that junction/symlink-linked skills appear in
``list_skills()``, ``build_prompt()``, and that the ``directory=``
attribute preserves the link path (not the resolved target).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from modex_agent.plugins.defaults.capabilities.skills.builder import DefaultSkillBuilder
from modex_agent.plugins.defaults.capabilities.skills.cache import DirectorySkillCache
from modex_agent.plugins.defaults.capabilities.skills.catalog import SkillCatalog
from modex_agent.plugins.defaults.capabilities.skills.source import (
    FileSkillSource,
    SkillLayout,
)


def _create_dir_link(src: Path, dst: Path) -> None:
    """Create a directory link: symlink on POSIX, junction on Windows.

    Raises ``OSError`` if link creation is not supported on this platform
    or the user lacks permission (e.g. Windows non-admin without Developer
    Mode).  Callers should catch ``OSError`` and ``pytest.skip()``.
    """
    try:
        os.symlink(src, dst, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            raise
    # Windows fallback: junctions do not require elevated privileges
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def linked_skill_setup():
    """Create a temp dir with: one real skill + one link-linked skill.

    The link source lives OUTSIDE the monitored dir (like
    ``~/.agents/skills/``), so ``Path.resolve()`` would escape the
    monitored scope — this is the exact scenario that triggers the bug.
    """
    tmp = Path(tempfile.mkdtemp())
    monitored_dir = tmp / "skills"
    monitored_dir.mkdir()

    # Real skill (always works)
    real_skill = monitored_dir / "real-skill"
    real_skill.mkdir()
    (real_skill / "SKILL.md").write_text(
        "---\nname: real-skill\ndescription: A real skill.\n---\n# Real\n",
        encoding="utf-8",
    )

    # Link-linked skill (the bug)
    external_source = tmp / "external_source"
    external_source.mkdir()
    linked_src = external_source / "linked-skill"
    linked_src.mkdir()
    (linked_src / "SKILL.md").write_text(
        "---\nname: linked-skill\ndescription: A link-linked skill.\n---\n# Linked\n",
        encoding="utf-8",
    )

    # Create link: monitored_dir/linked-skill -> external_source/linked-skill
    link_path = monitored_dir / "linked-skill"
    try:
        _create_dir_link(linked_src, link_path)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        pytest.skip("Cannot create directory links on this platform/environment")

    yield tmp, monitored_dir

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_linked_skill_appears_in_get_skills(linked_skill_setup) -> None:
    """DirectorySkillCache.get_skills() must include link-linked skills."""
    _, monitored_dir = linked_skill_setup

    source = FileSkillSource(
        directories=[monitored_dir],
        cache=True,
        layout=SkillLayout.DIRECTORY,
        skill_filename="SKILL.md",
    )
    cache = DirectorySkillCache(
        directories=[monitored_dir], layout=SkillLayout.DIRECTORY
    )
    builder = DefaultSkillBuilder()
    mgr = SkillCatalog(source=source, builder=builder, cache=cache)

    skills = await mgr.list_skills()
    skill_names = [s.name for s in skills]

    assert "real-skill" in skill_names
    assert "linked-skill" in skill_names, (
        f"link-linked skill was dropped; found only: {skill_names}"
    )


@pytest.mark.asyncio
async def test_linked_skill_appears_in_build_prompt(linked_skill_setup) -> None:
    """build_prompt() must include link-linked skills in the XML."""
    _, monitored_dir = linked_skill_setup

    source = FileSkillSource(
        directories=[monitored_dir],
        cache=True,
        layout=SkillLayout.DIRECTORY,
        skill_filename="SKILL.md",
    )
    cache = DirectorySkillCache(
        directories=[monitored_dir], layout=SkillLayout.DIRECTORY
    )
    builder = DefaultSkillBuilder()
    mgr = SkillCatalog(source=source, builder=builder, cache=cache)

    prompt = await mgr.render_prompt()

    assert "linked-skill" in prompt


@pytest.mark.asyncio
async def test_linked_skill_directory_path_is_link_path(linked_skill_setup) -> None:
    """The ``directory=`` attribute must be the LINK path (within the
    monitored dir), NOT the resolved link target.

    This ensures the agent's file tools can access the skill through the
    project-relative link path.  OS file operations (exists / read_text /
    iterdir) transparently follow junctions and symlinks, so the
    un-resolved path works correctly for all downstream consumers.
    """
    tmp, monitored_dir = linked_skill_setup

    source = FileSkillSource(
        directories=[monitored_dir],
        cache=True,
        layout=SkillLayout.DIRECTORY,
        skill_filename="SKILL.md",
    )
    cache = DirectorySkillCache(
        directories=[monitored_dir], layout=SkillLayout.DIRECTORY
    )
    builder = DefaultSkillBuilder()
    mgr = SkillCatalog(source=source, builder=builder, cache=cache)

    prompt = await mgr.render_prompt()

    # The link path (within monitored dir) should appear; the resolved
    # external target path should NOT.
    expected_link_path = str(monitored_dir.resolve() / "linked-skill")
    external_path = str((tmp / "external_source" / "linked-skill").resolve())

    assert expected_link_path in prompt, (
        f"skill directory should be the link path ({expected_link_path}), "
        f"not the resolved target"
    )
    assert external_path not in prompt, (
        f"skill directory should NOT be the resolved target ({external_path})"
    )
