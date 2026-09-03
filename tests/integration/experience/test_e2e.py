from pathlib import Path

import pytest


@pytest.mark.integration
async def test_full_pipeline_no_experiences(tmp_path: Path):
    """Full pipeline with no experiences — should not crash."""
    from modex_agent.plugins.defaults.capabilities.experience.catalog import (
        ExperienceCatalog,
    )
    from modex_agent.plugins.defaults.capabilities.experience.curator import (
        ExperienceCurator,
    )
    from modex_agent.plugins.defaults.capabilities.experience.metadata import (
        PerFileExperienceMetaStore,
    )
    from modex_agent.plugins.defaults.capabilities.experience.source import (
        FileExperienceSource,
    )
    from modex_agent.plugins.defaults.capabilities.experience.tools import (
        ExperienceReadTool,
    )

    exp_dir = tmp_path / "experiences"
    exp_dir.mkdir()

    # Create empty source
    source = FileExperienceSource(directories=[exp_dir])
    summaries = await source.list_experiences()
    assert len(summaries) == 0

    # Render the index — should be empty
    catalog = ExperienceCatalog(experience_dir=exp_dir, meta_store=PerFileExperienceMetaStore(exp_dir))
    prompt = await catalog.render_index()
    assert prompt == ""

    # Curator on empty should not crash
    meta_store = PerFileExperienceMetaStore(exp_dir)
    curator = ExperienceCurator(exp_dir, meta_store)
    counts = await curator.run()
    assert counts.checked == 0

    # Trackers should not crash on empty / nonexistent entries
    meta_store.bump_use("nonexistent")  # should not raise

    # Read tool on nonexistent — should return error XML
    read_tool = ExperienceReadTool(exp_dir, meta_store)
    result = await read_tool.execute(name="nonexistent")
    assert "not found" in result
