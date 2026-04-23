"""Unit tests for skill injection into ContextManager build_system_prompt."""

import tempfile
from pathlib import Path

import pytest

from framework.core.context import FileContextManager, InMemoryContextManager
from framework.core.skills import InlineBuilder, InlineSkillSource, SkillManager
from framework.core.skills.models import Skill
from framework.core.tool_manager import FunctionalTool, InMemoryToolManager


class MockSkillManager:
    """Lightweight mock for skill_manager that returns a fixed prompt."""

    def __init__(self, prompt: str):
        self._prompt = prompt

    async def build_prompt(self, context=None):
        return self._prompt


class TestInMemoryContextManagerSkills:
    @pytest.fixture
    def cm(self):
        return InMemoryContextManager(base_system_prompt="Base prompt")

    @pytest.mark.asyncio
    async def test_no_skill_manager_omits_skills_section(self, cm):
        prompt = await cm.build_system_prompt(tool_manager=None)
        assert "Base prompt" in prompt
        assert "## Skills" not in prompt

    @pytest.mark.asyncio
    async def test_skill_manager_injects_skills_after_base(self, cm):
        """Skills injected after base prompt; tools not in system prompt."""
        tm = InMemoryToolManager()
        tm.register(
            FunctionalTool(
                name="weather",
                description="Get weather",
                parameters={"type": "object", "properties": {}},
                func=lambda: "sunny",
            )
        )
        sm = MockSkillManager("## Skills\n\nSkill content")
        prompt = await cm.build_system_prompt(tool_manager=tm, skill_manager=sm)
        parts = prompt.split("\n\n---\n\n")
        assert parts[0] == "Base prompt"
        assert "## Skills" in parts[1]
        assert "weather" not in prompt  # tools not injected into system prompt

    @pytest.mark.asyncio
    async def test_empty_skill_prompt_omits_skills_section(self, cm):
        sm = MockSkillManager("")
        prompt = await cm.build_system_prompt(tool_manager=None, skill_manager=sm)
        assert "Base prompt" in prompt
        assert "## Skills" not in prompt

    @pytest.mark.asyncio
    async def test_tools_not_in_system_prompt(self, cm):
        """Tool descriptions are passed via API tools param, not system prompt."""
        tm = InMemoryToolManager()
        tm.register(
            FunctionalTool(
                name="calc",
                description="Calculate",
                parameters={"type": "object", "properties": {}},
                func=lambda: "42",
            )
        )
        prompt = await cm.build_system_prompt(tool_manager=tm, skill_manager=None)
        assert "Base prompt" in prompt
        assert "calc" not in prompt  # tools not injected into system prompt
        assert "## Skills" not in prompt

    @pytest.mark.asyncio
    async def test_skills_without_tools(self, cm):
        sm = MockSkillManager("## Skills\n\nInjected skill")
        prompt = await cm.build_system_prompt(tool_manager=None, skill_manager=sm)
        parts = prompt.split("\n\n---\n\n")
        assert parts[0] == "Base prompt"
        assert "## Skills" in parts[1]


class TestFileContextManagerSkills:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    @pytest.fixture
    def cm(self, tmp_dir):
        return FileContextManager(base_system_prompt="File prompt", data_dir=tmp_dir)

    @pytest.mark.asyncio
    async def test_no_skill_manager_omits_skills(self, cm):
        prompt = await cm.build_system_prompt(tool_manager=None)
        assert "File prompt" in prompt
        assert "## Skills" not in prompt

    @pytest.mark.asyncio
    async def test_skill_manager_injects_skills(self, cm):
        sm = MockSkillManager("## Skills\n\nInjected")
        prompt = await cm.build_system_prompt(tool_manager=None, skill_manager=sm)
        assert "File prompt" in prompt
        assert "## Skills" in prompt

    @pytest.mark.asyncio
    async def test_real_skill_manager_with_inline_builder(self, cm):
        source = InlineSkillSource([Skill(name="s1", content="body")])
        sm = SkillManager(source=source, builder=InlineBuilder())
        prompt = await cm.build_system_prompt(tool_manager=None, skill_manager=sm)
        assert "File prompt" in prompt
        assert "### s1" in prompt
        assert "body" in prompt

    @pytest.mark.asyncio
    async def test_empty_skill_list_omits_skills_header(self, cm):
        source = InlineSkillSource([])
        sm = SkillManager(source=source, builder=InlineBuilder())
        prompt = await cm.build_system_prompt(tool_manager=None, skill_manager=sm)
        assert "File prompt" in prompt
        assert "## Skills" not in prompt
