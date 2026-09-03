"""Unit tests for core/skills/builder.py."""

import inspect
from pathlib import Path

import pytest

from modex_agent.plugins.defaults.capabilities.skills.builder import (
    DefaultSkillBuilder,
    build_skill_command_xml,
)
from modex_agent.plugins.defaults.capabilities.skills.models import Skill


class TestDefaultSkillBuilder:
    def test_constructor_has_no_legacy_base_path_parameter(self) -> None:
        assert "base_path" not in inspect.signature(DefaultSkillBuilder).parameters

    @pytest.mark.asyncio
    async def test_empty_skills_returns_empty(self):
        b = DefaultSkillBuilder()
        assert await b.build([]) == ""

    @pytest.mark.asyncio
    async def test_xml_metadata_only_no_body_content(self):
        """Only name, directory, and description — never body content."""
        skills = [
            Skill(
                name="s1", description="d1", content="BODY_SHOULD_NOT_APPEAR", location="/tmp/s1.md"
            )
        ]
        b = DefaultSkillBuilder()
        out = await b.build(skills)
        assert "<available_skills>" in out
        assert "</available_skills>" in out
        assert 'name="s1"' in out
        assert "<description>d1</description>" in out
        assert "BODY_SHOULD_NOT_APPEAR" not in out

    @pytest.mark.asyncio
    async def test_multiple_skills(self):
        skills = [
            Skill(name="a", description="desc a", location="/tmp/a.md"),
            Skill(name="b", description="desc b", location="/tmp/b.md"),
        ]
        out = await DefaultSkillBuilder().build(skills)
        assert out.count("<skill ") == 2
        assert 'name="a"' in out
        assert 'name="b"' in out

    @pytest.mark.asyncio
    async def test_no_description_element_when_empty(self):
        skills = [Skill(name="s1", description="", location="/tmp/s1.md")]
        out = await DefaultSkillBuilder().build(skills)
        assert "<description>" not in out

    @pytest.mark.asyncio
    async def test_xml_escapes_special_chars(self):
        skills = [Skill(name="test_skill", description="a & b < c", location="/tmp/s.md")]
        out = await DefaultSkillBuilder().build(skills)
        # Description with special chars is wrapped in CDATA (not entity-escaped)
        assert "<![CDATA[\na & b < c\n]]>" in out


class TestBuildSkillCommandXml:
    def test_without_location_omits_directory_attribute(self) -> None:
        out = build_skill_command_xml("weather", "Use weather APIs.", "tomorrow")
        assert '<command_context type="skill" name="weather">\n' in out
        assert "directory=" not in out
        assert "<skill>\nUse weather APIs.\n</skill>" in out
        assert "<user_input>\ntomorrow\n</user_input>" in out

    def test_with_location_injects_directory_attribute(self) -> None:
        location = "/skills/writing-great-skills/SKILL.md"
        out = build_skill_command_xml(
            "writing-great-skills",
            "See [GLOSSARY.md](GLOSSARY.md).",
            "review this",
            skill_location=location,
        )
        expected_dir = str(Path(location).parent)
        assert (
            f'<command_context type="skill" name="writing-great-skills" directory="{expected_dir}">'
            in out
        )
        assert "<skill>\nSee [GLOSSARY.md](GLOSSARY.md).\n</skill>" in out
        assert "<user_input>\nreview this\n</user_input>" in out

    def test_none_location_omits_directory(self) -> None:
        out = build_skill_command_xml("s", "b", "a", skill_location=None)
        assert "directory=" not in out

    def test_empty_location_omits_directory(self) -> None:
        out = build_skill_command_xml("s", "b", "a", skill_location="")
        assert "directory=" not in out

    def test_directory_is_parent_not_the_skill_file(self) -> None:
        out = build_skill_command_xml("s", "b", "a", skill_location="/a/b/c/SKILL.md")
        expected_dir = str(Path("/a/b/c/SKILL.md").parent)
        assert f'directory="{expected_dir}"' in out
        dir_value = out.split('directory="')[1].split('"')[0]
        assert "SKILL.md" not in dir_value

    def test_directory_path_escaped_as_xml_attribute(self) -> None:
        out = build_skill_command_xml("s", "b", "a", skill_location="/tmp/a&b<c/SKILL.md")
        expected_dir = str(Path("/tmp/a&b<c/SKILL.md").parent)
        escaped = expected_dir.replace("&", "&amp;").replace("<", "&lt;")
        assert f'directory="{escaped}"' in out

    def test_relative_location_stays_relative(self) -> None:
        out = build_skill_command_xml("s", "b", "a", skill_location="skills/main/s/SKILL.md")
        expected_dir = str(Path("skills/main/s/SKILL.md").parent)
        assert f'directory="{expected_dir}"' in out
