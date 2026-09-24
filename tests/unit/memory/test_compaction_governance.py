"""Tests for ``MemoryCompactionGovernance`` (per-model compaction PRD §4.3.2).

Covers the read-side compaction trigger face against REAL memory machinery
(``DefaultMemorySystem`` + ``ScopedMessageHistory`` + registry-backed store):

  - over-budget payload → ``compact_session(source=PRE_LLM, max_context_tokens=
    current-turn limit)`` runs once; the returned list is ``[system] + the
    refreshed history`` and genuinely smaller;
  - under budget / no limit → zero rewrite, compaction never invoked;
  - rebuild happens whenever compaction was INITIATED (even triggered=False —
    stale-view/concurrent-writer protection);
  - governance failure is swallowed (best-effort layer);
  - the hard constraint: the resolver returns the SAME MemoryContext
    ``MemorySystemContextManager.load()`` built for the session;
  - per-history budget: ``model_info`` limit drives the append trigger;
  - governance and history judge through the same priority chain.

Both trigger faces share one budget chain, so tests that isolate the
governance face seed the history under a LARGE static config (append face
quiet) and hand the small limit only to the governance (fallback or turn
model_info).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from modex_agent.agents.summarizer.session_compactor import SessionCompactorAgent
from modex_agent.core.agent import AgentContext
from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.llm_struct import LLMResponse
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.budget import ContextBudget
from modex_agent.memory.cleanup import CleanupResult, CompactionSource
from modex_agent.memory.compaction_governance import MemoryCompactionGovernance
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.history import ListMessageHistory
from modex_agent.memory.injection import FullInjectionPolicy
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.registry import DefaultMemoryStoreRegistry
from modex_agent.memory.scope import MemoryContext
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.memory.token_estimator import TokenEstimator
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.manager import InMemoryToolManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FixedEstimator(TokenEstimator):
    """Every message counts as exactly 100 tokens (deterministic).

    Sized so a 90-message seed (9_360 tokens) crosses a 10_000-token limit's
    0.85 line AND leaves prunable headroom above the 2_000-token tail floor.
    """

    def __init__(self, per_message: int = 100) -> None:
        self.per_message = per_message

    def estimate_text(self, text: str) -> int:
        _ = text
        return self.per_message


class _NullCompactor:
    """Compaction summary generator stand-in: deterministic output."""

    async def compact(
        self,
        messages: Sequence[dict[str, Any]],
        previous_summary: str | None = None,
        *,
        session_id: str = "session-compactor",
        budget: ContextBudget | None = None,
    ) -> Any:
        from modex_agent.agents.summarizer.outcomes import CompactionOutcome

        _ = messages, previous_summary, session_id, budget
        return CompactionOutcome(
            summary="## Objective\n- compacted\n\n## Work State\n### Completed\n- (none)\n"
        )

    @staticmethod
    def extract_topic(summary: str, max_chars: int = 200) -> str | None:
        from modex_agent.agents.summarizer.session_compactor import SessionCompactorAgent

        return SessionCompactorAgent.extract_topic(summary, max_chars)


class _CountingSystem(DefaultMemorySystem):
    """Records compact_session invocations; delegates to the real pipeline."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.compact_calls: list[dict[str, Any]] = []

    async def compact_session(
        self,
        context: MemoryContext,
        *,
        budget: ContextBudget | None = None,
        source: CompactionSource,
    ) -> CleanupResult:
        result = await super().compact_session(context, budget=budget, source=source)
        self.compact_calls.append(
            {
                "budget": budget,
                "source": source,
                "triggered": result.triggered,
            }
        )
        return result

    def pre_llm_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.compact_calls if c["source"] == CompactionSource.PRE_LLM]


def _make_system(tmp_path: Path, cleanup_config: dict[str, int | float]) -> _CountingSystem:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    return _CountingSystem(
        layer_set=MemoryLayerFactory.single_user(registry=registry),
        store_registry=registry,
        cleanup_config=cleanup_config,
        token_estimator=_FixedEstimator(),
        compactor=_NullCompactor(),
    )


def _make_context_manager(system: DefaultMemorySystem) -> MemorySystemContextManager:
    return MemorySystemContextManager(
        memory_system=system,
        default_agent_id="main",
        default_agent_role="main",
        injection_policy=FullInjectionPolicy(),
    )


