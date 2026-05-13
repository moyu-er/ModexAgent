"""Tests for framework.ioc.factories.governance."""

from unittest.mock import MagicMock

import pytest

from framework.ioc.configs.memory import (
    GovernanceConfig,
    LossyConfig,
    MemoryConfig,
    ShortTermConfig,
    TokenBudgetConfig,
)
from framework.ioc.factories.governance import create_governance, create_peer_governance


class TestCreateGovernance:
    def test_none_cfg_returns_none(self) -> None:
        assert create_governance(None, llm_max_tokens=80000) is None

    def test_disabled_by_tool_chain_repair(self) -> None:
        cfg = MemoryConfig(governance=GovernanceConfig(tool_chain_repair=False))
        assert create_governance(cfg, llm_max_tokens=80000) is None

    def test_minimal_governance(self) -> None:
        """Only ToolChainRepair + FinalContextLegality when governance is bare."""
        cfg = MemoryConfig(governance=GovernanceConfig())
        gov = create_governance(cfg, llm_max_tokens=80000)
        assert gov is not None
        # 2 strategies: ToolChainRepair + FinalContextLegality
        assert len(gov._strategies) == 2

    def test_with_token_budget(self) -> None:
        cfg = MemoryConfig(
            governance=GovernanceConfig(
                tool_chain_repair=True,
                token_budget=TokenBudgetConfig(budget_ratio=0.5, safety_buffer=1024),
            )
        )
        gov = create_governance(cfg, llm_max_tokens=100000)
        assert gov is not None
        assert len(gov._strategies) == 3

    def test_with_lossy_compaction(self) -> None:
        cfg = MemoryConfig(
            governance=GovernanceConfig(
                tool_chain_repair=True,
                lossy_compaction=LossyConfig(tool_result_head_chars=800, assistant_head_chars=800),
            )
        )
        gov = create_governance(cfg, llm_max_tokens=80000)
        assert gov is not None
        assert len(gov._strategies) == 3

    def test_full_governance(self) -> None:
        cfg = MemoryConfig(
            governance=GovernanceConfig(
                tool_chain_repair=True,
                token_budget=TokenBudgetConfig(budget_ratio=0.3, safety_buffer=512),
                lossy_compaction=LossyConfig(),
            )
        )
        gov = create_governance(cfg, llm_max_tokens=80000)
        assert gov is not None
        assert len(gov._strategies) == 4


class TestCreatePeerGovernance:
    def test_none_cfg_uses_defaults(self) -> None:
        """None cfg → default governance with budget_ratio=0.3, safety_buffer=512."""
        gov = create_peer_governance(None, llm_max_tokens=80000)
        assert gov is not None
        assert len(gov._strategies) == 3  # ToolChainRepair + PriorityBudget + FinalContextLegality

    def test_none_governance_uses_defaults(self) -> None:
        """cfg set but governance=None → default governance."""
        cfg = MemoryConfig(short_term=ShortTermConfig(max_messages=50), governance=None)
        gov = create_peer_governance(cfg, llm_max_tokens=80000)
        assert gov is not None
        assert len(gov._strategies) == 3

    def test_peer_minimal(self) -> None:
        cfg = MemoryConfig(governance=GovernanceConfig())
        gov = create_peer_governance(cfg, llm_max_tokens=80000)
        assert gov is not None
        assert len(gov._strategies) == 2  # ToolChainRepair + FinalContextLegality

    def test_peer_with_budget(self) -> None:
        cfg = MemoryConfig(
            governance=GovernanceConfig(
                token_budget=TokenBudgetConfig(budget_ratio=0.3, safety_buffer=512),
            )
        )
        gov = create_peer_governance(cfg, llm_max_tokens=80000)
        assert gov is not None
        assert len(gov._strategies) == 3
