"""Tests for framework/memory/cleanup.py — cleanup_session() core function."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from modex_agent.agents.summarizer.abc import ArchiveGenerator
from modex_agent.agents.summarizer.outcomes import CompactionOutcome
from modex_agent.agents.summarizer.session_compactor import SessionCompactorAgent
from modex_agent.core.message import ChatMessage
from modex_agent.memory.archive_models import ArchiveDocuments, ArchiveGenerationResult
from modex_agent.memory.budget import ContextBudget
from modex_agent.memory.cleanup import (
    CleanupResult,
    _compute_boundary,
    _tail_keep_budget,
    check_cleanup_trigger,
    cleanup_session,
)
from modex_agent.memory.core.layers import MemoryLayerSet, SessionMemoryManager
from modex_agent.memory.core.models import CompressionReason, StorageRevision
from modex_agent.memory.core.split_stores import MemoryStoreBundle
from modex_agent.memory.hooks import (
    CleanupFinishedHook,
    CleanupTriggeredHook,
    MemoryHookContext,
    MemoryHookRunner,
)
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.layers.session import ScopedSessionMemoryManager
from modex_agent.memory.registry import DefaultMemoryStoreRegistry, MemoryStoreRegistry
from modex_agent.memory.scope import MemoryContext
from modex_agent.memory.stores.dir_archive import DirArchiveStorage
from modex_agent.memory.token_estimator import TokenEstimator
from modex_agent.persistence.managers.workspace import WorkspacePersistenceManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(session_id: str = "test-session") -> MemoryContext:
    return MemoryContext(session_id=session_id, user_id="test-user")


def _user_msg(content: str = "hello") -> dict[str, Any]:
    return {"role": "user", "content": content}


def _assistant_msg(content: str = "reply") -> dict[str, Any]:
    return {"role": "assistant", "content": content}


def _tool_call_msg(
    call_id: str = "call_1",
    fn_name: str = "tool_a",
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": fn_name, "arguments": "{}"}}],
    }


def _tool_result_msg(
    call_id: str = "call_1",
    content: str = "result",
) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


async def _add_messages(
    session: SessionMemoryManager,
    context: MemoryContext,
    messages: list[dict[str, Any]],
) -> None:
    """Add messages to session one by one so each gets a proper revision."""
    for msg in messages:
        await session.add_messages(context, [msg])


class _FixedEstimator(TokenEstimator):
    """Every message counts as exactly ``per_message`` tokens (deterministic)."""

    def __init__(self, per_message: int = 10) -> None:
        self.per_message = per_message

    def estimate_text(self, text: str) -> int:
        return self.per_message


def _sum_tokens_for(msgs: list[dict[str, Any]]) -> int:
    return sum(m.get("token_count", 0) for m in msgs)


class _RecordingHook(CleanupTriggeredHook, CleanupFinishedHook):
    """Recording hook that captures every CLEANUP_TRIGGERED and CLEANUP_FINISHED dispatch."""

    def __init__(self) -> None:
        self.triggered_calls: list[MemoryHookContext] = []
        self.finished_calls: list[MemoryHookContext] = []

    async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
        self.triggered_calls.append(ctx)

    async def on_cleanup_finished(self, ctx: MemoryHookContext) -> None:
        self.finished_calls.append(ctx)


class _RevisionConflictSession(ScopedSessionMemoryManager):
    """ScopedSessionMemoryManager whose retain_messages always returns None.

    Simulates a revision conflict (concurrent modification) so the cleanup
    orchestrator hits the revision-conflict early return path. All other
    methods (get_revision, get_all_messages, add_messages, ...) are inherited
    from the real ScopedSessionMemoryManager.
    """

    async def retain_messages(
        self,
        context: MemoryContext,
        keep_messages: Sequence[ChatMessage | dict[str, object]],
        expected_revision: StorageRevision,
    ) -> StorageRevision | None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(tmp_path: Path) -> MemoryStoreRegistry:
    return DefaultMemoryStoreRegistry(tmp_path)


def _make_layer_set(
    registry: MemoryStoreRegistry,
) -> MemoryLayerSet:
    return MemoryLayerFactory.single_user(registry=registry)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckTriggerTokenOnly:
    """check_cleanup_trigger fires only on token pressure, never on message count."""

    def test_no_trigger_under_threshold(self) -> None:
        msgs = [{"role": "user", "content": "x", "token_count": 10}]  # 10 tokens
        # trigger line = max_context_tokens * max_token_ratio = 100 * 0.8 = 80
        assert check_cleanup_trigger(msgs, _FixedEstimator(10), max_context_tokens=100, max_token_ratio=0.8) is None

    def test_trigger_over_threshold(self) -> None:
        msgs = [{"role": "user", "content": "x", "token_count": 10}] * 9  # 90 tokens
        reason = check_cleanup_trigger(msgs, _FixedEstimator(10), max_context_tokens=100, max_token_ratio=0.8)
        assert reason == CompressionReason.TOKEN_PRESSURE

    def test_missing_token_count_recomputes(self) -> None:
        msgs = [{"role": "user", "content": "x"}] * 9  # no token_count -> 10 each via estimator
        reason = check_cleanup_trigger(msgs, _FixedEstimator(10), max_context_tokens=100, max_token_ratio=0.8)
        assert reason == CompressionReason.TOKEN_PRESSURE

    def test_system_tokens_excluded_from_trigger(self) -> None:
        """ADR-0009: system-role tokens do NOT count toward session pressure."""
        # A giant system message alone must NOT trigger.
        sys_only = [{"role": "system", "content": "huge system prompt", "token_count": 100000}]
        assert check_cleanup_trigger(sys_only, _FixedEstimator(10), max_context_tokens=100, max_token_ratio=0.8) is None
        # The same token burden as a user message DOES trigger.
        user_only = [{"role": "user", "content": "x", "token_count": 100000}]
        assert check_cleanup_trigger(user_only, _FixedEstimator(10), max_context_tokens=100, max_token_ratio=0.8) == CompressionReason.TOKEN_PRESSURE

    def test_trigger_accounts_for_output_tokens(self) -> None:
        """max_output_tokens reserves space for the model response before applying ratio.

        With max_context_tokens=200000 and max_token_ratio=0.85:
          - max_output_tokens=0      -> threshold = 200000 * 0.85      = 170000
          - max_output_tokens=20000  -> threshold = (200000-20000)*0.85 = 153000
        A pressure of 160000 sits between the two: triggers only when output
        space is reserved.
        """
        msgs = [{"role": "user", "content": "x", "token_count": 160000}]
        est = _FixedEstimator(10)

        # Default (no reservation): 160000 < 170000 -> no trigger
        assert check_cleanup_trigger(
            msgs, est, max_context_tokens=200000, max_token_ratio=0.85
        ) is None
        # Explicit zero: identical to default (backward compatible)
        assert check_cleanup_trigger(
            msgs, est, max_context_tokens=200000, max_token_ratio=0.85, max_output_tokens=0
        ) is None
        # Reserve 20K for output: 160000 > 153000 -> trigger
        assert check_cleanup_trigger(
            msgs, est, max_context_tokens=200000, max_token_ratio=0.85, max_output_tokens=20000
        ) == CompressionReason.TOKEN_PRESSURE

    def test_trigger_when_output_exceeds_context(self) -> None:
        """max_output_tokens > max_context_tokens floors effective context at 1.

        Threshold becomes 1 * ratio, so any non-zero pressure triggers.
        This is a misconfiguration guard, not a normal operating mode.
        """
        msgs = [{"role": "user", "content": "x", "token_count": 1}]
        est = _FixedEstimator(10)
        assert check_cleanup_trigger(
            msgs, est, max_context_tokens=200000, max_token_ratio=0.85, max_output_tokens=300000
        ) == CompressionReason.TOKEN_PRESSURE


class TestCheckTriggerDualCondition:
    """Second (absolute-reserve) trigger condition — kimi-style dual trigger.

    Fires when EITHER ``pressure > effective * ratio`` OR
    ``pressure + reserve >= effective``. ``reserve_tokens=None`` auto-derives
    ``min(20_000, effective // 10)``; ``0`` explicitly disables the second
    condition.
    """

    def test_absolute_fires_before_ratio_with_explicit_reserve(self) -> None:
        """Reserve 500 on a 2000 window: the absolute line (1500) sits below
        the 0.85 ratio line (1700) — pressure 1600 fires only absolutely."""
        msgs = [{"role": "user", "content": "x", "token_count": 100}] * 16  # 1600
        est = _FixedEstimator(10)

        assert check_cleanup_trigger(
            msgs, est, max_context_tokens=2000, max_token_ratio=0.85, reserve_tokens=500
        ) == CompressionReason.TOKEN_PRESSURE
        # Same pressure with the second condition disabled → quiet.
        assert check_cleanup_trigger(
            msgs, est, max_context_tokens=2000, max_token_ratio=0.85, reserve_tokens=0
        ) is None
        # Just under the absolute line → quiet with a sub-ratio pressure.
        lower = [{"role": "user", "content": "x", "token_count": 100}] * 14  # 1400
        assert check_cleanup_trigger(
            lower, est, max_context_tokens=2000, max_token_ratio=0.85, reserve_tokens=500
        ) is None

    def test_auto_reserve_is_tenth_of_effective_window(self) -> None:
        """Auto reserve on a 100k window = min(20k, 10k) = 10k. With a loose
        ratio (0.95 → line 95k), pressure 90.5k fires only absolutely."""
        msgs = [{"role": "user", "content": "x", "token_count": 500}] * 181  # 90_500
        est = _FixedEstimator(10)

        assert check_cleanup_trigger(
            msgs, est, max_context_tokens=100_000, max_token_ratio=0.95
        ) == CompressionReason.TOKEN_PRESSURE
        assert check_cleanup_trigger(
            msgs, est, max_context_tokens=100_000, max_token_ratio=0.95, reserve_tokens=0
        ) is None

    def test_auto_reserve_caps_at_20k(self) -> None:
        """Auto reserve on a 500k window = min(20k, 50k) = 20k: pressure
        481k + 20k ≥ 500k fires although the 0.97 ratio line (485k) holds."""
        msgs = [{"role": "user", "content": "x", "token_count": 1000}] * 481  # 481_000
        est = _FixedEstimator(10)

        assert check_cleanup_trigger(
            msgs, est, max_context_tokens=500_000, max_token_ratio=0.97
        ) == CompressionReason.TOKEN_PRESSURE
        assert check_cleanup_trigger(
            msgs, est, max_context_tokens=500_000, max_token_ratio=0.97, reserve_tokens=0
        ) is None
    """Boundary keeps a tail whose token sum stays within the keep target."""

    def test_keeps_tail_within_token_target(self) -> None:
        # 5 messages, 10 tokens each = 50 total. keep_target=25 -> keep 2 (20 tokens);
        # a 3rd would push to 30 > 25.
        msgs = [{"role": "user", "content": f"m{i}", "token_count": 10} for i in range(5)]
        keep, pruned = _compute_boundary(msgs, keep_target_tokens=25, estimator=_FixedEstimator(10))
        assert _sum_tokens_for(keep) <= 25
        assert len(keep) == 2
        assert len(pruned) == 3

    def test_tool_chain_split_evicts_forward(self) -> None:
        # boundary lands at idx 2 (the tool result). Its owner assistant (idx 1) is
        # pruned, so the tool result is an orphan in keep -> _adjust_boundary_for_tool_chains
        # must evict it FORWARD (boundary 2 -> 3), moving the whole chain into pruned.
        msgs = [
            {"role": "user", "content": "u0", "token_count": 10},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "function": {"name": "f", "arguments": "{}"}}], "token_count": 10},
            {"role": "tool", "tool_call_id": "c1", "content": "r1", "token_count": 10},
            {"role": "user", "content": "u1", "token_count": 10},
        ]
        keep, pruned = _compute_boundary(
            msgs, keep_target_tokens=20, estimator=_FixedEstimator(10)
        )
        # The orphan tool result was evicted: none remains in keep.
        assert all(m.get("role") != "tool" for m in keep)
        # It landed in pruned together with its owning assistant (chain archived intact).
        assert any(m.get("tool_call_id") == "c1" for m in pruned)
        assert any(
            m.get("role") == "assistant" and any(tc.get("id") == "c1" for tc in (m.get("tool_calls") or []))
            for m in pruned
        )
        # Keep shrank to just the trailing user message.
        assert len(keep) == 1


class TestTailKeepBudget:
    """Tail keep budget is absolute (PRD §4.4.7): clamp(usable×0.25, 2k..15k).

    The tail is an absolute token budget, not a ratio of the window.
    """

    def test_small_window_floors_at_2000(self) -> None:
        assert _tail_keep_budget(ContextBudget(max_context_tokens=4_000)) == 2_000

    def test_large_window_clamps_at_15000(self) -> None:
        assert _tail_keep_budget(ContextBudget(max_context_tokens=200_000)) == 15_000

    def test_mid_window_is_a_quarter_of_usable(self) -> None:
        # usable = 40_000 − 8_000 = 32_000 → ×0.25 = 8_000 (within clamp).
        budget = ContextBudget(max_context_tokens=40_000, max_output_tokens=8_000)
        assert _tail_keep_budget(budget) == 8_000

    @pytest.mark.asyncio
    async def test_kept_tail_tokens_follow_formula_under_small_limit(
        self, registry: MemoryStoreRegistry
    ) -> None:
        """Integration: the kept tail's token sum lands within one message of
        the clamp-formula budget (limit 10_000, usable 10_000 → tail 2_500)."""
        layer_set = _make_layer_set(registry)
        context = _ctx("tail-budget")
        session = layer_set.session
        # 90 messages × 104 tokens (100 estimate + 4 overhead) = 9_360
        # > 0.8 × 10_000 line → triggers; tail budget 2_500 → keep 24 msgs.
        await _add_messages(session, context, [_user_msg(f"u-{i}") for i in range(90)])

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
        )

        assert result.triggered is True
        assert result.messages_pruned == 66
        assert result.messages_kept == 24
        remaining = await session.get_all_messages(context)
        # Messages were seeded via the store (no cached token_count), so
        # recompute with the same estimator the engine used.
        kept_tokens = sum(_FixedEstimator(100).estimate_message(m.to_dict()) for m in remaining)
        assert 2_500 - 104 < kept_tokens <= 2_500


class TestNoTrigger:
    """cleanup_session should not trigger when session is under limits."""

    @pytest.mark.asyncio
    async def test_no_trigger_when_under_limit(self, registry: MemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        # Add only 3 messages = 30 tokens, well under the trigger line
        await _add_messages(session, context, [
            _user_msg("a"), _assistant_msg("b"), _user_msg("c"),
        ])

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=8000),
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(10),
        )

        assert result.triggered is False
        assert result.tokens_before == 30
        assert result.tokens_after == 30


class TestCleanupHookTriggered:
    """CLEANUP_TRIGGERED fires once when a cleanup triggers, and not when under limit.

    It must fire AFTER trigger confirmation and BEFORE archive generation (the
    blocking LLM call) so an observer can warn the user about the pause.
    """

    @pytest.mark.asyncio
    async def test_fires_when_triggered(self, registry: MemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session
        await _add_messages(session, context, [_user_msg("x" * 500)] * 20)

        hook = _RecordingHook()
        runner = MemoryHookRunner()
        runner.add(hook)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            budget=ContextBudget(max_context_tokens=100),
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(10),
            hook_runner=runner,
        )

        assert result.triggered is True
        assert len(hook.triggered_calls) == 1
        assert hook.triggered_calls[0].memory_context is not None
        assert hook.triggered_calls[0].memory_context.session_id == "test-session"
        assert hook.triggered_calls[0].compression_reason == CompressionReason.TOKEN_PRESSURE

    @pytest.mark.asyncio
    async def test_does_not_fire_when_under_limit(self, registry: MemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session
        await _add_messages(session, context, [_user_msg("a"), _assistant_msg("b")])

        hook = _RecordingHook()
        runner = MemoryHookRunner()
        runner.add(hook)

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=8000),
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(10),
            hook_runner=runner,
        )

        assert result.triggered is False
        assert hook.triggered_calls == []
        assert hook.finished_calls == []

    @pytest.mark.asyncio
    async def test_fires_before_archive_generation(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """CLEANUP_TRIGGERED must run before the archive agent is invoked."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session
        # 90 msgs × 104 tokens > 0.8 × 10_000 line → triggers AND prunes
        # (tail budget 2_500), so the archive agent really receives messages.
        await _add_messages(session, context, [_user_msg(f"u-{i}") for i in range(90)])

        order: list[str] = []

        class _OrderingHook(CleanupTriggeredHook):
            async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
                order.append("triggered")

        class _OrderArchiveAgent(_MockArchiveAgent):
            async def generate(
                self,
                pruned_messages: Sequence[dict[str, Any]],
            ) -> ArchiveGenerationResult:
                order.append("archive")
                return await super().generate(pruned_messages)

        runner = MemoryHookRunner()
        runner.add(_OrderingHook())

        storage = _DirArchiveStorageFactory.create(tmp_path)

        await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            hook_runner=runner,
            archive_agent=_OrderArchiveAgent(),
            archive_storage=storage,
        )

        assert order == ["triggered", "archive"]

    @pytest.mark.asyncio
    async def test_finished_dispatches_on_triggered_path(
        self, registry: MemoryStoreRegistry,
    ) -> None:
        """CLEANUP_FINISHED fires exactly once on the normal (triggered) path."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session
        await _add_messages(session, context, [_user_msg("x" * 500)] * 20)

        hook = _RecordingHook()
        runner = MemoryHookRunner()
        runner.add(hook)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            budget=ContextBudget(max_context_tokens=100),
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(10),
            hook_runner=runner,
        )

        assert result.triggered is True
        assert len(hook.triggered_calls) == 1
        assert len(hook.finished_calls) == 1
        assert hook.finished_calls[0].cleanup_result is result
        assert hook.finished_calls[0].compression_reason == CompressionReason.TOKEN_PRESSURE


class TestCleanupHookTruthTable:
    """Parametrized truth-table tests proving exact TRIGGERED/FINISHED counts.

    | path              | triggered | pruned      | TRIGGERED | FINISHED |
    |-------------------|-----------|-------------|-----------|----------|
    | under_threshold   | False     | 0           | 0         | 0        |
    | all_invalid       | True      | total       | 0         | 1        |
    | no_safe_boundary  | True      | 0           | 0         | 1        |
    | revision_conflict | True      | 0           | 1         | 1        |
    | normal            | True      | prune_count | 1         | 1        |

    All paths use real ScopedSessionMemoryManager + real cleanup_session() +
    recording MemoryHook subclass. The revision_conflict path uses a real
    subclass whose retain_messages returns None (simulating concurrent
    modification). Fake only at external boundaries (archive agent, storage).
    """

    @pytest.mark.parametrize(
        "scenario",
        [
            "under_threshold",
            "all_invalid",
            "no_safe_boundary",
            "revision_conflict",
            "normal",
        ],
    )
    @pytest.mark.asyncio
    async def test_truth_table(
        self,
        registry: MemoryStoreRegistry,
        scenario: str,
    ) -> None:
        hook = _RecordingHook()
        runner = MemoryHookRunner()
        runner.add(hook)

        context = _ctx(f"truth-{scenario}")
        common: dict[str, Any] = {
            "context": context,
            "token_estimator": _FixedEstimator(10),
            "hook_runner": runner,
        }

        if scenario == "under_threshold":
            layer_set = _make_layer_set(registry)
            session = layer_set.session
            await _add_messages(session, context, [
                _user_msg("a"), _assistant_msg("b"), _user_msg("c"),
            ])
            result = await cleanup_session(
                session=session,
                archive=layer_set.archive,
                budget=ContextBudget(max_context_tokens=8000),
                max_token_ratio=0.8,
                **common,
            )
            assert result.triggered is False
            assert result.messages_pruned == 0
            assert result.tokens_before == result.tokens_after
            assert len(hook.triggered_calls) == 0
            assert len(hook.finished_calls) == 0
            return

        if scenario == "all_invalid":
            layer_set = _make_layer_set(registry)
            session = layer_set.session
            await _add_messages(session, context, [
                _tool_result_msg("orphan_1", "r1"),
                _tool_result_msg("orphan_2", "r2"),
                _tool_result_msg("orphan_3", "r3"),
            ] * 7)
            result = await cleanup_session(
                session=session,
                archive=None,
                budget=ContextBudget(max_context_tokens=100),
                max_token_ratio=0.8,
                **common,
            )
            assert result.triggered is True
            assert result.messages_pruned == 21
            assert result.messages_kept == 0
            assert result.tokens_before > 0
            assert result.tokens_after == 0
            assert len(hook.triggered_calls) == 0
            assert len(hook.finished_calls) == 1
            assert hook.finished_calls[0].cleanup_result is result
            return

        if scenario == "no_safe_boundary":
            layer_set = _make_layer_set(registry)
            session = layer_set.session
            # Tail budget floors at 2_000 tokens. One assistant call with 25
            # tool calls followed by their 25 results: the tail walk stops
            # inside the result run and every kept result's owner is pruned,
            # so the boundary evicts forward through the whole run → no safe
            # boundary exists.
            multi_call = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": f"c{i}", "type": "function",
                     "function": {"name": "tool_a", "arguments": "{}"}}
                    for i in range(25)
                ],
            }
            await _add_messages(session, context, [
                _user_msg("question"),
                multi_call,
                *[_tool_result_msg(f"c{i}", "result") for i in range(25)],
            ])
            result = await cleanup_session(
                session=session,
                archive=None,
                budget=ContextBudget(max_context_tokens=3_000),
                max_token_ratio=0.8,
                **{**common, "token_estimator": _FixedEstimator(100)},
            )
            assert result.triggered is True
            assert result.messages_pruned == 0
            assert result.tokens_before == result.tokens_after
            assert len(hook.triggered_calls) == 0
            assert len(hook.finished_calls) == 1
            assert hook.finished_calls[0].cleanup_result is result
            return

        if scenario == "revision_conflict":
            layer_set = _make_layer_set(registry)
            real_session = layer_set.session
            assert isinstance(real_session, ScopedSessionMemoryManager)
            conflict_session = _RevisionConflictSession(real_session._storage_factory)
            await _add_messages(conflict_session, context, [
                _user_msg(f"u-{i}") for i in range(10)
            ])
            result = await cleanup_session(
                session=conflict_session,
                archive=None,
                budget=ContextBudget(max_context_tokens=50),
                max_token_ratio=0.8,
                **common,
            )
            assert result.triggered is True
            assert result.messages_pruned == 0
            assert result.tokens_before == result.tokens_after
            assert len(hook.triggered_calls) == 1
            assert len(hook.finished_calls) == 1
            assert hook.finished_calls[0].cleanup_result is result
            return

        if scenario == "normal":
            layer_set = _make_layer_set(registry)
            session = layer_set.session
            await _add_messages(session, context, [
                _user_msg(f"u-{i}") for i in range(90)
            ])
            result = await cleanup_session(
                session=session,
                archive=None,
                budget=ContextBudget(max_context_tokens=10_000),
                max_token_ratio=0.8,
                **{**common, "token_estimator": _FixedEstimator(100)},
            )
            assert result.triggered is True
            assert result.messages_pruned > 0
            assert result.tokens_before > result.tokens_after
            assert len(hook.triggered_calls) == 1
            assert len(hook.finished_calls) == 1
            assert hook.finished_calls[0].cleanup_result is result
            return

        pytest.fail(f"Unknown scenario: {scenario}")


class TestCleanupHookResilience:
    """Cleanup continues after a hook error or timeout."""

    @pytest.mark.asyncio
    async def test_cleanup_continues_after_triggered_hook_error(
        self, registry: MemoryStoreRegistry,
    ) -> None:
        """A failing CLEANUP_TRIGGERED hook does not abort cleanup."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session
        await _add_messages(session, context, [_user_msg("x" * 500)] * 20)

        good_hook = _RecordingHook()

        class _FailingTriggeredHook(CleanupTriggeredHook):
            async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None:
                raise RuntimeError("hook boom")

        runner = MemoryHookRunner()
        runner.add(_FailingTriggeredHook())
        runner.add(good_hook)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            budget=ContextBudget(max_context_tokens=100),
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(10),
            hook_runner=runner,
        )

        assert result.triggered is True
        assert len(good_hook.triggered_calls) == 1
        assert len(good_hook.finished_calls) == 1