def _agent_context(history: Any, model_info: ModelInfo | None) -> AgentContext:
    runtime = None
    if model_info is not None:
        services = AgentRuntimeServices(model_info=model_info)
        runtime = AgentRuntime(services=services, state=None)  # type: ignore[arg-type]
    return AgentContext(
        system_prompt="sys",
        history=history,
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("gov-session.main"),
        runtime=runtime,
    )


async def _seed(history: Any, count: int) -> None:
    for i in range(count):
        await history.append({"role": "user", "content": f"u-{i}"})


async def _assembled(history: Any) -> list[dict[str, Any]]:
    """[system?] + history — the LLMNode._build_messages shape."""
    return [
        {"role": "system", "content": "sys"},
        *[m.to_dict() for m in (await history.to_list())],
    ]


# ---------------------------------------------------------------------------
# 1. Over-budget → compact once, rebuild from refreshed history
# ---------------------------------------------------------------------------


class TestOverBudgetCompaction:
    async def test_compacts_once_and_rebuilds_smaller(self, tmp_path: Path) -> None:
        # Static config huge → the append face stays quiet while seeding;
        # the governance's small fallback limit is what the pre-LLM face sees.
        system = _make_system(tmp_path, {"max_context_tokens": 200_000})
        context_manager = _make_context_manager(system)
        state = await context_manager.load("gov-session.main")
        history = state.history
        await _seed(history, 90)
        assert len(await history.to_list()) == 90

        governance = MemoryCompactionGovernance(
            memory_system=system,
            memory_context_resolver=lambda actx: context_manager.resolve_memory_context(
                actx.session.session_id
            ),
            fallback_max_context_tokens=10_000,  # 9_360 tokens > 0.85 × 10_000
            token_estimator=_FixedEstimator(),
        )
        ctx = _agent_context(history, model_info=None)
        messages = await _assembled(history)

        result = await governance.apply(messages, ctx)

        # compact_session ran exactly once on the read side, with the
        # governance-resolved budget, and actually compacted.
        pre_llm = system.pre_llm_calls()
        assert len(pre_llm) == 1
        assert pre_llm[0]["budget"] == ContextBudget(max_context_tokens=10_000)
        assert pre_llm[0]["triggered"] is True

        # Rebuilt shape: [system] + refreshed history, genuinely smaller.
        assert result[0]["role"] == "system"
        assert len(result) < len(messages)
        assert len(await history.to_list()) < 90

    async def test_turn_limit_wins_over_fallback(self, tmp_path: Path) -> None:
        # History loaded without runtime_info (static 200k, append face
        # quiet); the turn's model_info carries the small window the
        # governance must use instead of its own static fallback.
        system = _make_system(tmp_path, {"max_context_tokens": 200_000})
        context_manager = _make_context_manager(system)
        state = await context_manager.load("gov-session.main")
        history = state.history
        await _seed(history, 90)

        governance = MemoryCompactionGovernance(
            memory_system=system,
            memory_context_resolver=lambda actx: context_manager.resolve_memory_context(
                actx.session.session_id
            ),
            fallback_max_context_tokens=200_000,
            token_estimator=_FixedEstimator(),
        )
        ctx = _agent_context(
            history, model_info=ModelInfo(model_name="small", context_limit=10_000)
        )
        messages = await _assembled(history)

        result = await governance.apply(messages, ctx)

        pre_llm = system.pre_llm_calls()
        assert len(pre_llm) == 1
        assert pre_llm[0]["budget"] == ContextBudget(max_context_tokens=10_000)
        assert len(result) < len(messages)


# ---------------------------------------------------------------------------
# 2. Zero-rewrite fast paths
# ---------------------------------------------------------------------------


