"""TDD tests for the refactored AgentTemplate (Task 1.4)."""

from __future__ import annotations

from modex_agent.multi_agent.pool_config.specs import SubagentSpec
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.tools.presets import ContextMode, ToolPreset


class TestAgentTemplateDefaults:
    def test_defaults(self) -> None:
        t = AgentTemplate(spec=SubagentSpec(agent_name="scout"))
        assert t.spec.agent_name == "scout"
        assert t.spec.tool_preset == ToolPreset.READ_WRITE
        assert t.spec.tool_supplements == []
        assert t.spec.mcp == []
        assert t.spec.max_steps == 80
        assert t.spec.context_mode == ContextMode.FRESH


class TestAgentTemplateDeadFieldsGone:
    def test_dead_fields_absent(self) -> None:
        t = AgentTemplate(spec=SubagentSpec(agent_name="x"))
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
