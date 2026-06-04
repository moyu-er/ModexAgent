"""Unit tests for core/skills/builder.py."""

import pytest

from framework.core.skills.builder import DefaultSkillBuilder
from framework.core.skills.models import Skill


class TestDefaultSkillBuilder:
    @pytest.mark.asyncio
    async def test_empty_skills_returns_empty(self):
        b = DefaultSkillBuilder()
        assert await b.build([]) == ""

    @pytest.mark.asyncio
    async def test_xml_metadata_only_no_body_content(self):
        """Only name, directory, and description — never body content."""
        skills = [Skill(name="s1", description="d1", content="BODY_SHOULD_NOT_APPEAR", location="/tmp/s1.md")]
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
        assert "&amp;" in out
        assert "&lt;" in out