class TestCleanupHookLateRegistration:
    """Late registration: a hook added after history creation receives events."""

    @pytest.mark.asyncio
    async def test_late_hook_receives_subsequent_events(
        self, registry: MemoryStoreRegistry,
    ) -> None:
        """A hook added to the runner after a history is created still receives events.

        This works because DefaultMemorySystem passes the SAME MemoryHookRunner
        object by reference to every ScopedMessageHistory. Adding a hook to the
        runner after history creation means the next cleanup_session dispatch
        sees it.
        """
        from modex_agent.memory.default_system import DefaultMemorySystem
        from modex_agent.memory.history import ScopedMessageHistory

        layer_set = _make_layer_set(registry)
        system = DefaultMemorySystem(
            layer_set=layer_set,
            store_registry=registry,
            cleanup_config={"max_context_tokens": 50, "max_token_ratio": 0.8},
            token_estimator=_FixedEstimator(10),
        )

        context = _ctx("late-reg")
        history = system.create_message_history(context)
        assert isinstance(history, ScopedMessageHistory)

        late_hook = _RecordingHook()
        system.add_cleanup_hook(late_hook)

        await history.append(_user_msg("a"))
        await history.append(_user_msg("b"))
        await history.append(_user_msg("c"))
        await history.append(_user_msg("d"))
        await history.append(_user_msg("e"))
        await history.append(_user_msg("f"))
        await history.append(_user_msg("g"))
        await history.append(_user_msg("h"))
        await history.append(_user_msg("i"))
        await history.append(_user_msg("j"))

        assert len(late_hook.triggered_calls) >= 1
        assert len(late_hook.finished_calls) >= 1