class TestZeroRewritePaths:
    async def test_under_budget_returns_equal_content_uncalled(self, tmp_path: Path) -> None:
        system = _make_system(tmp_path, {"max_context_tokens": 200_000})
        context_manager = _make_context_manager(system)
        state = await context_manager.load("gov-session.main")
        history = state.history
        await _seed(history, 10)

        governance = MemoryCompactionGovernance(
            memory_system=system,
            memory_context_resolver=lambda actx: context_manager.resolve_memory_context(
                actx.session.session_id
            ),
            fallback_max_context_tokens=10_000,  # 100 tokens ≪ 8.5k line
            token_estimator=_FixedEstimator(),
        )
        messages = await _assembled(history)

        result = await governance.apply(messages, _agent_context(history, model_info=None))

        assert result == messages
        assert system.pre_llm_calls() == []

    async def test_no_limit_returns_as_is(self, tmp_path: Path) -> None:
        system = _make_system(tmp_path, {})
        context_manager = _make_context_manager(system)
        state = await context_manager.load("gov-session.main")
        history = state.history
        await _seed(history, 10)

        governance = MemoryCompactionGovernance(
            memory_system=system,
            memory_context_resolver=lambda actx: context_manager.resolve_memory_context(
                actx.session.session_id
            ),
            fallback_max_context_tokens=None,  # nothing declared anywhere
            token_estimator=_FixedEstimator(),
        )
        messages = await _assembled(history)

        result = await governance.apply(messages, _agent_context(history, model_info=None))

        assert result == messages
        # No limit anywhere: no compaction ever triggers on either face
        # (the single first-append POST_APPEND no-op probe is the history's
        # pre-existing empty-cache behavior and triggers nothing).
        assert [c for c in system.compact_calls if c["triggered"]] == []
        assert system.pre_llm_calls() == []


# ---------------------------------------------------------------------------
# 3. Rebuild-on-initiation even when the result says nothing happened
# ---------------------------------------------------------------------------


class TestRebuildOnInitiation:
    async def test_triggered_false_still_rebuilds_stale_view(self, tmp_path: Path) -> None:
        """The write side compacted just before our call: our compact_session
        re-check says under-threshold (triggered=False), but the history cache
        holds a stale pre-compaction view — initiation still rebuilds."""

        class _NoOpSystem(DefaultMemorySystem):
            async def compact_session(
                self,
                context: MemoryContext,
                *,
                budget: ContextBudget | None = None,
                source: CompactionSource,
            ) -> CleanupResult:
                return CleanupResult(triggered=False, source=source)

        registry = DefaultMemoryStoreRegistry(tmp_path)
        system = _NoOpSystem(
            layer_set=MemoryLayerFactory.single_user(registry=registry),
            store_registry=registry,
            cleanup_config={"max_context_tokens": 200_000},
            token_estimator=_FixedEstimator(),
        )
        context_manager = _make_context_manager(system)
        state = await context_manager.load("gov-session.main")
        history = state.history
        await _seed(history, 20)

        # Concurrent writer shrinks the store behind the history's cache.
        memory_context = context_manager.resolve_memory_context("gov-session.main")
        all_messages = await system.layers.session.get_all_messages(memory_context)
        await system.layers.session.replace_messages(memory_context, all_messages[:2])
        assert len(await history.to_list()) == 20  # stale cache

        governance = MemoryCompactionGovernance(
            memory_system=system,
            memory_context_resolver=lambda actx: context_manager.resolve_memory_context(
                actx.session.session_id
            ),
            fallback_max_context_tokens=10,  # 200 tokens ≫ 8.5 line → fires
            token_estimator=_FixedEstimator(),
        )
        messages = await _assembled(history)
        assert len(messages) == 21

        result = await governance.apply(messages, _agent_context(history, model_info=None))

        # The no-op compact returned triggered=False; the rebuild still
        # refreshed the stale view.
        assert len(result) < len(messages)
        assert result[0]["role"] == "system"
        assert len(await history.to_list()) == 2


# ---------------------------------------------------------------------------
# 4. Best-effort: failures never break the LLM call
# ---------------------------------------------------------------------------


class TestExceptionSafety:
    async def test_compaction_failure_passes_messages_through(self, tmp_path: Path) -> None:
        class _ExplodingSystem(DefaultMemorySystem):
            async def compact_session(
                self,
                context: MemoryContext,
                *,
                budget: ContextBudget | None = None,
                source: CompactionSource,
            ) -> CleanupResult:
                raise RuntimeError("compaction backend down")

        registry = DefaultMemoryStoreRegistry(tmp_path)
        system = _ExplodingSystem(
            layer_set=MemoryLayerFactory.single_user(registry=registry),
            store_registry=registry,
            cleanup_config={},
            token_estimator=_FixedEstimator(),
        )
        governance = MemoryCompactionGovernance(
            memory_system=system,
            memory_context_resolver=lambda actx: MemoryContext(
                session_id=actx.session.session_id, user_id="u"
            ),
            fallback_max_context_tokens=10,
            token_estimator=_FixedEstimator(),
        )
        history = ListMessageHistory([{"role": "user", "content": f"u-{i}"} for i in range(10)])
        messages = await _assembled(history)

        result = await governance.apply(messages, _agent_context(history, model_info=None))

        assert result == messages  # original list, untouched


