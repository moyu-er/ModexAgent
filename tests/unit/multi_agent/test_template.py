# tests/unit/multi_agent/test_template.py
"""Tests for AgentTemplate."""

from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.skills import SkillsConfig
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.tools.presets import ContextMode, ToolPreset


def test_agent_template_defaults():
    t = AgentTemplate(agent_name="test")
    assert t.agent_name == "test"
    assert t.description == ""
    assert t.max_steps == 80
    assert t.tool_preset == ToolPreset.READ_WRITE
    assert t.tool_supplements == []
    assert t.context_mode == ContextMode.FRESH
    assert t.memory is None
    assert t.skills is None
    assert t.mcp == []


def test_agent_template_full():
    t = AgentTemplate(
        agent_name="code-reviewer",
        description="Reviews code",
        max_steps=30,
        tool_preset=ToolPreset.READ_WRITE,
        memory=MemoryConfig(),
        skills=SkillsConfig(roots=["skills/reviewer"]),
    )
    assert t.max_steps == 30
    assert t.tool_preset == ToolPreset.READ_WRITE


def test_agent_template_system_prompt_mode_default() -> None:
    """Default system_prompt_mode is REPLACE."""
    from modex_agent.tools.presets import SystemPromptMode
    t = AgentTemplate(agent_name="test")
    assert t.system_prompt_mode == SystemPromptMode.REPLACE


def test_agent_template_system_prompt_mode_append() -> None:
    """system_prompt_mode can be set to APPEND."""
    from modex_agent.tools.presets import SystemPromptMode
    t = AgentTemplate(agent_name="delegate", system_prompt_mode=SystemPromptMode.APPEND)
    assert t.system_prompt_mode == SystemPromptMode.APPEND


def test_agent_template_dead_fields_absent():
    """Removed fields must not exist on the dataclass."""
    t = AgentTemplate(agent_name="test")
    for field in (
        "agent_type",
        "thinking_budget",
        "default_reads",
        "use_terminal",
        "terminal_visibility",
        "extra_tools",
    ):
        assert not hasattr(t, field), f"dead field {field!r} still present"