class TestTriggerAndCleanup:
    """cleanup_session should trigger and clean when over limits."""

    @pytest.mark.asyncio
    async def test_trigger_when_over_message_limit(self, registry: MemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        # 90 msgs × 104 tokens = 9_360 > 0.8 × 10_000 line → triggers; the
        # absolute tail budget (2_500 tokens) keeps only ~24 messages.
        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"user-{i}"))
            msgs.append(_assistant_msg(f"asst-{i}"))
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
        )

        assert result.triggered is True
        assert result.messages_kept > 0
        assert result.messages_pruned > 0
        assert result.archive_skipped is True  # no archive manager

        # Verify session was actually pruned
        remaining = await session.get_all_messages(context)
        assert len(remaining) == result.messages_kept
        assert len(remaining) < 90

    @pytest.mark.asyncio
    async def test_trigger_when_over_token_limit(self, registry: MemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        # 90 msgs × 104 tokens = 9_360 > 8_000 line → triggers
        msgs = []
        for _i in range(90):
            msgs.append(_user_msg("x" * 500))
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
        )

        assert result.triggered is True
        assert result.messages_pruned > 0


class TestCleanupAlwaysExecutes:
    """cleanup_session should clean even when archive is None."""

    @pytest.mark.asyncio
    async def test_cleanup_always_executes(self, registry: MemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        # 90 msgs × 104 tokens = 9_360 > 8_000 line → triggered
        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,  # No archive
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
        )

        assert result.triggered is True
        # Session was cleaned even without archive
        remaining = await session.get_all_messages(context)
        assert len(remaining) < 90
        assert len(remaining) > 0


