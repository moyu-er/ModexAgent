from framework.core.experience.builder import ExperiencePromptBuilder
from framework.core.experience.models import ExperienceSummary


def test_build_empty():
    b = ExperiencePromptBuilder()
    assert b.build([]) == ""


def test_build_single():
    b = ExperiencePromptBuilder()
    result = b.build([ExperienceSummary(
        name="debug-timeout",
        description="Debug connection timeouts",
        tags=["debug", "network"],
        scenario="connection timeout",
        directory="/data/experiences/main/agent/debug-timeout",
    )])
    assert "<available_experiences>" in result
    assert 'name="debug-timeout"' in result
    assert 'tags="debug,network"' in result
    assert 'directory="/data/experiences/main/agent/debug-timeout"' in result
    assert "Debug connection timeouts" in result
    assert "experience" in result  # instruction to use tool


def test_build_multiple():
    b = ExperiencePromptBuilder()
    result = b.build([
        ExperienceSummary(name="a", tags=["t1"], directory="/data/a"),
        ExperienceSummary(name="b", tags=["t2"], directory="/data/b"),
    ])
    assert result.count("<experience ") == 2
    assert 'directory="/data/a"' in result
    assert 'directory="/data/b"' in result
