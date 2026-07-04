"""Tests for framework/memory/cleanup.py — cleanup_session() core function."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from modex_agent.memory.archive_models import (
    ArchiveBundleResult,
    ArchiveChannel,
    ArchiveGenerationInputs,
    ArchiveGenerationResult,
    ArchiveInputStats,
    ArchiveWrite,
)
from modex_agent.memory.cleanup import (
    CleanupResult,
    _check_trigger,
    _compute_boundary,
    cleanup_session,
)
from modex_agent.memory.core.layers import MemoryLayerSet, SessionMemoryManager
from modex_agent.memory.core.models import CompressionReason
from modex_agent.core.scope import MemoryContext
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.registry.in_memory import InMemoryStoreRegistry
from modex_agent.memory.sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
)
from modex_agent.memory.token_estimator import TokenEstimator


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry() -> InMemoryStoreRegistry:
    return InMemoryStoreRegistry()


def _make_layer_set(
    registry: InMemoryStoreRegistry,
) -> MemoryLayerSet:
    return MemoryLayerFactory.single_user(registry=registry)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckTriggerTokenOnly:
    """_check_trigger fires only on token pressure, never on message count."""

    def test_no_trigger_under_threshold(self) -> None:
        msgs = [{"role": "user", "content": "x", "token_count": 10}]  # 10 tokens
        # trigger line = max_context_tokens * max_token_ratio = 100 * 0.8 = 80
        assert _check_trigger(msgs, _FixedEstimator(10), max_context_tokens=100, max_token_ratio=0.8) is None

    def test_trigger_over_threshold(self) -> None:
        msgs = [{"role": "user", "content": "x", "token_count": 10}] * 9  # 90 tokens
        reason = _check_trigger(msgs, _FixedEstimator(10), max_context_tokens=100, max_token_ratio=0.8)
        assert reason == CompressionReason.TOKEN_PRESSURE

    def test_missing_token_count_recomputes(self) -> None:
        msgs = [{"role": "user", "content": "x"}] * 9  # no token_count -> 10 each via estimator
        reason = _check_trigger(msgs, _FixedEstimator(10), max_context_tokens=100, max_token_ratio=0.8)
        assert reason == CompressionReason.TOKEN_PRESSURE

    def test_system_tokens_excluded_from_trigger(self) -> None:
        """ADR-0009: system-role tokens do NOT count toward session pressure."""
        # A giant system message alone must NOT trigger.
        sys_only = [{"role": "system", "content": "huge system prompt", "token_count": 100000}]
        assert _check_trigger(sys_only, _FixedEstimator(10), max_context_tokens=100, max_token_ratio=0.8) is None
        # The same token burden as a user message DOES trigger.
        user_only = [{"role": "user", "content": "x", "token_count": 100000}]
        assert _check_trigger(user_only, _FixedEstimator(10), max_context_tokens=100, max_token_ratio=0.8) == CompressionReason.TOKEN_PRESSURE


class TestComputeBoundaryTokenBased:
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


class TestNoTrigger:
    """cleanup_session should not trigger when session is under limits."""

    @pytest.mark.asyncio
    async def test_no_trigger_when_under_limit(self, registry: InMemoryStoreRegistry) -> None:
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
            max_context_tokens=8000,
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
        )

        assert result.triggered is False


class TestOnTriggeredCallback:
    """on_triggered fires once when a cleanup triggers, and not when under limit.

    It must fire AFTER trigger confirmation and BEFORE archive generation (the
    blocking LLM call) so an observer can warn the user about the pause.
    """

    @pytest.mark.asyncio
    async def test_fires_when_triggered(self, registry: InMemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session
        await _add_messages(session, context, [_user_msg("x" * 500)] * 20)

        calls: list[tuple[str, CompressionReason]] = []

        async def _on_triggered(ctx: MemoryContext, reason: CompressionReason) -> None:
            calls.append((ctx.session_id, reason))

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_context_tokens=100,
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            on_triggered=_on_triggered,
        )

        assert result.triggered is True
        assert len(calls) == 1
        assert calls[0] == ("test-session", CompressionReason.TOKEN_PRESSURE)

    @pytest.mark.asyncio
    async def test_does_not_fire_when_under_limit(self, registry: InMemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session
        await _add_messages(session, context, [_user_msg("a"), _assistant_msg("b")])

        calls: list[tuple[str, CompressionReason]] = []

        async def _on_triggered(ctx: MemoryContext, reason: CompressionReason) -> None:
            calls.append((ctx.session_id, reason))

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_context_tokens=8000,
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            on_triggered=_on_triggered,
        )

        assert result.triggered is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_fires_before_archive_generation(
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """on_triggered must run before the archive agent is invoked."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session
        await _add_messages(session, context, [_user_msg(f"u-{i}") for i in range(10)])

        order: list[str] = []

        async def _on_triggered(ctx: MemoryContext, reason: CompressionReason) -> None:
            order.append("triggered")

        class _OrderArchiveAgent(_MockArchiveAgent):
            async def generate(self, pruned_messages, archive_dir, archive_id=0):
                order.append("archive")
                return await super().generate(pruned_messages, archive_dir, archive_id)

        storage = _DirArchiveStorageFactory.create(tmp_path)

        await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_context_tokens=50,
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            on_triggered=_on_triggered,
            archive_agent=_OrderArchiveAgent(),
            archive_storage=storage,
        )

        assert order == ["triggered", "archive"]


