import pytest
from pathlib import Path
from framework.core.experience.source import FileExperienceSource, sanitize_name


def test_sanitize_name():
    assert sanitize_name("Debug Network Timeout") == "debug-network-timeout"
    assert sanitize_name("QQ Large File Upload!!!") == "qq-large-file-upload!!!"
    assert sanitize_name("  Spaces  ") == "spaces"
    assert sanitize_name("") == "untitled"


@pytest.mark.asyncio
async def test_list_experiences():
    fixtures = Path(__file__).parent / "fixtures"
    source = FileExperienceSource(directories=[fixtures])
    summaries = await source.list_experiences()
    assert len(summaries) == 1
    assert summaries[0].name == "test-exp"
    assert summaries[0].tags == ["test", "example"]


@pytest.mark.asyncio
async def test_load_experience():
    fixtures = Path(__file__).parent / "fixtures"
    source = FileExperienceSource(directories=[fixtures])
    exp = await source.load_experience("test-exp")
    assert exp is not None
    assert exp.name == "test-exp"
    assert exp.body.startswith("# Test Experience")
    assert exp.location is not None
    assert exp.location.name == "EXPERIENCE.md"


@pytest.mark.asyncio
async def test_load_nonexistent():
    source = FileExperienceSource(directories=[Path("/nonexistent")])
    exp = await source.load_experience("nonexistent")
    assert exp is None


@pytest.mark.asyncio
async def test_list_experiences_uses_directory_name_as_canonical(tmp_path: Path):
    """list_experiences() returns directory name as identity, not frontmatter name.

    When frontmatter 'name' differs from directory name, directory name wins.
    """
    exp_dir = tmp_path / "original-name"
    exp_dir.mkdir()
    (exp_dir / "EXPERIENCE.md").write_text(
        "---\nname: Original Name\ndescription: Test\n---\n\n# Title\n\nBody.\n",
        encoding="utf-8",
    )
    source = FileExperienceSource(directories=[tmp_path])

    summaries = await source.list_experiences()
    assert len(summaries) == 1
    assert summaries[0].name == "original-name"  # directory name wins


@pytest.mark.asyncio
async def test_load_experience_matches_by_directory_name(tmp_path: Path):
    """load_experience() matches by directory name, not frontmatter name."""
    exp_dir = tmp_path / "my-dir-name"
    exp_dir.mkdir()
    (exp_dir / "EXPERIENCE.md").write_text(
        "---\nname: Frontmatter Name\ndescription: Test\n---\n\n# Title\n\nBody.\n",
        encoding="utf-8",
    )
    source = FileExperienceSource(directories=[tmp_path])

    # Must find by directory name
    exp = await source.load_experience("my-dir-name")
    assert exp is not None
    assert exp.name == "my-dir-name"

    # Must NOT find by frontmatter name
    exp2 = await source.load_experience("Frontmatter Name")
    assert exp2 is None
