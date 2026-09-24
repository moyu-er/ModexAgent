"""Tests for the unified ``compact_session`` memory-system entry point.

Covers the T2 refactor that folded the history-side 10-kwarg
``cleanup_session`` free-function call into
``ContextManagedMemorySystem.compact_session``:

  - ABC default no-op (compaction-capability-free deployments);
  - in-flight dedup across concurrent calls for one session;
  - ``source`` passthrough to ``CleanupResult`` and hook payloads;
  - the append trigger path routed through the new interface.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from modex_agent.agents.summarizer.outcomes import CompactionOutcome
from modex_agent.agents.summarizer.session_compactor import SessionCompactorAgent
from modex_agent.core.message import ChatMessage
from modex_agent.memory.budget import ContextBudget
from modex_agent.memory.cleanup import CleanupResult, CompactionSource
from modex_agent.memory.core.system import ContextManagedMemorySystem
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.hooks import (
    CleanupFinishedHook,
    CleanupTriggeredHook,
    MemoryHookContext,
)
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.registry import DefaultMemoryStoreRegistry
from modex_agent.memory.scope import MemoryContext
from modex_agent.memory.token_estimator import TokenEstimator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# 90 messages × 104 tokens = 9_360 > 0.85 × 10_000 line → triggers; the
# absolute tail budget (2_500 tokens ≈ 24 msgs) lands far below the line so
# the in-flight dedup waiter re-checks an UNDER-threshold session.
_CLEANUP_CONFIG: dict[str, int | float] = {
    "max_context_tokens": 10_000,
    "max_token_ratio": 0.85,
}

_COMPACT_SUMMARY = (
    "## Objective\n- compact session objective\n\n## Work State\n### Completed\n- (none)\n"
)


def _ctx(session_id: str = "compact-session") -> MemoryContext:
    return MemoryContext(session_id=session_id, user_id="test-user")


class _FixedEstimator(TokenEstimator):
    """Every message counts as exactly 100 tokens (deterministic)."""

    def __init__(self) -> None:
        pass

    def estimate_text(self, text: str) -> int:
        _ = text
        return 100


class _SlowCountingCompactor(SessionCompactorAgent):
    """Counts ``compact()`` calls and yields so concurrent callers overlap.

    Reuses the real ``extract_topic`` so topic extraction stays covered.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def compact(
        self,
        messages: Sequence[dict[str, Any]],
        previous_summary: str | None = None,
        *,
        session_id: str = "session-compactor",
        budget: ContextBudget | None = None,
    ) -> CompactionOutcome:
        self.calls += 1
        await asyncio.sleep(0.05)  # keep the second caller waiting on the lock
        return CompactionOutcome(summary=_COMPACT_SUMMARY)

    @staticmethod
    def extract_topic(summary: str, max_chars: int = 200) -> str | None:
        return SessionCompactorAgent.extract_topic(summary, max_chars)


class _RecordingHook(CleanupTriggeredHook, CleanupFinishedHook):
    """Captures CLEANUP_TRIGGERED / CLEANUP_FINISHED dispatches."""

    def __init__(self) -> None:
        self.triggered_calls: list[MemoryHookContext] = []
        self.finished_calls: list[MemoryHookContext] = []

    async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
        self.triggered_calls.append(ctx)

    async def on_cleanup_finished(self, ctx: MemoryHookContext) -> None:
        self.finished_calls.append(ctx)


def _make_system(tmp_path: Path, **overrides: Any) -> DefaultMemorySystem:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    return DefaultMemorySystem(
        layer_set=layer_set,
        store_registry=registry,
        cleanup_config=dict(_CLEANUP_CONFIG),
        token_estimator=_FixedEstimator(),
        **overrides,
    )


async def _seed_pressure(system: DefaultMemorySystem, context: MemoryContext) -> None:
    """90 messages × 104 tokens = 9_360 tokens > 8_500 line -> triggers."""
    for i in range(90):
        await system.layers.session.add_messages(context, [{"role": "user", "content": f"u-{i}"}])


# ---------------------------------------------------------------------------
# 1. ABC default: compaction-free deployments are a safe no-op
# ---------------------------------------------------------------------------


