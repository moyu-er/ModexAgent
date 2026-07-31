"""MainAgentSpec execution_strategy field tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_agent.agents.external.paths import ProviderKind
from modex_agent.multi_agent.pool_config import MainAgentSpec
from modex_agent.tools.presets import ToolPreset, ToolSupplement


class TestMainAgentSpecExecutionStrategy:
    def test_defaults_to_react(self) -> None:
        spec = MainAgentSpec(agent_name="main")
        assert spec.execution_strategy == "react"

    def test_accepts_external(self) -> None:
        spec = MainAgentSpec(
            agent_name="main",
            execution_strategy="external",
            provider_kind=ProviderKind.OPENCODE,
        )
        assert spec.execution_strategy == "external"
        assert spec.agent_name == "main"
        assert spec.tool_preset == ToolPreset.FULL
        assert spec.tool_supplements == [ToolSupplement.TODO]

    def test_preserves_other_defaults(self) -> None:
        spec = MainAgentSpec(
            agent_name="main",
            execution_strategy="external",
            provider_kind=ProviderKind.OPENCODE,
        )
        assert spec.description == ""
        assert spec.max_steps == 100
        assert spec.use_terminal is False
        assert spec.approval is None

    def test_frozen(self) -> None:
        spec = MainAgentSpec(
            agent_name="main",
            execution_strategy="external",
            provider_kind=ProviderKind.OPENCODE,
        )
        with pytest.raises(ValidationError):
            spec.execution_strategy = "react"
