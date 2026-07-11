"""TDD tests for the refactored AgentConfig (Task 1.3).

Schema: drop dead fields (tools, system_prompt, extra_tools); add
``mcp``, ``tool_preset``, ``tool_supplements``; ``max_steps`` default 100;
``extra="forbid"``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.tools.presets import ToolPreset, ToolSupplement


class TestAgentConfigDefaults:
    def test_defaults(self) -> None:
        cfg = AgentConfig(name="a")
        assert cfg.tool_preset == ToolPreset.FULL
        assert cfg.tool_supplements == []
        assert cfg.mcp == []
        assert cfg.max_steps == 100
        assert cfg.use_terminal is False
        assert cfg.description == ""

    def test_description_settable(self) -> None:
        cfg = AgentConfig(name="a", description="Team lead for coding tasks.")
        assert cfg.description == "Team lead for coding tasks."


class TestAgentConfigDeadFieldsGone:
    @pytest.mark.parametrize("field", ["tools", "system_prompt", "extra_tools"])
    def test_dead_field_absent(self, field: str) -> None:
        cfg = AgentConfig(name="a")
        assert not hasattr(cfg, field)

    def test_dead_field_rejected_on_construction(self) -> None:
        """extra='forbid' rejects removed fields."""
        with pytest.raises(ValidationError):
            AgentConfig(name="a", tools=[])  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            AgentConfig(name="a", system_prompt="x")  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            AgentConfig(name="a", extra_tools=["x"])  # type: ignore[call-arg]


class TestAgentConfigUnknownKeysRejected:
    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentConfig(name="a", not_a_real_field=1)  # type: ignore[call-arg]


class TestAgentConfigToolPolicy:
    def test_tool_preset_and_supplements_settable(self) -> None:
        cfg = AgentConfig(
            name="coding",
            tool_preset=ToolPreset.READ_WRITE,
            tool_supplements=[ToolSupplement.AST_GREP],
            mcp=["playwright"],
        )
        assert cfg.tool_preset == ToolPreset.READ_WRITE
        assert cfg.tool_supplements == [ToolSupplement.AST_GREP]
        assert cfg.mcp == ["playwright"]