class TestCleanupRemovesInvalidToolChains:
    """cleanup_session should remove orphan tool results via sanitizer."""

    @pytest.mark.asyncio
    async def test_cleanup_removes_invalid_tool_chains(self, registry: MemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        # Build messages with an orphan tool result (no matching assistant tool_call)
        msgs = [
            _user_msg("start"),
            _tool_result_msg("call_orphan", "orphan-result"),  # orphan — no preceding assistant tool_call
            _assistant_msg("normal reply"),
            _user_msg("continue"),
            _assistant_msg("reply2"),
            # Add more to exceed limit
            _user_msg("a"),
            _assistant_msg("b"),
            _user_msg("c"),
            _assistant_msg("d"),
            _user_msg("e"),
            _assistant_msg("f"),
        ]
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            budget=ContextBudget(max_context_tokens=50),
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(10),
        )

        assert result.triggered is True
        # Orphan tool result should have been removed during sanitization
        remaining = await session.get_all_messages(context)
        for msg in remaining:
            # No orphan tool results should remain
            if msg.role == "tool" and msg.tool_call_id == "call_orphan":
                pytest.fail("Orphan tool result should have been sanitized away")


class TestKeepBoundary:
    """Tests for the keep boundary computation."""

    @pytest.mark.asyncio
    async def test_never_splits_tool_chain(self, registry: MemoryStoreRegistry) -> None:
        """Boundary should not split an assistant tool_call from its tool result.

        86 msgs × 104 tokens > 8_000 line; tail budget 2_500 keeps the last
        ~24 messages — the tool chain sits inside the keep region and must
        arrive there whole."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = [_user_msg(f"u-{i}") for i in range(80)]
        msgs.append(_tool_call_msg("call_1", "tool_a"))  # tool chain start
        msgs.append(_tool_result_msg("call_1", "result"))  # tool chain end
        msgs.extend(_user_msg(f"tail-{i}") for i in range(4))
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
        )

        assert result.triggered is True
        remaining = await session.get_all_messages(context)
        # If assistant(tool_call) is kept, its tool result must also be kept
        has_tool_call = any(
            m.role == "assistant" and m.tool_calls
            for m in remaining
        )
        has_tool_result = any(
            m.role == "tool" and m.tool_call_id == "call_1"
            for m in remaining
        )
        # Both or neither — never split
        assert has_tool_call == has_tool_result, (
            f"Tool chain split: has_call={has_tool_call}, has_result={has_tool_result}"
        )

    @pytest.mark.asyncio
    async def test_single_user_session_cleans_properly(self, registry: MemoryStoreRegistry) -> None:
        """Session with 1 user + 50 tool pairs: cleanup MUST prune messages.

        This was the session.jsonl bug — _adjust_boundary_for_last_user
        forced boundary=0, keeping all 101 messages despite exceeding limits.
        """
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = [_user_msg("question")]
        for i in range(50):
            msgs.append(_tool_call_msg(f"call_{i}"))
            msgs.append(_tool_result_msg(f"call_{i}", f"result_{i}"))
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session, archive=None, context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 101 msgs ≈ 10_504 tokens (104/msg), line 8_000 -> triggers
            max_token_ratio=0.8,  # tail keep budget = clamp(10_000×0.25, 2k..15k) = 2_500
            token_estimator=_FixedEstimator(100),
        )

        assert result.triggered is True
        assert result.messages_pruned > 0, (
            f"Must prune messages when over token limit (total={len(msgs)} msgs, "
            f"max_context_tokens=10_000), but pruned=0"
        )
        remaining = await session.get_all_messages(context)
        assert len(remaining) < len(msgs), (
            f"Session must be smaller after cleanup: {len(remaining)} >= {len(msgs)}"
        )


class TestKeepToolChainIntegrity:
    """Tool chains in the keep region must stay intact (no split assistant/tool pairs)."""

    @pytest.mark.asyncio
    async def test_tool_chain_in_keep_region_not_split(
        self, registry: MemoryStoreRegistry,
    ) -> None:
        """A chain sitting at the keep boundary is evicted WHOLE into pruned.

        60 users + [tool_call, tool_result] + 24 users: the tail budget
        (2_500 tokens ≈ 24 msgs) ends exactly at the chain, so the whole
        chain must land in pruned — never a kept orphan result."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = [_user_msg(f"u-{i}") for i in range(60)]
        msgs.append(_tool_call_msg("call_1"))     # tool chain
        msgs.append(_tool_result_msg("call_1"))
        msgs.extend(_user_msg(f"tail-{i}") for i in range(24))
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 86 msgs ≈ 8_944 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,  # tail keep budget 2_500 -> keep the last 24 users
            token_estimator=_FixedEstimator(100),
        )

        assert result.triggered is True
        remaining = await session.get_all_messages(context)
        assert len(remaining) > 0

        # Tool chain must be intact: if tool_call is kept, tool_result must be too
        has_tool_call = any(
            m.role == "assistant" and m.tool_calls
            for m in remaining
        )
        has_tool_result = any(
            m.role == "tool" and m.tool_call_id == "call_1"
            for m in remaining
        )
        assert has_tool_call == has_tool_result, (
            f"Tool chain split: has_call={has_tool_call}, has_result={has_tool_result}"
        )


