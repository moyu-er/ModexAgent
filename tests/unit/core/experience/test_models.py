from framework.core.experience.models import Experience, ExperienceSummary


def test_experience_summary_defaults():
    s = ExperienceSummary(name="test")
    assert s.name == "test"
    assert s.description == ""
    assert s.tags == []
    assert s.scenario == ""
    assert s.directory == ""


def test_experience_summary_non_defaults():
    s = ExperienceSummary(
        name="test",
        description="desc",
        tags=["a", "b"],
        scenario="scenario",
        directory="/data/experiences/a",
    )
    assert s.directory == "/data/experiences/a"
    assert s.tags == ["a", "b"]


def test_experience_defaults():
    e = Experience(name="test")
    assert e.name == "test"
    assert e.description == ""
    assert e.tags == []
    assert e.scenario == ""
    assert e.trigger == ""
    assert e.version == 1
    assert e.created_at is None
    assert e.pinned is False
    assert e.location is None
    assert e.body == ""
    assert e.frontmatter == {}


def test_experience_non_defaults():
    e = Experience(
        name="test",
        pinned=True,
        version=2,
    )
    assert e.pinned is True
    assert e.version == 2
