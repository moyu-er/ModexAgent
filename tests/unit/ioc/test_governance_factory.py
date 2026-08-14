"""Tests for framework.ioc.factories.governance."""

import pytest

from modex_agent.ioc.configs.memory import (
    BudgetConfig,
    GovernanceConfig,
    MemoryConfig,
    SessionConfig,
    ShortTermConfig,
)
from modex_agent.ioc.factories.governance import create_governance, create_subagent_governance


class TestCreateGovernance:
    def test_none_cfg_returns_none(self) -> None:
        assert create_governance(None) is None

    def test_disabled_by_tool_chain_repair(self) -> None:
        cfg = MemoryConfig(governance=GovernanceConfig(tool_chain_repair=False))
        assert create_governance(cfg) is None

    def test_minimal_governance(self) -> None:
        """ToolChainRepair when governance is bare."""
        cfg = MemoryConfig(governance=GovernanceConfig())
        gov = create_governance(cfg)
        assert gov is not None
        assert len(gov._strategies) == 1  # ToolChainRepair only

    def test_with_budget(self) -> None:
        cfg = MemoryConfig(
            governance=GovernanceConfig(
                tool_chain_repair=True,
                budget=BudgetConfig(
                    governance_ratio=0.55,
                    protect_tokens=30_000,
                    min_gain_tokens=15_000,
                ),
            )
        )
        gov = create_governance(cfg)
        assert gov is not None
        assert len(gov._strategies) == 2  # ContextBudget + ToolChainRepair

    def test_budget_wires_params_from_config(self) -> None:
        cfg = MemoryConfig(
            session=SessionConfig(max_context_tokens=128_000),
            governance=GovernanceConfig(
                budget=BudgetConfig(
                    governance_ratio=0.50,
                    protect_tokens=20_000,
                    min_gain_tokens=10_000,
                    keep_recent=5,
                ),
            )
        )
        gov = create_governance(cfg)
        assert gov is not None
        budget_gov = gov._strategies[0]
        assert budget_gov._max_context_tokens == 128_000
        assert budget_gov._threshold == int(128_000 * 0.50)
        assert budget_gov._protect_tokens == 20_000
        assert budget_gov._min_gain == 10_000
        assert budget_gov._keep_recent == 5


class TestCreatePeerGovernance:
    def test_none_cfg_uses_defaults(self) -> None:
        """None cfg → minimal governance (ToolChainRepair)."""
        gov = create_subagent_governance(None)
        assert gov is not None
        assert len(gov._strategies) == 1

    def test_none_governance_uses_defaults(self) -> None:
        """cfg set but governance=None → default governance."""
        cfg = MemoryConfig(short_term=ShortTermConfig(max_context_tokens=50000), governance=None)
        gov = create_subagent_governance(cfg)
        assert gov is not None
        assert len(gov._strategies) == 1

    def test_subagent_minimal(self) -> None:
        cfg = MemoryConfig(governance=GovernanceConfig())
        gov = create_subagent_governance(cfg)
        assert gov is not None
        assert len(gov._strategies) == 1