# ---------------------------------------------------------------------------
# 5. Hard constraint: same MemoryContext as load()
# ---------------------------------------------------------------------------


class TestContextIdentity:
    async def test_resolver_returns_loads_context_instance(self, tmp_path: Path) -> None:
        system = _make_system(tmp_path, {"max_context_tokens": 200_000})
        context_manager = _make_context_manager(system)
        await context_manager.load("gov-session.main")

        resolved = context_manager.resolve_memory_context("gov-session.main")
        # Same cached instance load() built (cache-or-build is the single
        # owner of context identity), and a stable identity across calls.
        assert resolved is context_manager._context_cache["gov-session.main"]
        assert context_manager.resolve_memory_context("gov-session.main") is resolved
        assert resolved == MemoryContext(
            session_id="gov-session.main",
            user_id="default",
            agent_id="main",
            agent_role="main",
        )


# ---------------------------------------------------------------------------
# 6. Per-history budget: model_info drives the append trigger
# ---------------------------------------------------------------------------


class TestPerHistoryBudget:
    async def test_model_info_limit_triggers_append_compaction(self, tmp_path: Path) -> None:
        # Static config huge; per-turn model_info small → the append check
        # must use the TURN limit and trigger compaction mid-append.
        system = _make_system(tmp_path, {"max_context_tokens": 200_000})
        context = MemoryContext(session_id="per-turn-append", user_id="u")

        history = system.create_message_history(
            context, model_info=ModelInfo(model_name="small", context_limit=10_000)
        )
        # 104 tokens/message, limit 10_000 → line 8_500: the 82nd append fires.
        await _seed(history, 90)

        triggered = [c for c in system.compact_calls if c["triggered"]]
        assert triggered, "per-turn model limit must drive the append trigger"
        assert all(c["source"] == CompactionSource.POST_APPEND for c in triggered)
        assert all(c["budget"] == ContextBudget(max_context_tokens=10_000) for c in triggered)
        assert len(await history.to_list()) < 90

    async def test_static_limit_only_does_not_trigger(self, tmp_path: Path) -> None:
        # No model_info; static limit huge → no compaction ever fires.
        system = _make_system(tmp_path, {"max_context_tokens": 200_000})
        context = MemoryContext(session_id="static-only", user_id="u")

        history = system.create_message_history(context)
        await _seed(history, 10)

        assert [c for c in system.compact_calls if c["triggered"]] == []
        assert len(await history.to_list()) == 10


# ---------------------------------------------------------------------------
# 7. Governance and history share the priority chain
# ---------------------------------------------------------------------------


class TestSharedResolverChain:
    async def test_governance_and_history_agree_on_the_turn_limit(self, tmp_path: Path) -> None:
        # A model_info whose window leaves the append face untriggered must
        # also leave the governance pre-check untriggered — both faces go
        # through resolve_effective_budget + check_cleanup_trigger, so the
        # governance's bogus static fallback (1) never reaches the judgment.
        system = _make_system(tmp_path, {"max_context_tokens": 200_000})
        context = MemoryContext(session_id="shared-chain", user_id="u")
        model_info = ModelInfo(model_name="m", context_limit=2_000)

        history = system.create_message_history(context, model_info=model_info)
        await _seed(history, 10)  # 1_040 tokens vs line 1_700 → append face quiet
        assert [c for c in system.compact_calls if c["triggered"]] == []

        governance = MemoryCompactionGovernance(
            memory_system=system,
            memory_context_resolver=lambda actx: MemoryContext(
                session_id=actx.session.session_id, user_id="u"
            ),
            fallback_max_context_tokens=1,  # wrong static value on purpose
            token_estimator=_FixedEstimator(),
        )
        messages = await _assembled(history)

        result = await governance.apply(messages, _agent_context(history, model_info))

        # The turn limit (200) won over the bogus fallback (1): both faces
        # judged the same 100-token pressure as within budget.
        assert result == messages
        assert system.pre_llm_calls() == []