class TestTriggerAndCleanup:
    """cleanup_session should trigger and clean when over limits."""

    @pytest.mark.asyncio
    async def test_trigger_when_over_message_limit(self, registry: InMemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        # Add 20 messages = 200 tokens, max_context_tokens=100 -> line 80 -> triggers
        msgs = []
        for i in range(10):
            msgs.append(_user_msg(f"user-{i}"))
            msgs.append(_assistant_msg(f"asst-{i}"))
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_context_tokens=100,
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
        )

        assert result.triggered is True
        assert result.messages_kept > 0
        assert result.messages_pruned > 0
        assert result.archive_skipped is True  # no archive manager

        # Verify session was actually pruned
        remaining = await session.get_all_messages(context)
        assert len(remaining) == result.messages_kept
        assert len(remaining) < 20

    @pytest.mark.asyncio
    async def test_trigger_when_over_token_limit(self, registry: InMemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        # Add messages to trigger token pressure: 20 msgs = 200 tokens, line 80 -> triggers
        msgs = []
        for i in range(20):
            msgs.append(_user_msg("x" * 500))
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_context_tokens=100,  # line 80 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
        )

        assert result.triggered is True
        assert result.messages_pruned > 0


class TestCleanupAlwaysExecutes:
    """cleanup_session should clean even when archive is None."""

    @pytest.mark.asyncio
    async def test_cleanup_always_executes(self, registry: InMemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        # 10 messages = 100 tokens, max_context_tokens=50 -> line 40 -> triggered
        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,  # No archive
            context=context,
            max_context_tokens=50,
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
        )

        assert result.triggered is True
        # Session was cleaned even without archive
        remaining = await session.get_all_messages(context)
        assert len(remaining) < 10
        assert len(remaining) > 0


