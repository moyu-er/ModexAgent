# tests/unit/multi_agent/test_template.py
"""Tests for AgentTemplate."""

from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.configs.skills import SkillsConfig
from framework.multi_agent.template import AgentTemplate
from framework.tools.presets import ContextMode, ThinkingBudget, ToolPreset


def test_agent_template_defaults():
    t = AgentTemplate(agent_type="test")
    assert t.agent_type == "test"
    assert t.description == ""
    assert t.max_steps == 20
    assert t.tool_preset == ToolPreset.READ_WRITE
    assert t.context_mode == ContextMode.FRESH
    assert t.thinking_budget == ThinkingBudget.MEDIUM
    assert t.use_terminal is True
    assert t.memory is None
    assert t.skills is None


def test_agent_template_full():
    t = AgentTemplate(
        agent_type="code-reviewer",
        description="Reviews code",
        max_steps=30,
        tool_preset=ToolPreset.READ_WRITE,
        use_terminal=False,
        memory=MemoryConfig(),
        skills=SkillsConfig(roots=["skills/reviewer"]),
    )
    assert t.max_steps == 30
    assert t.tool_preset == ToolPreset.READ_WRITE


def test_agent_template_system_prompt_mode_default() -> None:
    """Default system_prompt_mode is REPLACE."""
    from framework.tools.presets import SystemPromptMode
    t = AgentTemplate(agent_type="test")
    assert t.system_prompt_mode == SystemPromptMode.REPLACE


def test_agent_template_system_prompt_mode_append() -> None:
    """system_prompt_mode can be set to APPEND."""
    from framework.tools.presets import SystemPromptMode
    t = AgentTemplate(agent_type="delegate", system_prompt_mode=SystemPromptMode.APPEND)
    assert t.system_prompt_mode == SystemPromptMode.APPEND