# ---------------------------------------------------------------------------
# 8. MessageHistory.refresh() contract
# ---------------------------------------------------------------------------


class TestHistoryRefresh:
    async def test_scoped_refresh_invalidates_cache(self, tmp_path: Path) -> None:
        system = _make_system(tmp_path, {"max_context_tokens": 200_000})
        context_manager = _make_context_manager(system)
        state = await context_manager.load("gov-session.main")
        history = state.history
        await _seed(history, 10)
        assert len(await history.to_list()) == 10

        await history.refresh()

        memory_context = context_manager.resolve_memory_context("gov-session.main")
        all_messages = await system.layers.session.get_all_messages(memory_context)
        await system.layers.session.replace_messages(memory_context, all_messages[:2])
        assert len(await history.to_list()) == 2  # re-read after refresh

    async def test_list_history_refresh_is_noop(self) -> None:
        history = ListMessageHistory([{"role": "user", "content": "x"}])
        await history.refresh()  # optional no-op default
        assert len(await history.to_list()) == 1


# ---------------------------------------------------------------------------
# 9. Mid-session model switch: history exceeds the NEW model's window —
#    the pre-LLM face compacts at the new limit and SEGMENTED summarization
#    completes (map + reduce), leaving the rebuilt context within budget.
# ---------------------------------------------------------------------------


class _SwitchSummaryProvider(CallbackStreamProvider):
    """Counts calls; answers every summarization request with a short summary."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Any = None,
        on_reasoning_delta: Any = None,
        **kwargs: Any,
    ) -> LLMResponse:
        del messages, model, temperature, tools, on_content_delta, on_reasoning_delta, kwargs
        self.calls += 1
        return LLMResponse(content="## Objective\nswitch compact summary")

    def get_default_model(self) -> str:
        return "small"


class TestModelSwitchSegmentedCompaction:
    async def test_switch_to_smaller_model_segments_and_completes(
        self, tmp_path: Path
    ) -> None:
        # Real compactor (not _NullCompactor): the pruned transcript must be
        # summarized even though it exceeds the new model's window.
        provider = _SwitchSummaryProvider()
        compactor = SessionCompactorAgent(provider)
        registry = DefaultMemoryStoreRegistry(tmp_path)
        system = _CountingSystem(
            layer_set=MemoryLayerFactory.single_user(registry=registry),
            store_registry=registry,
            cleanup_config={"max_context_tokens": 200_000},
            token_estimator=_FixedEstimator(),
            compactor=compactor,
        )
        context_manager = _make_context_manager(system)
        state = await context_manager.load("gov-session.main")
        history = state.history
        # 90 turns × 400 chars ≈ 100+ char-estimated tokens each — far past
        # the small model's window, past one summary segment too.
        for i in range(90):
            await history.append(
                {"role": "user", "content": f"u-{i} " + "x" * 400}
            )

        governance = MemoryCompactionGovernance(
            memory_system=system,
            memory_context_resolver=lambda actx: context_manager.resolve_memory_context(
                actx.session.session_id
            ),
            fallback_max_context_tokens=200_000,
            token_estimator=_FixedEstimator(),
        )

        # Before the switch: the big-window model fits — zero-rewrite path.
        big = _agent_context(
            history, ModelInfo(model_name="big", context_limit=200_000)
        )
        out = await governance.apply(await _assembled(history), big)
        assert len(out) == 91  # system + 90, untouched
        assert system.pre_llm_calls() == []

        # After the switch: the small model's limit drives the pre-LLM face.
        small = _agent_context(
            history, ModelInfo(model_name="small", context_limit=6_000)
        )
        out = await governance.apply(await _assembled(history), small)

        pre_llm = system.pre_llm_calls()
        assert len(pre_llm) == 1
        assert pre_llm[0]["budget"] is not None
        assert pre_llm[0]["budget"].max_context_tokens == 6_000
        assert pre_llm[0]["triggered"] is True
        # Segmented summarization completed: several map calls + one reduce
        # (a single-pass call cannot fit the pruned transcript).
        assert provider.calls >= 3
        # Rebuilt context: system + pruned tail, within the small window
        # (store-side pressure ≈ kept × 100 ≪ threshold).
        assert len(out) == 1 + len(await history.to_list())
        assert len(out) < 91


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