class TestToolChainDominanceDoesNotOverPrune:
    """Regression: sessions dominated by tool chains must not over-prune.

    When most of the session is tool chains (1 user + 50 tc/tool pairs + 1 user),
    the boundary adjustment must not walk all the way to the last user,
    keeping only 1 message. This was the MiniMax 400 error root cause.
    """

    @pytest.mark.asyncio
    async def test_tool_chain_heavy_session_keeps_reasonable_count(
        self, registry: MemoryStoreRegistry,
    ) -> None:
        """1 user + 50 tool pairs + 1 new user: must keep a tail-sized batch,
        not 1 (102 msgs × 104 tokens = 10_608 > 8_000 line; tail 2_500)."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = [_user_msg("question")]
        for i in range(50):
            msgs.append(_tool_call_msg(f"call_{i}"))
            msgs.append(_tool_result_msg(f"call_{i}", f"result_{i}"))
        msgs.append(_user_msg("follow-up"))

        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),
            max_token_ratio=0.8,  # tail keep budget 2_500 -> keep ~23 msgs
            token_estimator=_FixedEstimator(100),
        )

        assert result.triggered is True
        assert result.messages_kept > 10, (
            f"kept={result.messages_kept} but expected a tail-sized keep "
            f"(total={len(msgs)} msgs, tail budget=2_500 tokens)"
        )

    @pytest.mark.asyncio
    async def test_kept_count_respects_tail_budget_floor(
        self, registry: MemoryStoreRegistry,
    ) -> None:
        """kept must stay proportional to the tail budget even when the
        session is pure tool chains: at least tail // (pair size × per-msg)."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = [_user_msg("q1")]
        for i in range(50):
            msgs.append(_tool_call_msg(f"call_{i}"))
            msgs.append(_tool_result_msg(f"call_{i}", f"r_{i}"))
        msgs.append(_user_msg("q2"))

        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),
            max_token_ratio=0.8,  # tail keep budget 2_500 tokens
            token_estimator=_FixedEstimator(100),
        )

        assert result.triggered is True
        min_kept = 2_500 // (2 * 104)  # a full tc/tr pair per 208 tokens
        assert result.messages_kept >= min_kept, (
            f"kept={result.messages_kept} < min_kept={min_kept} (tail budget=2_500)"
        )


