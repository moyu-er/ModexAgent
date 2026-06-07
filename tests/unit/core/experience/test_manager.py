import pytest
from pathlib import Path

from framework.core.experience.manager import ExperienceManager
from framework.core.experience.source import FileExperienceSource


@pytest.mark.asyncio
async def test_build_prompt_empty():
    source = FileExperienceSource(directories=[Path("/nonexistent")])
    manager = ExperienceManager(source=source)
    prompt = await manager.build_prompt()
    assert prompt == ""


@pytest.mark.asyncio
async def test_build_prompt_with_experiences():
    fixtures = Path(__file__).parent / "fixtures"
    source = FileExperienceSource(directories=[fixtures])
    manager = ExperienceManager(source=source)
    prompt = await manager.build_prompt()
    assert "<available_experiences>" in prompt
    assert "test-exp" in prompt


@pytest.mark.asyncio
async def test_build_prompt_filters_by_max_experiences(tmp_path: Path):
    """build_prompt must cap at max_experiences."""
    for i in range(5):
        d = tmp_path / f"exp-{i}"
        d.mkdir()
        (d / "EXPERIENCE.md").write_text(
            f"---\nname: exp-{i}\ndescription: x\n---\n\nBody.\n",
            encoding="utf-8",
        )
    source = FileExperienceSource(directories=[tmp_path])
    manager = ExperienceManager(source=source)
    prompt = await manager.build_prompt(max_experiences=2)
    assert prompt.count("<experience ") == 2
