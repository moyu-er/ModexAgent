import pytest
from pathlib import Path


@pytest.mark.integration
async def test_full_pipeline_no_experiences(tmp_path: Path):
    """Full pipeline with no experiences — should not crash."""
    from modex_agent.core.experience.source import FileExperienceSource
    from modex_agent.core.experience.manager import ExperienceManager
    from modex_agent.core.experience.meta import PerFileExperienceMetaStore
    from modex_agent.core.experience.curator import ExperienceCurator
    from modex_agent.memory.tools.experience import ExperienceReadTool, ExperienceListTool

    exp_dir = tmp_path / "experiences"
    exp_dir.mkdir()

    # Create empty source
    source = FileExperienceSource(directories=[exp_dir])
    summaries = await source.list_experiences()
    assert len(summaries) == 0

    # Build prompt — should be empty
    manager = ExperienceManager(source=source)
    prompt = await manager.build_prompt()
    assert prompt == ""

    # Curator on empty should not crash
    usage_file = exp_dir / ".usage.json"
    meta_store = PerFileExperienceMetaStore(exp_dir)
    curator = ExperienceCurator(exp_dir, meta_store)
    counts = await curator.run()
    assert counts["checked"] == 0

    # Trackers should not crash on empty / nonexistent entries
    meta_store.bump_use("nonexistent")  # should not raise

    # Read tool on nonexistent — should return error XML
    read_tool = ExperienceReadTool(exp_dir, meta_store)
    result = await read_tool.execute(name="nonexistent")
    assert "not found" in result
