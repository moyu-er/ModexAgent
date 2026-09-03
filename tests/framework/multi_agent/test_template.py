"""TDD tests for the refactored AgentTemplate (Task 1.4).

The ``TestMaterializeExternalDispatch`` class is removed: external
subagent dispatch is now handled by ``AssemblyPipeline.run`` (which
selects ``external_sub`` stages based on ``AgentType``), not by
``AgentTemplate.materialize``. The template uniformly routes through
``pipeline.run(sub_spec, ctx)`` regardless of execution_strategy.

The structural tests (defaults + dead-fields-gone) remain.
"""

from __future__ import annotations

from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.scope.spec import AgentSpec
from modex_agent.tools.presets import ContextMode, ToolPreset


class TestAgentTemplateDefaults:
    def test_defaults(self) -> None:
        t = AgentTemplate(spec=AgentSpec(name="scout"))
        assert t.spec.name == "scout"
        assert t.toolset_profile == ToolPreset.READ_WRITE
        assert t.spec.mcp == []
        assert t.spec.max_steps == 100
        assert t.spec.context_mode == ContextMode.FRESH
        assert t.memory is None


class TestAgentTemplateDeadFieldsGone:
    def test_dead_fields_absent(self) -> None:
        t = AgentTemplate(spec=AgentSpec(name="x"))
        for field in (
            "agent_type",
            "thinking_budget",
            "default_reads",
            "use_terminal",
            "terminal_visibility",
            "extra_tools",
            "approval",
            "experience",
            "assembly_context",
            "pipeline",
        ):
            assert not hasattr(t, field), f"dead field {field!r} still present"
