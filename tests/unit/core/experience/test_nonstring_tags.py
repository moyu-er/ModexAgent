"""Regression: non-string tag values in EXPERIENCE.md must not crash injection.

Root cause: ``FileExperienceSource`` built ``ExperienceSummary.tags`` straight
from YAML frontmatter.  YAML parses unquoted numerics (e.g. ``tags: [12306]``)
as ``int``, so ``tags`` ended up ``list[int]`` — violating the declared
``list[str]`` contract.  ``ExperiencePromptBuilder.build()`` then did
``",".join(exp.tags)`` and raised ``TypeError: expected str instance, int
found``.  That exception was swallowed by ``MemorySystemContextManager.load()``
step 8 (``except Exception: logger.debug``), so the symptom looked like
"experience not injected" rather than "experience crashed".

This locks down: frontmatter tags of any scalar type are coerced to ``str``,
and build_prompt() never crashes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from framework.core.experience.builder import ExperiencePromptBuilder
from framework.core.experience.source import FileExperienceSource

_EXP_MD = (
    "---\n"
    "name: int-tag-exp\n"
    "description: has a numeric tag\n"
    "tags: [12306, ssh]\n"   # 12306 parses as int in YAML
    "scenario: s\n"
    "---\n"
    "# body\n"
)


def _write(root: Path) -> None:
    d = root / "int-tag-exp"
    d.mkdir(parents=True, exist_ok=True)
    (d / "EXPERIENCE.md").write_text(_EXP_MD, encoding="utf-8")


@pytest.mark.asyncio
async def test_nonstring_tags_are_coerced_to_str(tmp_path: Path) -> None:
    """list_experiences must return tags as list[str] even for YAML ints."""
    root = tmp_path / "exp"
    _write(root)

    src = FileExperienceSource(directories=[root])
    summaries = await src.list_experiences(context=None)

    assert len(summaries) == 1
    tags = summaries[0].tags
    # Contract: every tag is a str.
    assert all(isinstance(t, str) for t in tags), (
        f"tags must be list[str], got {tags!r}"
    )
    assert tags == ["12306", "ssh"]


@pytest.mark.asyncio
async def test_build_prompt_does_not_crash_on_nonstring_tags(tmp_path: Path) -> None:
    """build_prompt() renders the block instead of raising TypeError."""
    root = tmp_path / "exp"
    _write(root)

    src = FileExperienceSource(directories=[root])
    summaries = await src.list_experiences(context=None)

    # This used to raise: TypeError: sequence item 0: expected str, int found
    prompt = ExperiencePromptBuilder().build(summaries)
    assert "int-tag-exp" in prompt
    assert "12306" in prompt
