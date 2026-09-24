"""Tests for framework.ioc.factories.governance."""

from pathlib import Path
from typing import Any

import pytest

from modex_agent.core.session_id import SessionInfo
from modex_agent.ioc.configs.memory import (
    BudgetConfig,
    GovernanceConfig,
    MemoryConfig,
    SessionConfig,
    ShortTermConfig,
)
from modex_agent.ioc.factories.governance import create_governance, create_subagent_governance
from modex_agent.memory.budget import ContextBudget
from modex_agent.memory.cleanup import CleanupResult, CompactionSource
from modex_agent.memory.compaction_governance import MemoryCompactionGovernance
from modex_agent.memory.context_governance import (
    CompositeGovernance,
    ContextBudgetGovernance,
    ToolChainRepairGovernance,
)
from modex_agent.memory.core.system import ContextManagedMemorySystem
from modex_agent.memory.scope import MemoryContext
from modex_agent.memory.token_estimator import TokenEstimator


class _StubMemorySystem(ContextManagedMemorySystem):
    """Minimal concrete memory system for chain-wiring assertions."""

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    def add_cleanup_hook(self, hook: Any) -> None: ...

    def create_message_history(self, context: MemoryContext, initial_messages: Any = None, **_: Any) -> Any:  # noqa: ANN401
        raise NotImplementedError

    async def get_history(self, context: MemoryContext) -> list[Any]:
        return []

    async def clear(self, context: MemoryContext) -> None: ...

    async def ensure_within_budget(self, context: MemoryContext) -> None: ...


def _resolver(agent_ctx: Any) -> MemoryContext:
    return MemoryContext(session_id=agent_ctx.session.session_id, user_id="u")


class _TaggedEstimator(TokenEstimator):
    """Identity-tagged estimator — recognized by type, not behavior."""

    def estimate_text(self, text: str) -> int:
        _ = text
        return 1


_SESSION_INFO = SessionInfo(session_id="s1", agent_name="main")


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
            ),
        )
        gov = create_governance(cfg)
        assert gov is not None
        budget_gov = gov._strategies[0]
        assert budget_gov._max_context_tokens == 128_000
        assert budget_gov._threshold == int(128_000 * 0.50)
        assert budget_gov._protect_tokens == 20_000
        assert budget_gov._min_gain == 10_000
        assert budget_gov._keep_recent == 5


class TestCreateGovernanceMemoryChain:
    """Memory compaction rides the chain head (PRD §4.3.2 / D-2)."""

    def test_memory_strategy_is_chain_head(self) -> None:
        cfg = MemoryConfig(
            session=SessionConfig(max_context_tokens=64_000),
            governance=GovernanceConfig(),
        )
        gov = create_governance(
            cfg, memory_system=_StubMemorySystem(), memory_context_resolver=_resolver
        )
        assert gov is not None
        strategies = gov._strategies
        assert len(strategies) == 2
        assert isinstance(strategies[0], MemoryCompactionGovernance)
        assert isinstance(strategies[1], ToolChainRepairGovernance)
        # Fallback budget comes from the session config (the pool's window).
        assert strategies[0]._fallback_max_context_tokens == 64_000

    def test_full_chain_order_memory_budget_repair(self) -> None:
        cfg = MemoryConfig(
            session=SessionConfig(max_context_tokens=64_000),
            governance=GovernanceConfig(budget=BudgetConfig()),
        )
        gov = create_governance(
            cfg, memory_system=_StubMemorySystem(), memory_context_resolver=_resolver
        )
        assert gov is not None
        strategies = gov._strategies
        assert len(strategies) == 3
        assert isinstance(strategies[0], MemoryCompactionGovernance)
        assert isinstance(strategies[1], ContextBudgetGovernance)
        assert isinstance(strategies[2], ToolChainRepairGovernance)

    def test_memory_wiring_without_resolver_fails_fast(self) -> None:
        """Only one of the pair is a wiring bug: the resolver is the
        correctness constraint (same MemoryContext as load()), never
        silently skipped."""
        cfg = MemoryConfig(governance=GovernanceConfig())
        with pytest.raises(ValueError, match="together"):
            create_governance(cfg, memory_system=_StubMemorySystem())

    def test_repair_off_with_memory_keeps_memory_head(self) -> None:
        cfg = MemoryConfig(governance=GovernanceConfig(tool_chain_repair=False))
        gov = create_governance(
            cfg, memory_system=_StubMemorySystem(), memory_context_resolver=_resolver
        )
        assert gov is not None
        assert len(gov._strategies) == 1
        assert isinstance(gov._strategies[0], MemoryCompactionGovernance)

    def test_trigger_ratio_threads_from_session_config(self) -> None:
        """The read-side pre-check's ratio comes from the SAME session
        config as the fallback limit — one threshold source, not a constant
        drifting away from ``SessionConfig.max_token_ratio``."""
        cfg = MemoryConfig(
            session=SessionConfig(max_context_tokens=64_000, max_token_ratio=0.4),
            governance=GovernanceConfig(),
        )
        gov = create_governance(
            cfg, memory_system=_StubMemorySystem(), memory_context_resolver=_resolver
        )
        assert gov is not None
        strategy = gov._strategies[0]
        assert isinstance(strategy, MemoryCompactionGovernance)
        assert strategy._fallback_max_context_tokens == 64_000
        assert strategy._trigger_ratio == 0.4


