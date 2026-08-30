# tests/unit/multi_agent/test_template.py
"""Tests for AgentTemplate."""

from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.skills import SkillsConfig
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.scope.spec import AgentSpec
from modex_agent.tools.presets import ContextMode, ToolPreset


def test_agent_template_defaults():
    t = AgentTemplate(spec=AgentSpec(name="test"))
    assert t.spec.name == "test"
    assert t.spec.description == ""
    assert t.spec.max_steps == 100
    assert t.toolset_profile == ToolPreset.READ_WRITE
    assert t.spec.context_mode == ContextMode.FRESH
    assert t.memory is None
    assert t.skills is None
    assert t.spec.mcp == []


def test_agent_template_full():
    t = AgentTemplate(
        spec=AgentSpec(
            name="code-reviewer",
            description="Reviews code",
            max_steps=30,
            toolset=ToolPreset.READ_WRITE,
        ),
        memory=MemoryConfig(),
        skills=SkillsConfig(roots=["skills/reviewer"]),
    )
    assert t.spec.max_steps == 30
    assert t.toolset_profile == ToolPreset.READ_WRITE


def test_agent_template_dead_fields_absent():
    """Removed fields must not exist on the dataclass."""
    t = AgentTemplate(spec=AgentSpec(name="test"))
    for field in (
        "agent_type",
        "thinking_budget",
        "default_reads",
        "use_terminal",
        "terminal_visibility",
        "extra_tools",
        "approval",
        "experience",
    ):
        assert not hasattr(t, field), f"dead field {field!r} still present"