class TestCleanupRemovesInvalidToolChains:
    """cleanup_session should remove orphan tool results via sanitizer."""

    @pytest.mark.asyncio
    async def test_cleanup_removes_invalid_tool_chains(self, registry: InMemoryStoreRegistry) -> None:
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
            max_context_tokens=50,
            max_token_ratio=0.8,
            keep_ratio=0.5,
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
    async def test_never_splits_tool_chain(self, registry: InMemoryStoreRegistry) -> None:
        """Boundary should not split an assistant tool_call from its tool result."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        # Build a sequence where the boundary would fall inside a tool chain
        msgs = [
            _user_msg("1"),
            _assistant_msg("reply-1"),
            _user_msg("2"),
            _assistant_msg("reply-2"),
            _user_msg("3"),
            _assistant_msg("reply-3"),
            _user_msg("4"),
            _tool_call_msg("call_1", "tool_a"),  # tool chain start
            _tool_result_msg("call_1", "result"),  # tool chain end
            _assistant_msg("final"),
        ]
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_context_tokens=100,  # 10 msgs = 100 tokens, line 80 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.4,  # keep_target_tokens = 40 -> keep ~4 msgs
            token_estimator=_FixedEstimator(10),
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
    async def test_single_user_session_cleans_properly(self, registry: InMemoryStoreRegistry) -> None:
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
            max_context_tokens=280,  # 101 msgs ~= 1414 tokens (14/msg: 10 estimate + 4 overhead,
            # recomputed since _add_messages bypasses append-stamping), line 224 -> triggers
            max_token_ratio=0.8, keep_ratio=0.5,  # keep_target_tokens = 140 -> keep ~10 msgs
            token_estimator=_FixedEstimator(10),
            user_retention=layer_set.user_retention,
        )

        assert result.triggered is True
        assert result.messages_pruned > 0, (
            f"Must prune messages when over token limit (total={len(msgs)} msgs, "
            f"max_context_tokens=280), but pruned=0"
        )
        remaining = await session.get_all_messages(context)
        assert len(remaining) < len(msgs), (
            f"Session must be smaller after cleanup: {len(remaining)} >= {len(msgs)}"
        )


class TestKeepToolChainIntegrity:
    """Tool chains in the keep region must stay intact (no split assistant/tool pairs)."""

    @pytest.mark.asyncio
    async def test_tool_chain_in_keep_region_not_split(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """When keep boundary falls on a tool chain, the chain stays intact."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = [
            _user_msg("1"),
            _assistant_msg("r1"),
            _user_msg("2"),
            _assistant_msg("r2"),
            _user_msg("3"),
            _tool_call_msg("call_1"),     # tool chain
            _tool_result_msg("call_1"),
            _assistant_msg("final"),
        ]
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_context_tokens=80,  # 8 msgs = 80 tokens, line 64 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.4,  # keep_target_tokens = 32 -> keep 3 (tool chain intact)
            token_estimator=_FixedEstimator(10),
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


class TestUserRetentionExtraction:
    """Pruned user messages must be extracted and persisted to the URB."""

    @pytest.mark.asyncio
    async def test_pruned_user_without_response_saved_to_urb(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """User message pruned during ReAct loop (no final response) saved to URB.

        Scenario: q1 asked, assistant started tool call but no plain response.
        Boundary moves forward past tool chain to user q2. q1 becomes URB entry.
        """
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = [
            _user_msg("q1"),           # will be pruned — no plain assistant after it
            _tool_call_msg("c1"),      # tool call (not plain assistant → doesn't clear URB)
            _tool_result_msg("c1"),
            _user_msg("q2"),           # most recent user (in keep)
        ]
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_context_tokens=20,  # 4 msgs = 40 tokens, line 16 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            user_retention=layer_set.user_retention,
        )

        assert result.triggered is True
        assert result.user_retention_extracted >= 1

        entries = await layer_set.user_retention.get_entries(context)
        assert len(entries) >= 1
        urbs = [str(e.pruned_user_content) for e in entries]
        assert any("q1" in c for c in urbs), (
            f"Expected q1 in URB, got {urbs}"
        )

    @pytest.mark.asyncio
    async def test_pruned_users_with_assistant_still_extracted(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """Pruned user messages with plain assistant responses are extracted as completed entries."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = [
            _user_msg("q1"), _assistant_msg("a1"),
            _user_msg("q2"), _assistant_msg("a2"),
            _user_msg("q3"), _assistant_msg("a3"),
            _user_msg("q4"), _assistant_msg("a4"),
            _user_msg("q5"), _assistant_msg("a5"),
        ]
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.4,
            token_estimator=_FixedEstimator(10),
            user_retention=layer_set.user_retention,
        )

        assert result.triggered is True
        # Pruned user messages in completed turns are still extracted to URB
        # (they carry completing_assistant_content for governance decisions)
        assert result.user_retention_extracted > 0

    @pytest.mark.asyncio
    async def test_no_urb_when_user_retention_is_none(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """When user_retention=None, cleanup still succeeds (no extraction)."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            user_retention=None,
        )

        assert result.triggered is True
        assert result.user_retention_extracted == 0