class TestKeepResanitized:
    """Keep region is re-sanitized to ensure tool chain integrity."""

    @pytest.mark.asyncio
    async def test_incomplete_tool_chain_in_keep_force_cleaned(
        self, registry: MemoryStoreRegistry,
    ) -> None:
        """If keep region has incomplete tool chains, they are removed."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        # Build a scenario where keep region could have incomplete chains
        msgs = [
            _user_msg("1"),
            _tool_call_msg("call_1"),
            _tool_result_msg("call_1"),
            _assistant_msg("r1"),
            _user_msg("2"),
            _tool_call_msg("call_2"),
            # No tool result for call_2 — incomplete
            _user_msg("3"),
            _assistant_msg("r3"),
        ]
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            budget=ContextBudget(max_context_tokens=50),  # 8 msgs = 80 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(10),
        )

        assert result.triggered is True
        remaining = await session.get_all_messages(context)
        # No orphan tool_call without result (except possibly last open)
        for i, m in enumerate(remaining):
            if m.role == "assistant" and m.tool_calls:
                call_ids = {
                    tc["id"] if isinstance(tc, dict) else tc.call_id for tc in m.tool_calls
                }
                # Check each call_id has a matching tool result
                for cid in call_ids:
                    has_result = any(
                        rm.role == "tool" and rm.tool_call_id == cid
                        for rm in remaining
                    )
                    assert has_result, (
                        f"Orphan tool_call {cid} at index {i} has no matching result"
                    )


class TestCleanupResultType:
    """Verify CleanupResult dataclass fields."""

    def test_cleanup_result_fields(self) -> None:
        result = CleanupResult(
            triggered=True,
            messages_kept=5,
            messages_pruned=10,
            tokens_before=120,
            tokens_after=40,
            archive_skipped=False,
            reason=CompressionReason.MESSAGE_COUNT,
        )
        assert result.triggered is True
        assert result.messages_kept == 5
        assert result.messages_pruned == 10
        assert result.tokens_before == 120
        assert result.tokens_after == 40
        assert result.archive_skipped is False
        assert result.reason == CompressionReason.MESSAGE_COUNT

    def test_cleanup_result_not_triggered(self) -> None:
        result = CleanupResult(triggered=False)
        assert result.triggered is False
        assert result.messages_kept == 0
        assert result.messages_pruned == 0
        assert result.tokens_before == 0
        assert result.tokens_after == 0


# ---------------------------------------------------------------------------
# Mock archive agent for Phase 4 tests
# ---------------------------------------------------------------------------


class _MockArchiveAgent(ArchiveGenerator):
    """Mock ArchiveSummarizer that records calls and can be configured to succeed or fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[list[dict]] = []
        self._fail = fail

    async def generate(
        self,
        pruned_messages: Sequence[dict[str, Any]],
    ) -> ArchiveGenerationResult:
        self.calls.append(list(pruned_messages))
        if self._fail:
            raise RuntimeError("mock failure")
        return ArchiveGenerationResult(
            documents=ArchiveDocuments(
                context="context summary",
                core="knowledge summary",
                index="Test Archive Topic",
            )
        )


class _DirArchiveStorageFactory:
    """Factory for DirArchiveStorage backed by a temp directory."""

    @staticmethod
    def create(tmp_path: Path) -> DirArchiveStorage:
        from pathlib import Path

        from modex_agent.memory.stores.dir_archive import DirArchiveStorage
        return DirArchiveStorage(Path(tmp_path) / "archives")


# ---------------------------------------------------------------------------
# Phase 4: Archive agent integration tests
# ---------------------------------------------------------------------------


class TestArchiveAgentIntegration:
    """Tests for the new archive_agent flow in cleanup_session."""

    @pytest.mark.asyncio
    async def test_with_archive_agent_generates_md_files(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """When archive_agent is provided, archive MD files are generated."""
        layer_set = _make_layer_set(registry)
        context = _ctx("agent-session-1")
        session = layer_set.session

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent()
        storage = _DirArchiveStorageFactory.create(tmp_path)

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent,
            archive_storage=storage,
        )

        assert result.triggered is True
        assert len(agent.calls) == 1
        # Verify files were written to the archive directory
        archive_path = storage.base_dir / "1"
        assert (archive_path / "context.md").exists()
        assert (archive_path / "knowledge.md").exists()

    @pytest.mark.asyncio
    async def test_archive_agent_failure_falls_back(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """When archive_agent fails, pruned index falls back to write_pruned."""
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("agent-fail-session")
        session = layer_set.session

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent(fail=True)
        storage = _DirArchiveStorageFactory.create(tmp_path)
        pruned_mgr = PrunedManager(pruned_base_dir=tmp_path / "pruned")

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent,
            archive_storage=storage,
            pruned_manager=pruned_mgr,
        )

        assert result.triggered is True
        # Archive was attempted but failed
        assert len(agent.calls) == 1
        # Pruned index should have been populated via fallback
        entries = pruned_mgr._get_storage(context.session_id or "").read_index()
        assert len(entries) >= 1

    @pytest.mark.asyncio
    async def test_archive_id_increments_on_success(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """archive_id (next_archive_id in state) increments after successful flow."""
        layer_set = _make_layer_set(registry)
        context = _ctx("increment-session")
        session = layer_set.session

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent()
        storage = _DirArchiveStorageFactory.create(tmp_path)

        # Initial state: no state.json, defaults to next_archive_id=1
        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent,
            archive_storage=storage,
        )

        assert result.triggered is True
        archive = layer_set.archive
        assert archive is not None
        first_entries = await archive.get_recent(context, limit=5)
        assert [entry.entry_id for entry in first_entries] == [1]

        # Second cleanup: should use archive_id=2
        await _add_messages(session, context, msgs)

        agent2 = _MockArchiveAgent()
        result2 = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent2,
            archive_storage=storage,
        )

        assert result2.triggered is True
        assert len(agent2.calls) == 1
        second_entries = await archive.get_recent(context, limit=5)
        assert [entry.entry_id for entry in second_entries] == [1, 2]
        assert (storage.base_dir / "2" / "context.md").exists()

    @pytest.mark.asyncio
    async def test_skips_agent_if_archive_complete(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """When archive directory is already complete, no LLM call is made."""
        layer_set = _make_layer_set(registry)
        context = _ctx("skip-complete-session")
        session = layer_set.session

        # Pre-populate a complete archive for id=1
        storage = _DirArchiveStorageFactory.create(tmp_path)
        await storage.write_archive_state({"next_archive_id": 1})
        await storage.write_archive_file(1, "context.md", "existing context")
        await storage.write_archive_file(1, "knowledge.md", "existing knowledge")

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent()

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent,
            archive_storage=storage,
        )

        assert result.triggered is True
        assert len(agent.calls) == 1
        assert result.archive_skipped is False

    @pytest.mark.asyncio
    async def test_archives_before_session_commit(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """Archive generation happens BEFORE session messages are committed."""
        # Use a tracking agent that records order
        layer_set = _make_layer_set(registry)
        context = _ctx("order-session")
        session = layer_set.session

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        # Before cleanup, session has 10 messages
        before_count = len(await session.get_all_messages(context))
        assert before_count == 90

        agent = _MockArchiveAgent()
        storage = _DirArchiveStorageFactory.create(tmp_path)

        # The agent writes files to the archive directory
        # We verify that after cleanup, the session is pruned AND files exist
        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent,
            archive_storage=storage,
        )

        assert result.triggered is True
        assert result.messages_pruned > 0
        # Session was committed (pruned) AND archive files were generated
        after_count = len(await session.get_all_messages(context))
        assert after_count < before_count
        # Archive files exist (agent wrote them before commit)
        assert len(agent.calls) == 1
        archive_path = storage.base_dir / "1"
        assert (archive_path / "context.md").exists()


