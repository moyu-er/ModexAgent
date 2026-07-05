"""TDD tests for the refactored AgentTemplate (Task 1.4)."""

from __future__ import annotations

from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.tools.presets import ContextMode, ToolPreset


class TestAgentTemplateDefaults:
    def test_defaults(self) -> None:
        t = AgentTemplate(agent_name="scout")
        assert t.agent_name == "scout"
        assert t.tool_preset == ToolPreset.READ_WRITE
        assert t.tool_supplements == []
        assert t.mcp == []
        assert t.max_steps == 80
        assert t.context_mode == ContextMode.FRESH


class TestAgentTemplateDeadFieldsGone:
    def test_dead_fields_absent(self) -> None:
        t = AgentTemplate(agent_name="x")
        for field in (
            "agent_type",
            "thinking_budget",
            "default_reads",
            "use_terminal",
            "terminal_visibility",
            "extra_tools",
        ):
            assert not hasattr(t, field), f"dead field {field!r} still present"
