"""ExperienceCatalog.render_index — the retired manager+builder fold.

Covers the retired ``ExperienceManager.build_prompt`` and
``ExperiencePromptBuilder.build`` behavior through the catalog's public
``render_index()`` face (prompt rendering uses the same catalog the tool
surface and reviewer use — invariant §10.6).
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.plugins.defaults.capabilities.experience.catalog import ExperienceCatalog
from modex_agent.plugins.defaults.capabilities.experience.metadata import (
    PerFileExperienceMetaStore,
)


def _catalog(root: Path) -> ExperienceCatalog:
    return ExperienceCatalog(experience_dir=root, meta_store=PerFileExperienceMetaStore(root))


async def test_render_index_empty(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    assert await catalog.render_index() == ""


async def test_render_index_with_experiences(tmp_path: Path) -> None:
    fixtures = Path(__file__).parent / "fixtures"
    catalog = _catalog(fixtures)
    prompt = await catalog.render_index()
    assert "<available_experiences>" in prompt
    assert "test-exp" in prompt


async def test_render_index_caps_at_limit(tmp_path: Path) -> None:
    """render_index must cap at the limit (default 20, override lower)."""
    for i in range(5):
        d = tmp_path / f"exp-{i}"
        d.mkdir()
        (d / "EXPERIENCE.md").write_text(
            f"---\nname: exp-{i}\ndescription: x\n---\n\nBody.\n",
            encoding="utf-8",
        )
    catalog = _catalog(tmp_path)
    prompt = await catalog.render_index(limit=2)
    assert prompt.count("<experience ") == 2
    full = await catalog.render_index()
    assert full.count("<experience ") == 5
