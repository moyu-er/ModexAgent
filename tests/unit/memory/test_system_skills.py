"""Unit tests for skill injection into MemorySystemContextManager.build_system_prompt."""

import pytest

from framework.core.skills import InlineBuilder, InlineSkillSource, SkillManager
from framework.core.skills.models import Skill
from framework.core.tool_manager import FunctionalTool, InMemoryToolManager
from framework.memory.system import MemorySystem, MemorySystemContextManager
from pathlib import Path


class MockSkillManager:
    def __init__(self, prompt: str):
        self._prompt = prompt

    async def build_prompt(self, context=None):
        return self._prompt


class TestMemorySystemContextManagerSkills:
    @pytest.fixture
    def cm(self):
        ms = MemorySystem(workspace=Path("./data/test_memory_skills"))
        return MemorySystemContextManager(
            memory_system=ms,
            base_system_prompt="Base prompt",
        )

    @pytest.mark.asyncio
    async def test_no_skill_manager_omits_skills(self, cm):
        prompt = await cm.build_system_prompt(tool_manager=None, skill_manager=None)
        assert "Base prompt" in prompt
        assert "## Skills" not in prompt

    @pytest.mark.asyncio
    async def test_skill_manager_injects_after_memory_and_tools(self, cm):
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
        assert "## Skills" in parts[1]  # skills come after base, before agent note

    @pytest.mark.asyncio
    async def test_empty_skill_prompt_omits_skills_header(self, cm):
        sm = MockSkillManager("")
        prompt = await cm.build_system_prompt(tool_manager=None, skill_manager=sm)
        assert "Base prompt" in prompt
        assert "## Skills" not in prompt

    @pytest.mark.asyncio
    async def test_real_skill_manager_with_inline_builder(self, cm):
        source = InlineSkillSource([Skill(name="mem_skill", content="memory body")])
        sm = SkillManager(source=source, builder=InlineBuilder())
        prompt = await cm.build_system_prompt(tool_manager=None, skill_manager=sm)
        assert "Base prompt" in prompt
        assert "### mem_skill" in prompt
        assert "memory body" in prompt

    @pytest.mark.asyncio
    async def test_empty_skill_list_omits_skills_header(self, cm):
        source = InlineSkillSource([])
        sm = SkillManager(source=source, builder=InlineBuilder())
        prompt = await cm.build_system_prompt(tool_manager=None, skill_manager=sm)
        assert "Base prompt" in prompt
        assert "## Skills" not in prompt

    @pytest.mark.asyncio
    async def test_order_base_memory_skills_runtime(self, cm):
        """Order: Base -> Memory -> Skills -> Runtime. Tools not in system prompt."""
        tm = InMemoryToolManager()
        tm.register(
            FunctionalTool(
                name="t1",
                description="Tool one",
                parameters={"type": "object", "properties": {}},
                func=lambda: "ok",
            )
        )
        sm = MockSkillManager("## Skills\n\nS")
        prompt = await cm.build_system_prompt(
            tool_manager=tm,
            skill_manager=sm,
            runtime_info={"current_time": "12:00"},
        )
        # Base -> (no memory yet) -> Skills -> Runtime; tools not in prompt
        assert "Base prompt" in prompt
        assert "## Skills" in prompt
        assert "12:00" in prompt
        assert "t1" not in prompt  # tools not injected into system prompt
        assert prompt.index("Base prompt") < prompt.index("## Skills")
        assert prompt.index("## Skills") < prompt.index("12:00")