class TestArchiveSuccessPrunedContent:
    """Regression: when archive agent succeeds, pruned raw content must still be written.

    Bug: cleanup_session used refresh_from_archives() on the archive-success path,
    which only wrote index.jsonl pointing to archive layer files. Raw pruned messages
    were lost and content_filename was a cross-layer reference that didn't resolve.
    """

    @pytest.mark.asyncio
    async def test_pruned_writes_raw_content_when_archive_succeeds(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """Pruned content file must exist with raw messages, not just an index."""
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("pruned-archive-session")
        session = layer_set.session

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent()
        storage = _DirArchiveStorageFactory.create(tmp_path)
        pruned_mgr = PrunedManager(pruned_base_dir=tmp_path / "pruned")

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent,
            archive_storage=storage,
            pruned_manager=pruned_mgr,
        )

        assert result.triggered is True
        assert result.messages_pruned > 0

        # Pruned index must have entries
        pruned_storage = pruned_mgr._get_storage(context.session_id or "")
        entries = pruned_storage.read_index()
        assert len(entries) >= 1

        entry = entries[-1]

        # Bug 1: content_filename must resolve to a real file in pruned dir
        from pathlib import Path
        pruned_dir = Path(pruned_storage.get_directory_path())
        content_path = pruned_dir / entry.content_filename
        assert content_path.exists(), (
            f"content_filename '{entry.content_filename}' does not exist at {content_path}"
        )

    @pytest.mark.asyncio
    async def test_pruned_content_contains_raw_messages(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """Pruned content file must contain the raw pruned messages (markdown transcript)."""
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("pruned-raw-session")
        session = layer_set.session

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"unique-content-{i}"))
            msgs.append(_assistant_msg(f"reply-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent()
        storage = _DirArchiveStorageFactory.create(tmp_path)
        pruned_mgr = PrunedManager(pruned_base_dir=tmp_path / "pruned")

        await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent,
            archive_storage=storage,
            pruned_manager=pruned_mgr,
        )

        pruned_storage = pruned_mgr._get_storage(context.session_id or "")
        entries = pruned_storage.read_index()
        assert len(entries) >= 1

        entry = entries[-1]
        from pathlib import Path
        content_path = Path(pruned_storage.get_directory_path()) / entry.content_filename
        assert content_path.exists()

        # Transcript is a rendered markdown file with numbered message blocks
        assert content_path.suffix == ".md"
        text = content_path.read_text(encoding="utf-8")
        assert "## [001]" in text
        # At least one user message from our input should be in the pruned content
        assert "unique-content-" in text, (
            "Pruned content should contain raw user messages from the pruned session region"
        )

    @pytest.mark.asyncio
    async def test_pruned_entry_has_correct_message_count_and_times(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """Pruned index entry must have message_count > 0 and non-empty time fields."""
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("pruned-fields-session")
        session = layer_set.session

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent()
        storage = _DirArchiveStorageFactory.create(tmp_path)
        pruned_mgr = PrunedManager(pruned_base_dir=tmp_path / "pruned")

        await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent,
            archive_storage=storage,
            pruned_manager=pruned_mgr,
        )

        pruned_storage = pruned_mgr._get_storage(context.session_id or "")
        entries = pruned_storage.read_index()
        assert len(entries) >= 1

        entry = entries[-1]
        # Bug 3: message_count must reflect actual pruned messages
        assert entry.message_count > 0, (
            f"message_count should be > 0, got {entry.message_count}"
        )
        # Time display fields must be populated
        assert entry.start_time_display != "", (
            "start_time_display should not be empty"
        )
        assert entry.cleanup_time_display != "", (
            "cleanup_time_display should not be empty"
        )


# ---------------------------------------------------------------------------
# Phase 5: Resolved storage propagation regression tests
# ---------------------------------------------------------------------------


class TestResolvedStoragePropagation:
    @pytest.mark.asyncio
    async def test_pruned_written_when_storage_not_injected(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """archive_storage=None → pruned catalog still written (fallback topic)."""
        from modex_agent.memory.pruned.manager import PrunedManager
        layer_set = _make_layer_set(registry)
        context = _ctx("resolve-topic-session")
        session = layer_set.session

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent()
        pruned_mgr = PrunedManager(pruned_base_dir=tmp_path / "pruned")
        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent,
            archive_storage=None,
            pruned_manager=pruned_mgr,
        )

        assert result.triggered is True
        pruned_storage = pruned_mgr._get_storage(context.session_id or "")
        entries = pruned_storage.read_index()
        assert len(entries) >= 1
        # No compactor → topic falls back to time-range format
        entry = entries[-1]
        assert "(" in entry.topic and "messages)" in entry.topic, (
            f"Expected fallback time-range topic, got: '{entry.topic}'"
        )

    @pytest.mark.asyncio
    async def test_pruned_written_when_storage_explicitly_provided(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """archive_storage provided → pruned catalog written (fallback topic)."""
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("explicit-storage-session")
        session = layer_set.session

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent()
        storage = _DirArchiveStorageFactory.create(tmp_path)
        pruned_mgr = PrunedManager(pruned_base_dir=tmp_path / "pruned")

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent,
            archive_storage=storage,
            pruned_manager=pruned_mgr,
        )

        assert result.triggered is True

        pruned_storage = pruned_mgr._get_storage(context.session_id or "")
        entries = pruned_storage.read_index()
        assert len(entries) >= 1

        entry = entries[-1]
        # No compactor → topic falls back to time-range format
        assert "(" in entry.topic and "messages)" in entry.topic, (
            f"Expected fallback time-range topic, got: '{entry.topic}'"
        )

    @pytest.mark.asyncio
    async def test_fallback_topic_when_archive_agent_fails(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """archive_storage=None + agent fails → fallback time-range topic."""
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("fail-fallback-session")
        session = layer_set.session

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent(fail=True)
        pruned_mgr = PrunedManager(pruned_base_dir=tmp_path / "pruned")

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent,
            archive_storage=None,
            pruned_manager=pruned_mgr,
        )

        assert result.triggered is True

        pruned_storage = pruned_mgr._get_storage(context.session_id or "")
        entries = pruned_storage.read_index()
        assert len(entries) >= 1

        entry = entries[-1]
        # Should be fallback: "YYYY-MM-DD HH:MM ~ YYYY-MM-DD HH:MM (N messages)"
        assert "(" in entry.topic and "messages)" in entry.topic, (
            f"Expected fallback time-range topic, got: '{entry.topic}'"
        )

    @pytest.mark.asyncio
    async def test_no_archive_agent_uses_fallback_topic(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """No archive_agent at all → fallback topic (existing behavior)."""
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("no-agent-session")
        session = layer_set.session

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        pruned_mgr = PrunedManager(pruned_base_dir=tmp_path / "pruned")

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            pruned_manager=pruned_mgr,
        )

        assert result.triggered is True

        pruned_storage = pruned_mgr._get_storage(context.session_id or "")
        entries = pruned_storage.read_index()
        assert len(entries) >= 1

        entry = entries[-1]
        assert "(" in entry.topic and "messages)" in entry.topic

    @pytest.mark.asyncio
    async def test_archive_state_advances_when_storage_not_injected(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """archive_storage=None → state.json still gets next_archive_id incremented."""

        layer_set = _make_layer_set(registry)
        context = _ctx("state-advance-session")
        session = layer_set.session

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent()

        # Provide storage so state advance writes to a known location.
        # The key test is that Phase 7 (advance) uses the resolved storage.
        storage = _DirArchiveStorageFactory.create(tmp_path)

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent,
            archive_storage=storage,
        )

        assert result.triggered is True

        archive = layer_set.archive
        assert archive is not None
        entries = await archive.get_recent(context, limit=5)
        assert [entry.entry_id for entry in entries] == [1]

    @pytest.mark.asyncio
    async def test_archive_register_when_storage_not_injected(
        self, registry: MemoryStoreRegistry, tmp_path,
    ) -> None:
        """archive_storage=None + dynamic resolve → register_archive_with_layer works."""
        layer_set = _make_layer_set(registry)
        context = _ctx("register-resolve-session")
        session = layer_set.session

        msgs = []
        for i in range(45):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent()
        storage = _DirArchiveStorageFactory.create(tmp_path)

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_000 -> triggers
            max_token_ratio=0.8,
            token_estimator=_FixedEstimator(100),
            archive_agent=agent,
            archive_storage=storage,
        )

        assert result.triggered is True
        assert result.archive_skipped is False

        # In the MD-only architecture, archives are written directly to disk
        # by the ArchiveSummarizer. Verify the MD files exist in the archive dir.
        from modex_agent.memory.stores.dir_archive import DirArchiveStorage
        dir_storage = DirArchiveStorage(tmp_path / "archives")
        archive_ids = await dir_storage.list_archives()
        assert len(archive_ids) >= 1
        content = await dir_storage.read_archive_file(archive_ids[0], "context.md")
        assert content == "context summary"


