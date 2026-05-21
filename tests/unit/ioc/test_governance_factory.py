"""Tests for framework.ioc.factories.governance."""

import pytest

from framework.ioc.configs.memory import (
    GovernanceConfig,
    LossyConfig,
    MemoryConfig,
    ShortTermConfig,
)
from framework.ioc.factories.governance import create_governance, create_subagent_governance


class TestCreateGovernance:
    def test_none_cfg_returns_none(self) -> None:
        assert create_governance(None, llm_max_tokens=80000) is None

    def test_disabled_by_tool_chain_repair(self) -> None:
        cfg = MemoryConfig(governance=GovernanceConfig(tool_chain_repair=False))
        assert create_governance(cfg, llm_max_tokens=80000) is None

    def test_minimal_governance(self) -> None:
        """ToolChainRepair + FinalContextLegality when governance is bare."""
        cfg = MemoryConfig(governance=GovernanceConfig())
        gov = create_governance(cfg, llm_max_tokens=80000)
        assert gov is not None
        assert len(gov._strategies) == 2  # ToolChainRepair + FinalContextLegality

    def test_with_lossy_compaction(self) -> None:
        cfg = MemoryConfig(
            governance=GovernanceConfig(
                tool_chain_repair=True,
                lossy_compaction=LossyConfig(
                    tool_result_head_chars=800,
                    assistant_head_chars=800,
                    tool_args_head_chars=1024,
                ),
            )
        )
        gov = create_governance(cfg, llm_max_tokens=80000)
        assert gov is not None
        assert len(gov._strategies) == 3  # LossyCompaction + ToolChainRepair + FinalContextLegality

    def test_lossy_wires_tool_args(self) -> None:
        """LossyContentCompactionGovernance receives tool_args_head_chars from config."""
        cfg = MemoryConfig(
            governance=GovernanceConfig(
                lossy_compaction=LossyConfig(tool_args_head_chars=4096),
            )
        )
        gov = create_governance(cfg, llm_max_tokens=80000)
        assert gov is not None
        assert gov._strategies[0]._tool_args_head_chars == 4096


class TestCreatePeerGovernance:
    def test_none_cfg_uses_defaults(self) -> None:
        """None cfg → minimal governance (ToolChainRepair + FinalContextLegality)."""
        gov = create_subagent_governance(None, llm_max_tokens=80000)
        assert gov is not None
        assert len(gov._strategies) == 2

    def test_none_governance_uses_defaults(self) -> None:
        """cfg set but governance=None → default governance."""
        cfg = MemoryConfig(short_term=ShortTermConfig(max_messages=50), governance=None)
        gov = create_subagent_governance(cfg, llm_max_tokens=80000)
        assert gov is not None
        assert len(gov._strategies) == 2

    def test_peer_minimal(self) -> None:
        cfg = MemoryConfig(governance=GovernanceConfig())
        gov = create_subagent_governance(cfg, llm_max_tokens=80000)
        assert gov is not None
        assert len(gov._strategies) == 2
