"""Regression: non-string tag values in EXPERIENCE.md must not crash injection.

Root cause: ``FileExperienceSource`` built ``ExperienceSummary.tags`` straight
from YAML frontmatter.  YAML parses unquoted numerics (e.g. ``tags: [12306]``)
as ``int``, so ``tags`` ended up ``list[int]`` — violating the declared
``list[str]`` contract.  ``render_index_xml`` then did
``",".join(exp.tags)`` and raised ``TypeError: expected str instance, int
found``.  That exception was swallowed by ``MemorySystemContextManager.load()``
step 8 (``except Exception: logger.debug``), so the symptom looked like
"experience not injected" rather than "experience crashed".

This locks down: frontmatter tags of any scalar type are coerced to ``str``,
and render_index never crashes.
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.plugins.defaults.capabilities.experience.catalog import (
    ExperienceCatalog,
)
from modex_agent.plugins.defaults.capabilities.experience.metadata import (
    PerFileExperienceMetaStore,
)
from modex_agent.plugins.defaults.capabilities.experience.source import FileExperienceSource

_EXP_MD = (
    "---\n"
    "name: int-tag-exp\n"
    "description: has a numeric tag\n"
    "tags: [12306, ssh]\n"  # 12306 parses as int in YAML
    "scenario: s\n"
    "---\n"
    "# body\n"
)


def _write(root: Path) -> None:
    d = root / "int-tag-exp"
    d.mkdir(parents=True, exist_ok=True)
    (d / "EXPERIENCE.md").write_text(_EXP_MD, encoding="utf-8")


async def test_nonstring_tags_are_coerced_to_str(tmp_path: Path) -> None:
    """list_experiences must return tags as list[str] even for YAML ints."""
    root = tmp_path / "exp"
    _write(root)

    src = FileExperienceSource(directories=[root])
    summaries = await src.list_experiences(context=None)

    assert len(summaries) == 1
    tags = summaries[0].tags
    assert all(isinstance(t, str) for t in tags), f"tags must be list[str], got {tags!r}"
    assert tags == ["12306", "ssh"]


async def test_render_index_does_not_crash_on_nonstring_tags(tmp_path: Path) -> None:
    """render_index renders the block instead of raising TypeError."""
    root = tmp_path / "exp"
    _write(root)

    catalog = ExperienceCatalog(
        experience_dir=root, meta_store=PerFileExperienceMetaStore(root)
    )
    prompt = await catalog.render_index()
    assert "int-tag-exp" in prompt
    assert "12306" in prompt


def test_source_directories_materialize(tmp_path: Path) -> None:
    """The source's directory list resolves (smoke for the fixture shape)."""
    src = FileExperienceSource(directories=[tmp_path])
    assert src.directories == [tmp_path.resolve()]