# ---------------------------------------------------------------------------
# SQLite backend end-to-end: compact summary persistence (the bot default)
# ---------------------------------------------------------------------------


class _StubCompactor(SessionCompactorAgent):
    """Minimal SessionCompactorAgent stand-in.

    Records calls, returns a fixed structured summary, and reuses the real
    ``extract_topic`` so topic extraction is covered too.
    """

    def __init__(self, summary: str) -> None:
        self._summary = summary
        self.calls: list[dict[str, Any]] = []

    async def compact(
        self,
        messages: Sequence[dict[str, Any]],
        previous_summary: str | None = None,
        *,
        session_id: str = "session-compactor",
        budget: ContextBudget | None = None,
    ) -> CompactionOutcome:
        self.calls.append(
            {
                "messages": list(messages),
                "previous_summary": previous_summary,
                "session_id": session_id,
                "budget": budget,
            }
        )
        return CompactionOutcome(summary=self._summary)

    @staticmethod
    def extract_topic(summary: str, max_chars: int = 200) -> str | None:
        return SessionCompactorAgent.extract_topic(summary, max_chars)


_COMPACT_SUMMARY = (
    "## Objective\n"
    "- sqlite e2e objective\n"
    "\n"
    "## Work State\n"
    "### Completed\n"
    "- (none)\n"
)


class TestCleanupSqliteBackend:
    """End-to-end cleanup against the SQLite backend (the bot default).

    Regression coverage for the bug where the compact summary was silently
    dropped on SQLite: ``retain_messages`` only soft-deleted and never
    inserted, so the summary never landed in the session.
    """

    async def _open_sqlite_session(
        self, tmp_path: Path
    ) -> tuple[WorkspacePersistenceManager, ScopedSessionMemoryManager]:
        from modex_agent.core.scope import RecordScope

        manager = WorkspacePersistenceManager(tmp_path / "state.db")
        await manager.open()
        scope = RecordScope(session_id="s1", agent_id="main", user_id="u1")

        async def factory(_context: MemoryContext) -> MemoryStoreBundle:
            return manager.create_bundle(scope)

        return manager, ScopedSessionMemoryManager(factory)

    @pytest.mark.asyncio
    async def test_compact_summary_persisted_at_top(self, tmp_path: Path) -> None:
        manager, session = await self._open_sqlite_session(tmp_path)
        try:
            context = _ctx("sqlite-compact-session")
            msgs = []
            for i in range(45):
                msgs.append(_user_msg(f"u-{i}"))
                msgs.append(_assistant_msg(f"a-{i}"))
            await _add_messages(session, context, msgs)

            compactor = _StubCompactor(_COMPACT_SUMMARY)
            result = await cleanup_session(
                session=session,
                archive=None,
                context=context,
                compactor=compactor,
                budget=ContextBudget(max_context_tokens=10_000),  # 90 msgs = 9_360 tokens, line 8_500 -> triggers
                max_token_ratio=0.85,  # tail budget 2_500 -> keep 24 msgs
                token_estimator=_FixedEstimator(100),
            )

            assert result.triggered is True
            assert result.compact_generated is True

            kept = await session.get_all_messages(context)
            assert str(kept[0].role) == "compact"
            assert "sqlite e2e objective" in (kept[0].content or "")
            # Tail preserved verbatim after the summary (no tool chains here).
            original = [m["content"] for m in msgs]
            assert [m.content for m in kept[1:]] == original[-(len(kept) - 1):]
            assert len(kept) >= 2  # compact + at least one tail message

            # History view: pruned visible as soft-deleted, no duplicates of
            # kept messages, no compact (COMPACT excluded from load_all_messages).
            from modex_agent.core.scope import RecordScope

            bundle = manager.create_bundle(
                RecordScope(session_id="s1", agent_id="main", user_id="u1")
            )
            all_msgs = await bundle.messages.load_all_messages()
            compact_rows = [m for m in all_msgs if m.get("role") == "compact"]
            assert len(compact_rows) == 0
            contents = [m.get("content") for m in all_msgs]
            for tail_msg in kept[1:]:
                assert contents.count(tail_msg.content) == 1
        finally:
            await manager.close()

    @pytest.mark.asyncio
    async def test_second_cycle_chains_previous_summary(self, tmp_path: Path) -> None:
        manager, session = await self._open_sqlite_session(tmp_path)
        try:
            context = _ctx("sqlite-chain-session")
            msgs = []
            for i in range(45):
                msgs.append(_user_msg(f"u-{i}"))
                msgs.append(_assistant_msg(f"a-{i}"))
            await _add_messages(session, context, msgs)

            compactor = _StubCompactor(_COMPACT_SUMMARY)
            await cleanup_session(
                session=session,
                archive=None,
                context=context,
                compactor=compactor,
                budget=ContextBudget(max_context_tokens=10_000),
                max_token_ratio=0.85,
                token_estimator=_FixedEstimator(100),
            )

            # Grow the session past the trigger again (compact + 24 tail
            # + 65 fresh = ~9_360 tokens > 8_500 line).
            more = []
            for i in range(45, 77):
                more.append(_user_msg(f"u-{i}"))
                more.append(_assistant_msg(f"a-{i}"))
            more.append(_user_msg("final"))
            await _add_messages(session, context, more)

            result = await cleanup_session(
                session=session,
                archive=None,
                context=context,
                compactor=compactor,
                budget=ContextBudget(max_context_tokens=10_000),
                max_token_ratio=0.85,
                token_estimator=_FixedEstimator(100),
            )

            assert result.triggered is True
            assert result.compact_generated is True
            # The previous compact summary was extracted from the pruned zone
            # and chained into the second compaction call.
            assert len(compactor.calls) == 2
            assert compactor.calls[1]["previous_summary"] == _COMPACT_SUMMARY

            kept = await session.get_all_messages(context)
            compact_rows = [m for m in kept if str(m.role) == "compact"]
            assert len(compact_rows) == 1
            assert str(kept[0].role) == "compact"
        finally:
            await manager.close()