class TestEstimatorConvergence:
    """The memory strategy reuses the injected memory system's estimator."""

    def test_real_system_estimator_used_when_not_explicitly_passed(self, tmp_path: Path) -> None:
        from modex_agent.memory.default_system import DefaultMemorySystem
        from modex_agent.memory.layers.factory import MemoryLayerFactory
        from modex_agent.memory.registry import DefaultMemoryStoreRegistry

        registry = DefaultMemoryStoreRegistry(tmp_path)
        system = DefaultMemorySystem(
            layer_set=MemoryLayerFactory.single_user(registry=registry),
            store_registry=registry,
            token_estimator=_TaggedEstimator(),
        )
        cfg = MemoryConfig(governance=GovernanceConfig())
        gov = create_governance(
            cfg, memory_system=system, memory_context_resolver=_resolver
        )
        assert gov is not None
        strategy = gov._strategies[0]
        assert isinstance(strategy, MemoryCompactionGovernance)
        # The governance judges pressure with the system's estimator, not a
        # silently-diverging CharTokenEstimator default.
        assert strategy._estimator is system.token_estimator
        assert isinstance(strategy._estimator, _TaggedEstimator)

    def test_explicit_estimator_wins_over_system_estimator(self) -> None:
        cfg = MemoryConfig(governance=GovernanceConfig())
        explicit = _TaggedEstimator()
        gov = create_governance(
            cfg,
            token_estimator=explicit,
            memory_system=_StubMemorySystem(),
            memory_context_resolver=_resolver,
        )
        assert gov is not None
        strategy = gov._strategies[0]
        assert isinstance(strategy, MemoryCompactionGovernance)
        assert strategy._estimator is explicit


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

    def test_subagent_with_memory_gets_same_head(self) -> None:
        """A subagent wired with a memory system gets the same compaction
        head as the main agent (convergence rule 1: one chain shape)."""
        cfg = MemoryConfig(governance=GovernanceConfig())
        gov = create_subagent_governance(
            cfg, memory_system=_StubMemorySystem(), memory_context_resolver=_resolver
        )
        assert gov is not None
        strategies = gov._strategies
        assert len(strategies) == 2
        assert isinstance(strategies[0], MemoryCompactionGovernance)
        assert isinstance(strategies[1], ToolChainRepairGovernance)


class TestMemoryStrategyCompactionCall:
    async def test_strategy_drives_compact_session_pre_llm(self, tmp_path: Any) -> None:
        """The built memory strategy really drives compact_session with the
        session-config fallback limit and the PRE_LLM source label."""

        class _Recording(_StubMemorySystem):
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def compact_session(
                self,
                context: MemoryContext,
                *,
                budget: ContextBudget | None = None,
                source: CompactionSource,
            ) -> CleanupResult:
                self.calls.append({"budget": budget, "source": source})
                return CleanupResult(triggered=False, source=source)

        system = _Recording()
        cfg = MemoryConfig(
            session=SessionConfig(max_context_tokens=64_000), governance=GovernanceConfig()
        )
        gov: CompositeGovernance | None = create_governance(
            cfg, memory_system=system, memory_context_resolver=_resolver
        )
        assert gov is not None

        from modex_agent.core.agent import AgentContext
        from modex_agent.memory.history import ListMessageHistory
        from modex_agent.tools.manager import InMemoryToolManager

        agent_ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=_SESSION_INFO,
            runtime=None,
        )

        messages = [{"role": "user", "content": "x", "token_count": 60_000}]
        # 60k tokens > 0.85 × 64k = 54.4k → over the line.
        result = await gov.apply(messages, agent_ctx)

        assert len(system.calls) == 1
        assert system.calls[0]["budget"] == ContextBudget(max_context_tokens=64_000)
        assert system.calls[0]["source"] == CompactionSource.PRE_LLM
        # Initiated → rebuilt: [no system] + refreshed to_messages()
        assert result == []

    async def test_trigger_line_follows_configured_ratio(self, tmp_path: Any) -> None:
        """Non-default ratio moves the trigger line: 40k tokens vs a 64k
        limit sits UNDER the default 0.85 line (54.4k) but OVER a configured
        0.5 line (32k) — the session-config ratio must drive the judgment."""

        class _Recording(_StubMemorySystem):
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def compact_session(
                self,
                context: MemoryContext,
                *,
                budget: ContextBudget | None = None,
                source: CompactionSource,
            ) -> CleanupResult:
                self.calls.append({"budget": budget, "source": source})
                return CleanupResult(triggered=False, source=source)

        system = _Recording()
        cfg = MemoryConfig(
            session=SessionConfig(max_context_tokens=64_000, max_token_ratio=0.5),
            governance=GovernanceConfig(),
        )
        gov: CompositeGovernance | None = create_governance(
            cfg, memory_system=system, memory_context_resolver=_resolver
        )
        assert gov is not None

        from modex_agent.core.agent import AgentContext
        from modex_agent.memory.history import ListMessageHistory
        from modex_agent.tools.manager import InMemoryToolManager

        agent_ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=_SESSION_INFO,
            runtime=None,
        )

        messages = [{"role": "user", "content": "x", "token_count": 40_000}]
        # 40k > 0.5 × 64k = 32k (would NOT cross the 0.85 line).
        await gov.apply(messages, agent_ctx)

        assert len(system.calls) == 1