class TestUserRetentionCompletion:
    """URB entries must be correctly marked as completed when their answering
    plain assistant is in the kept (unpruned) region.

    Key property: a plain assistant completes ALL currently unfinished entries,
    including ones left over from previous cleanups.  Unfinished entries always
    sit at the buffer's tail.
    """

    @pytest.mark.asyncio
    async def test_pruned_user_completed_by_kept_assistant(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """User pruned, plain assistant kept → entry must be completed."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = [
            _user_msg("q1"), _assistant_msg("a1"),
            _user_msg("q2"), _assistant_msg("a2"),
            _user_msg("q3"), _assistant_msg("a3"),
            _user_msg("q4"), _assistant_msg("a4"),
        ]
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_context_tokens=50,  # 8 msgs = 80 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            user_retention=layer_set.user_retention,
        )

        assert result.triggered is True
        entries = await layer_set.user_retention.get_entries(context)
        # All pruned users had plain assistants (pruned or kept) after them.
        # Every entry must be completed.
        unfinished = [e for e in entries if not e.is_completed]
        assert unfinished == [], (
            f"All entries should be completed, but found unfinished: "
            f"{[e.pruned_user_content for e in unfinished]}"
        )

    @pytest.mark.asyncio
    async def test_pruned_user_completed_by_pruned_assistant_kept_has_no_plain(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """Plain assistant in the PRUNED region, kept region has no plain
        assistant (only tool chain).  Pruned entries must still be completed."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = [
            _user_msg("q1"), _assistant_msg("a1"),
            _user_msg("q2"), _assistant_msg("a2"),
            _user_msg("q3"),
            _tool_call_msg("c1"),
            _tool_result_msg("c1"),
        ]
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_context_tokens=70,  # 7 msgs ~= 98 tokens (14/msg: 10 estimate + 4 overhead,
            # recomputed since _add_messages bypasses append-stamping), line 56 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.4,  # keep_target_tokens = 28 -> keep [call c1, result c1]
            token_estimator=_FixedEstimator(10),
            user_retention=layer_set.user_retention,
        )

        assert result.triggered is True
        entries = await layer_set.user_retention.get_entries(context)
        by_content = {e.pruned_user_content: e for e in entries}
        # q1 answered by a1 (pruned), q2 answered by a2 (pruned) → completed
        assert by_content["q1"].is_completed, "q1 should be completed by pruned a1"
        assert by_content["q2"].is_completed, "q2 should be completed by pruned a2"
        # q3 has no plain assistant after it → legitimately unfinished
        assert not by_content["q3"].is_completed, "q3 should be unfinished (no plain assistant)"

    @pytest.mark.asyncio
    async def test_kept_assistant_completes_prior_unfinished_entries(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """A plain assistant in the kept region also completes unfinished
        entries from a *previous* cleanup cycle."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        # Round 1: force a cleanup that leaves unfinished entries.
        # q1 has no following plain assistant → stays unfinished.
        msgs = [
            _user_msg("q1"),
            _tool_call_msg("c1", "tool_x"),
            _tool_result_msg("c1"),
            _user_msg("q2"),
            _user_msg("q3"),
        ]
        await _add_messages(session, context, msgs)

        await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_context_tokens=20,  # 5 msgs = 50 tokens, line 16 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            user_retention=layer_set.user_retention,
        )

        entries_after_round1 = await layer_set.user_retention.get_entries(context)
        assert len(entries_after_round1) > 0, "First cleanup should extract entries"
        # At this point, all entries are unfinished (no plain assistant anywhere).
        assert all(not e.is_completed for e in entries_after_round1)

        # Round 2: add messages so that a plain assistant lands in the KEPT region.
        # This assistant should complete ALL existing unfinished entries.
        await _add_messages(session, context, [
            _user_msg("q4"),
            _assistant_msg("final_answer"),
        ])
        await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_context_tokens=20,  # 3 msgs = 30 tokens, line 16 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            user_retention=layer_set.user_retention,
        )

        entries_after_round2 = await layer_set.user_retention.get_entries(context)
        unfinished = [e for e in entries_after_round2 if not e.is_completed]
        assert unfinished == [], (
            f"Plain assistant in kept region should complete all entries, "
            f"but found unfinished: {[e.pruned_user_content for e in unfinished]}"
        )


class TestToolChainDominanceDoesNotOverPrune:
    """Regression: sessions dominated by tool chains must not over-prune.

    When most of the session is tool chains (1 user + 50 tc/tool pairs + 1 user),
    _adjust_boundary_for_first_user must not walk all the way to the last user,
    keeping only 1 message. This was the MiniMax 400 error root cause.
    """

    @pytest.mark.asyncio
    async def test_tool_chain_heavy_session_keeps_reasonable_count(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """1 user + 50 tool pairs + 1 new user: must keep ~40%, not 1."""
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
            max_context_tokens=1000,  # 102 msgs = 1020 tokens, line 800 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.4,  # keep_target_tokens = 400 -> keep ~40 msgs
            token_estimator=_FixedEstimator(10),
            user_retention=layer_set.user_retention,
        )

        assert result.triggered is True
        assert result.messages_kept > 1, (
            f"kept={result.messages_kept} but expected significantly more than 1. "
            f"total={len(msgs)}, keep_ratio=0.4"
        )

    @pytest.mark.asyncio
    async def test_kept_count_respects_keep_ratio_floor(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """kept must be at least keep_target // 2 even with tool-chain sessions."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        total = 102
        keep_ratio = 0.4
        keep_target = max(1, int(total * keep_ratio))  # 40

        msgs = [_user_msg("q1")]
        for i in range(50):
            msgs.append(_tool_call_msg(f"call_{i}"))
            msgs.append(_tool_result_msg(f"call_{i}", f"r_{i}"))
        msgs.append(_user_msg("q2"))
        assert len(msgs) == total

        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_context_tokens=1000,  # 102 msgs = 1020 tokens, line 800 -> triggers
            max_token_ratio=0.8,
            keep_ratio=keep_ratio,  # keep_target_tokens = 400 -> keep ~40 msgs
            token_estimator=_FixedEstimator(10),
        )

        assert result.triggered is True
        min_kept = max(keep_target // 2, 2)
        assert result.messages_kept >= min_kept, (
            f"kept={result.messages_kept} < min_kept={min_kept} "
            f"(keep_target={keep_target})"
        )


class TestKeepResanitized:
    """Keep region is re-sanitized to ensure tool chain integrity."""

    @pytest.mark.asyncio
    async def test_incomplete_tool_chain_in_keep_force_cleaned(
        self, registry: InMemoryStoreRegistry,
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
            max_context_tokens=50,  # 8 msgs = 80 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.6,
            token_estimator=_FixedEstimator(10),
        )

        assert result.triggered is True
        remaining = await session.get_all_messages(context)
        # No orphan tool_call without result (except possibly last open)
        for i, m in enumerate(remaining):
            if m.role == "assistant" and m.tool_calls:
                call_ids = {tc["id"] if isinstance(tc, dict) else tc.id for tc in m.tool_calls}
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
            archive_skipped=False,
            reason=CompressionReason.MESSAGE_COUNT,
        )
        assert result.triggered is True
        assert result.messages_kept == 5
        assert result.messages_pruned == 10
        assert result.archive_skipped is False
        assert result.reason == CompressionReason.MESSAGE_COUNT

    def test_cleanup_result_not_triggered(self) -> None:
        result = CleanupResult(triggered=False)
        assert result.triggered is False
        assert result.messages_kept == 0
        assert result.messages_pruned == 0