class TestDefaultNoOp:
    async def test_subclass_without_override_returns_not_triggered(
        self, noop_memory_system: ContextManagedMemorySystem
    ) -> None:
        result = await noop_memory_system.compact_session(
            _ctx(), source=CompactionSource.POST_APPEND
        )
        assert isinstance(result, CleanupResult)
        assert result.triggered is False
        assert result.messages_pruned == 0
        assert result.source is None


# ---------------------------------------------------------------------------
# 2. In-flight dedup: concurrent calls for one session run one summary
# ---------------------------------------------------------------------------


class TestInFlightDedup:
    async def test_concurrent_calls_run_single_compaction(self, tmp_path: Path) -> None:
        compactor = _SlowCountingCompactor()
        system = _make_system(tmp_path, compactor=compactor)
        context = _ctx("dedup-session")
        await _seed_pressure(system, context)

        results = await asyncio.gather(
            system.compact_session(context, source=CompactionSource.POST_APPEND),
            system.compact_session(context, source=CompactionSource.PRE_LLM),
        )

        # Exactly one caller ran the pipeline; the lock waiter re-checked the
        # post-compaction state (under threshold) and returned triggered=False.
        assert sorted(r.triggered for r in results) == [False, True]
        assert compactor.calls == 1
        loser = results[0] if results[0].triggered is False else results[1]
        assert loser.messages_pruned == 0


# ---------------------------------------------------------------------------
# 3. Source passthrough to CleanupResult and hook payloads
# ---------------------------------------------------------------------------


class TestSourcePassthrough:
    @pytest.mark.parametrize("source", list(CompactionSource))
    async def test_source_reaches_result_and_finished_hook(
        self, tmp_path: Path, source: CompactionSource
    ) -> None:
        hook = _RecordingHook()
        system = _make_system(tmp_path)
        system.add_cleanup_hook(hook)
        context = _ctx(f"source-{source.value}")
        await _seed_pressure(system, context)

        result = await system.compact_session(context, source=source)

        assert result.triggered is True
        assert result.source == source
        finished = hook.finished_calls[-1]
        assert finished.cleanup_result is not None
        assert finished.cleanup_result.source == source

    async def test_under_threshold_result_carries_source(self, tmp_path: Path) -> None:
        """Even the no-trigger return labels its origin (write-side)."""
        system = _make_system(tmp_path)
        context = _ctx("under-threshold")

        result = await system.compact_session(context, source=CompactionSource.POST_APPEND)

        assert result.triggered is False
        assert result.source == CompactionSource.POST_APPEND


# ---------------------------------------------------------------------------
# 4. History append path routes through compact_session
# ---------------------------------------------------------------------------


class _CountingMemorySystem(DefaultMemorySystem):
    """Counts compact_session invocations, delegates to the real pipeline."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.compact_calls: list[CompactionSource] = []

    async def compact_session(
        self,
        context: MemoryContext,
        *,
        budget: ContextBudget | None = None,
        source: CompactionSource,
    ) -> CleanupResult:
        self.compact_calls.append(source)
        return await super().compact_session(context, budget=budget, source=source)


class TestHistoryAppendPath:
    async def test_append_trigger_calls_compact_session(self, tmp_path: Path) -> None:
        # max_context_tokens=10_000 -> line 8_500; 104-token messages (stamped
        # at append) trigger on the 82nd append (8_528 > 8_500).
        registry = DefaultMemoryStoreRegistry(tmp_path)
        system = _CountingMemorySystem(
            layer_set=MemoryLayerFactory.single_user(registry=registry),
            store_registry=registry,
            cleanup_config={
                "max_context_tokens": 10_000,
                "max_token_ratio": 0.85,
            },
            token_estimator=_FixedEstimator(),
            compactor=_SlowCountingCompactor(),
        )
        context = _ctx("append-path")
        history = system.create_message_history(context)

        for i in range(90):
            await history.append({"role": "user", "content": f"u-{i}"})

        assert system.compact_calls, "append trigger must go through compact_session"
        assert all(s == CompactionSource.POST_APPEND for s in system.compact_calls)

        # Compaction actually ran through the new interface: the refreshed
        # cache holds far fewer messages than were appended.
        msgs = await history.to_list()
        assert len(msgs) < 90
        assert all(isinstance(m, ChatMessage) for m in msgs)
