# tests/unit/multi_agent/test_template.py
"""Tests for AgentTemplate."""

from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.configs.skills import SkillsConfig
from framework.multi_agent.template import AgentTemplate


def test_agent_template_defaults():
    t = AgentTemplate(agent_type="test")
    assert t.agent_type == "test"
    assert t.description == ""
    assert t.max_steps == 20
    assert t.standard_tools is True
    assert t.use_terminal is True
    assert t.memory is None
    assert t.skills is None


def test_agent_template_full():
    t = AgentTemplate(
        agent_type="code-reviewer",
        description="Reviews code",
        max_steps=30,
        standard_tools=False,
        use_terminal=False,
        memory=MemoryConfig(),
        skills=SkillsConfig(roots=["skills/reviewer"]),
    )
    assert t.max_steps == 30
    assert t.standard_tools is False