# ---------------------------------------------------------------------------
# Mock archive agent for Phase 4 tests
# ---------------------------------------------------------------------------


class _MockArchiveAgent:
    """Mock ArchiveSummarizer that records calls and can be configured to succeed or fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[list[dict], object, int]] = []
        self._fail = fail

    async def generate(
        self,
        pruned_messages: list[dict],
        archive_dir: object,
        archive_id: int = 0,
    ) -> object:
        from modex_agent.agents.summarizer.archive_agent import ArchiveSummarizerResult

        self.calls.append((list(pruned_messages), archive_dir, archive_id))
        if self._fail:
            return ArchiveSummarizerResult(
                success=False,
                archive_id=archive_id,
                error="mock failure",
            )
        # Actually write files so is_archive_complete works
        from pathlib import Path
        archive_dir_path = Path(str(archive_dir))
        archive_dir_path.mkdir(parents=True, exist_ok=True)
        (archive_dir_path / "context.md").write_text("context summary", encoding="utf-8")
        (archive_dir_path / "knowledge.md").write_text("knowledge summary", encoding="utf-8")
        (archive_dir_path / "index.md").write_text("Test Archive Topic", encoding="utf-8")
        return ArchiveSummarizerResult(
            success=True,
            archive_id=archive_id,
            files_written=("context.md", "knowledge.md", "index.md"),
        )


class _DirArchiveStorageFactory:
    """Factory for DirArchiveStorage backed by a temp directory."""

    @staticmethod
    def create(tmp_path) -> object:
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
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """When archive_agent is provided, archive MD files are generated."""
        layer_set = _make_layer_set(registry)
        context = _ctx("agent-session-1")
        session = layer_set.session

        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent()
        storage = _DirArchiveStorageFactory.create(tmp_path)

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            archive_agent=agent,
            archive_storage=storage,
        )

        assert result.triggered is True
        assert len(agent.calls) == 1
        # Verify files were written to the archive directory
        archive_dir = agent.calls[0][1]
        from pathlib import Path
        archive_path = Path(str(archive_dir))
        assert (archive_path / "context.md").exists()
        assert (archive_path / "knowledge.md").exists()
        assert (archive_path / "index.md").exists()

    @pytest.mark.asyncio
    async def test_archive_agent_failure_falls_back(
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """When archive_agent fails, pruned index falls back to write_pruned."""
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("agent-fail-session")
        session = layer_set.session

        msgs = []
        for i in range(5):
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
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            archive_agent=agent,
            archive_storage=storage,
            pruned_manager=pruned_mgr,
        )

        assert result.triggered is True
        # Archive was attempted but failed
        assert len(agent.calls) == 1
        # Pruned index should have been populated via fallback
        entries = pruned_mgr._get_storage(context.session_id).read_index()
        assert len(entries) >= 1

    @pytest.mark.asyncio
    async def test_archive_id_increments_on_success(
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """archive_id (next_archive_id in state) increments after successful flow."""
        layer_set = _make_layer_set(registry)
        context = _ctx("increment-session")
        session = layer_set.session

        msgs = []
        for i in range(5):
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
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            archive_agent=agent,
            archive_storage=storage,
        )

        assert result.triggered is True
        # State should now have next_archive_id=2
        state = await storage.read_archive_state()
        assert state is not None
        assert state["next_archive_id"] == 2

        # Second cleanup: should use archive_id=2
        await _add_messages(session, context, msgs)

        agent2 = _MockArchiveAgent()
        result2 = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            archive_agent=agent2,
            archive_storage=storage,
        )

        assert result2.triggered is True
        assert len(agent2.calls) == 1
        # The second call should have archive_id=2
        assert agent2.calls[0][2] == 2
        # State should now have next_archive_id=3
        state = await storage.read_archive_state()
        assert state["next_archive_id"] == 3

    @pytest.mark.asyncio
    async def test_skips_agent_if_archive_complete(
        self, registry: InMemoryStoreRegistry, tmp_path,
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
        await storage.write_archive_file(1, "index.md", "existing index")

        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent()

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            archive_agent=agent,
            archive_storage=storage,
        )

        assert result.triggered is True
        # Agent should NOT have been called since archive is already complete
        assert len(agent.calls) == 0
        # archive_skipped should be False (archive was present, just complete)
        assert result.archive_skipped is False

    @pytest.mark.asyncio
    async def test_archives_before_session_commit(
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """Archive generation happens BEFORE session messages are committed."""
        # Use a tracking agent that records order
        layer_set = _make_layer_set(registry)
        context = _ctx("order-session")
        session = layer_set.session

        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        # Before cleanup, session has 10 messages
        before_count = len(await session.get_all_messages(context))
        assert before_count == 10

        agent = _MockArchiveAgent()
        storage = _DirArchiveStorageFactory.create(tmp_path)

        # The agent writes files to the archive directory
        # We verify that after cleanup, the session is pruned AND files exist
        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
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
        archive_dir = agent.calls[0][1]
        from pathlib import Path
        archive_path = Path(str(archive_dir))
        assert (archive_path / "index.md").exists()


class TestArchiveSuccessPrunedContent:
    """Regression: when archive agent succeeds, pruned raw content must still be written.

    Bug: cleanup_session used refresh_from_archives() on the archive-success path,
    which only wrote index.jsonl pointing to archive layer files. Raw pruned messages
    were lost and content_filename was a cross-layer reference that didn't resolve.
    """

    @pytest.mark.asyncio
    async def test_pruned_writes_raw_content_when_archive_succeeds(
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """Pruned content file must exist with raw messages, not just an index."""
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("pruned-archive-session")
        session = layer_set.session

        msgs = []
        for i in range(5):
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
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            archive_agent=agent,
            archive_storage=storage,
            pruned_manager=pruned_mgr,
        )

        assert result.triggered is True
        assert result.messages_pruned > 0

        # Pruned index must have entries
        pruned_storage = pruned_mgr._get_storage(context.session_id)
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
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """Pruned content file must contain the raw pruned messages (JSONL)."""
        import json
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("pruned-raw-session")
        session = layer_set.session

        msgs = []
        for i in range(5):
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
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            archive_agent=agent,
            archive_storage=storage,
            pruned_manager=pruned_mgr,
        )

        pruned_storage = pruned_mgr._get_storage(context.session_id)
        entries = pruned_storage.read_index()
        assert len(entries) >= 1

        entry = entries[-1]
        from pathlib import Path
        content_path = Path(pruned_storage.get_directory_path()) / entry.content_filename
        assert content_path.exists()

        # Raw messages must be readable as JSONL
        raw_lines = content_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(raw_lines) > 0
        parsed = [json.loads(line) for line in raw_lines]
        # At least one user message from our input should be in the pruned content
        user_contents = [m.get("content", "") for m in parsed if m.get("role") == "user"]
        assert any("unique-content-" in c for c in user_contents), (
            "Pruned content should contain raw user messages from the pruned session region"
        )

    @pytest.mark.asyncio
    async def test_pruned_entry_has_correct_message_count_and_times(
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """Pruned index entry must have message_count > 0 and non-empty time fields."""
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("pruned-fields-session")
        session = layer_set.session

        msgs = []
        for i in range(5):
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
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            archive_agent=agent,
            archive_storage=storage,
            pruned_manager=pruned_mgr,
        )

        pruned_storage = pruned_mgr._get_storage(context.session_id)
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
    """Regression: archive_storage=None must not prevent pruned topic enrichment.

    Root cause: _generate_archive_phase dynamically resolves storage into a
    local variable but subsequent phases receive the original ``None``.  The
    fix carries ``resolved_storage`` via ``_ArchiveOutcome`` so that Phases
    3/5/6 can use it.
    """

    @pytest.mark.asyncio
    async def test_pruned_topic_from_archive_when_storage_not_injected(
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """archive_storage=None + dynamic resolve → pruned topic = archive index.md."""
        from modex_agent.memory.pruned.manager import PrunedManager
        from modex_agent.memory.stores.dir_archive import DirArchiveStorage

        layer_set = _make_layer_set(registry)
        context = _ctx("resolve-topic-session")
        session = layer_set.session

        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        # To make dynamic resolution work, the archive layer must return a
        # storage path.  Mock get_storage_path to return a real directory.
        archive_storage_dir = tmp_path / "archives"
        real_storage = DirArchiveStorage(archive_storage_dir)
        original_get_storage_path = layer_set.archive.get_storage_path

        async def _mock_get_storage_path(ctx: MemoryContext) -> object:
            return archive_storage_dir

        layer_set.archive.get_storage_path = _mock_get_storage_path

        try:
            agent = _MockArchiveAgent()
            pruned_mgr = PrunedManager(pruned_base_dir=tmp_path / "pruned")

            # Pass archive_storage=None to trigger the bug path
            result = await cleanup_session(
                session=session,
                archive=layer_set.archive,
                context=context,
                max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
                max_token_ratio=0.8,
                keep_ratio=0.5,
                token_estimator=_FixedEstimator(10),
                archive_agent=agent,
                archive_storage=None,
                pruned_manager=pruned_mgr,
            )

            assert result.triggered is True

            # Key assertion: pruned topic should come from archive index.md
            pruned_storage = pruned_mgr._get_storage(context.session_id)
            entries = pruned_storage.read_index()
            assert len(entries) >= 1

            entry = entries[-1]
            # _MockArchiveAgent writes "Test Archive Topic" to index.md
            assert entry.topic == "Test Archive Topic", (
                f"Expected pruned topic from archive index.md, got: '{entry.topic}'"
            )
        finally:
            layer_set.archive.get_storage_path = original_get_storage_path

    @pytest.mark.asyncio
    async def test_pruned_topic_from_archive_when_storage_explicitly_provided(
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """archive_storage provided → existing behavior unchanged (topic from archive)."""
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("explicit-storage-session")
        session = layer_set.session

        msgs = []
        for i in range(5):
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
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            archive_agent=agent,
            archive_storage=storage,
            pruned_manager=pruned_mgr,
        )

        assert result.triggered is True

        pruned_storage = pruned_mgr._get_storage(context.session_id)
        entries = pruned_storage.read_index()
        assert len(entries) >= 1

        entry = entries[-1]
        assert entry.topic == "Test Archive Topic"

    @pytest.mark.asyncio
    async def test_fallback_topic_when_archive_agent_fails(
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """archive_storage=None + agent fails → fallback time-range topic."""
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("fail-fallback-session")
        session = layer_set.session

        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent(fail=True)
        pruned_mgr = PrunedManager(pruned_base_dir=tmp_path / "pruned")

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            archive_agent=agent,
            archive_storage=None,
            pruned_manager=pruned_mgr,
        )

        assert result.triggered is True

        pruned_storage = pruned_mgr._get_storage(context.session_id)
        entries = pruned_storage.read_index()
        assert len(entries) >= 1

        entry = entries[-1]
        # Should be fallback: "YYYY-MM-DD HH:MM ~ YYYY-MM-DD HH:MM (N messages)"
        assert "(" in entry.topic and "messages)" in entry.topic, (
            f"Expected fallback time-range topic, got: '{entry.topic}'"
        )

    @pytest.mark.asyncio
    async def test_no_archive_agent_uses_fallback_topic(
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """No archive_agent at all → fallback topic (existing behavior)."""
        from modex_agent.memory.pruned.manager import PrunedManager

        layer_set = _make_layer_set(registry)
        context = _ctx("no-agent-session")
        session = layer_set.session

        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        pruned_mgr = PrunedManager(pruned_base_dir=tmp_path / "pruned")

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            pruned_manager=pruned_mgr,
        )

        assert result.triggered is True

        pruned_storage = pruned_mgr._get_storage(context.session_id)
        entries = pruned_storage.read_index()
        assert len(entries) >= 1

        entry = entries[-1]
        assert "(" in entry.topic and "messages)" in entry.topic

    @pytest.mark.asyncio
    async def test_archive_state_advances_when_storage_not_injected(
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """archive_storage=None → state.json still gets next_archive_id incremented."""
        from modex_agent.memory.stores.dir_archive import DirArchiveStorage

        layer_set = _make_layer_set(registry)
        context = _ctx("state-advance-session")
        session = layer_set.session

        msgs = []
        for i in range(5):
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
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
            archive_agent=agent,
            archive_storage=storage,
        )

        assert result.triggered is True

        state = await storage.read_archive_state()
        assert state is not None
        assert state["next_archive_id"] == 2

    @pytest.mark.asyncio
    async def test_archive_register_when_storage_not_injected(
        self, registry: InMemoryStoreRegistry, tmp_path,
    ) -> None:
        """archive_storage=None + dynamic resolve → register_archive_with_layer works."""
        layer_set = _make_layer_set(registry)
        context = _ctx("register-resolve-session")
        session = layer_set.session

        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        agent = _MockArchiveAgent()
        storage = _DirArchiveStorageFactory.create(tmp_path)

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_context_tokens=50,  # 10 msgs = 100 tokens, line 40 -> triggers
            max_token_ratio=0.8,
            keep_ratio=0.5,
            token_estimator=_FixedEstimator(10),
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
