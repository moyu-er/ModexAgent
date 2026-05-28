"""Tests for framework/memory/cleanup.py — cleanup_session() core function."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from framework.memory.archive_generation import (
    ArchiveGenerationStrategy,
    ArchiveInputMessage,
)
from framework.memory.archive_models import (
    ArchiveBundleResult,
    ArchiveChannel,
    ArchiveGenerationInputs,
    ArchiveGenerationResult,
    ArchiveInputStats,
    ArchiveWrite,
)
from framework.memory.cleanup import CleanupResult, cleanup_session
from framework.memory.core.layers import MemoryLayerSet, SessionMemoryManager
from framework.memory.core.models import CompressionReason
from framework.memory.core.scope import MemoryContext
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.registry.in_memory import InMemoryStoreRegistry
from framework.memory.sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationMode,
)


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


class _CountingArchiveStrategy(ArchiveGenerationStrategy):
    """Stub archive strategy that records calls and can be configured to succeed or fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[list[ArchiveInputMessage], MemoryContext, CompressionReason]] = []
        self._fail = fail

    async def generate(
        self,
        messages: Sequence[ArchiveInputMessage],
        context: MemoryContext,
        reason: CompressionReason,
    ) -> ArchiveGenerationResult:
        self.calls.append((list(messages), context, reason))
        if self._fail:
            raise RuntimeError("archive generation failed")
        return ArchiveGenerationResult(
            writes=(
                ArchiveWrite(
                    channel=ArchiveChannel.CONTEXT,
                    summary="archived summary",
                    metadata={"reason": reason.value},
                ),
            ),
            inputs=ArchiveGenerationInputs(
                context_transcript="transcript",
                knowledge_transcript="transcript",
                stats=ArchiveInputStats(
                    input_messages=len(messages),
                    context_messages=len(messages),
                    knowledge_messages=0,
                    tool_chains=0,
                    dropped_messages=0,
                ),
            ),
        )


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


class TestNoTrigger:
    """cleanup_session should not trigger when session is under limits."""

    @pytest.mark.asyncio
    async def test_no_trigger_when_under_limit(self, registry: InMemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        # Add only 3 messages — well under max_messages=100
        await _add_messages(session, context, [
            _user_msg("a"), _assistant_msg("b"), _user_msg("c"),
        ])

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_messages=100,
            max_tokens=8000,
            keep_ratio=0.5,
            archive_strategy=None,
        )

        assert result.triggered is False
        assert result.messages_kept == 0
        assert result.messages_pruned == 0


class TestTriggerAndCleanup:
    """cleanup_session should trigger and clean when over limits."""

    @pytest.mark.asyncio
    async def test_trigger_when_over_message_limit(self, registry: InMemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        # Add 20 messages, max_messages=10 → should trigger
        msgs = []
        for i in range(10):
            msgs.append(_user_msg(f"user-{i}"))
            msgs.append(_assistant_msg(f"asst-{i}"))
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_messages=10,
            max_tokens=None,
            keep_ratio=0.5,
            archive_strategy=None,
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

        # Add messages with substantial content to trigger token pressure
        msgs = []
        for i in range(20):
            msgs.append(_user_msg("x" * 500))  # ~125 tokens each
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_messages=1000,
            max_tokens=500,  # very low limit → triggers
            keep_ratio=0.5,
            archive_strategy=None,
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

        # 10 messages, max_messages=5 → triggered
        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,  # No archive
            context=context,
            max_messages=5,
            max_tokens=None,
            keep_ratio=0.5,
            archive_strategy=None,
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
            max_messages=5,
            max_tokens=None,
            keep_ratio=0.5,
            archive_strategy=None,
        )

        assert result.triggered is True
        # Orphan tool result should have been removed during sanitization
        remaining = await session.get_all_messages(context)
        for msg in remaining:
            # No orphan tool results should remain
            if msg.role == "tool" and msg.tool_call_id == "call_orphan":
                pytest.fail("Orphan tool result should have been sanitized away")


class TestArchiveIntegration:
    """Tests for archive strategy interaction."""

    @pytest.mark.asyncio
    async def test_archive_called_when_archive_present(self, registry: InMemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        strategy = _CountingArchiveStrategy()

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_messages=5,
            max_tokens=None,
            keep_ratio=0.5,
            archive_strategy=strategy,
        )

        assert result.triggered is True
        assert result.archive_skipped is False
        assert len(strategy.calls) == 1
        # The strategy should have received the pruned messages
        pruned_msgs = strategy.calls[0][0]
        assert len(pruned_msgs) > 0

    @pytest.mark.asyncio
    async def test_archive_skipped_when_archive_is_none(self, registry: InMemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        strategy = _CountingArchiveStrategy()

        result = await cleanup_session(
            session=session,
            archive=None,  # no archive manager
            context=context,
            max_messages=5,
            max_tokens=None,
            keep_ratio=0.5,
            archive_strategy=strategy,
        )

        assert result.triggered is True
        assert result.archive_skipped is True
        # Strategy should NOT have been called
        assert len(strategy.calls) == 0

    @pytest.mark.asyncio
    async def test_archive_skipped_when_strategy_is_none(self, registry: InMemoryStoreRegistry) -> None:
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
            archive=layer_set.archive,  # archive exists
            context=context,
            max_messages=5,
            max_tokens=None,
            keep_ratio=0.5,
            archive_strategy=None,  # but no strategy
        )

        assert result.triggered is True
        assert result.archive_skipped is True


class TestArchiveFailureCounter:
    """Tests for the archive failure counter."""

    @pytest.mark.asyncio
    async def test_archive_failure_increments_counter(self, registry: InMemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx("fail-session-1")
        session = layer_set.session

        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-{i}"))
            msgs.append(_assistant_msg(f"a-{i}"))
        await _add_messages(session, context, msgs)

        strategy = _CountingArchiveStrategy(fail=True)

        # First call: strategy raises → failure counter = 1
        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_messages=5,
            max_tokens=None,
            keep_ratio=0.5,
            archive_strategy=strategy,
            archive_fail_threshold=3,
        )

        assert result.triggered is True
        # Session cleanup still succeeded even though archive failed
        remaining = await session.get_all_messages(context)
        assert len(remaining) < 10

        # Need to re-add messages for second attempt
        await _add_messages(session, context, msgs)

        result2 = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_messages=5,
            max_tokens=None,
            keep_ratio=0.5,
            archive_strategy=strategy,
            archive_fail_threshold=3,
        )
        assert result2.triggered is True

    @pytest.mark.asyncio
    async def test_archive_skipped_after_consecutive_failures(self, registry: InMemoryStoreRegistry) -> None:
        layer_set = _make_layer_set(registry)
        context = _ctx("skip-session")
        session = layer_set.session

        strategy = _CountingArchiveStrategy(fail=True)

        # Run enough times to hit the threshold
        threshold = 2
        for attempt in range(threshold):
            msgs = []
            for i in range(5):
                msgs.append(_user_msg(f"u-{attempt}-{i}"))
                msgs.append(_assistant_msg(f"a-{attempt}-{i}"))
            await _add_messages(session, context, msgs)

            await cleanup_session(
                session=session,
                archive=layer_set.archive,
                context=context,
                max_messages=5,
                max_tokens=None,
                keep_ratio=0.5,
                archive_strategy=strategy,
                archive_fail_threshold=threshold,
            )

        # After hitting threshold, next call should skip archive entirely
        msgs = []
        for i in range(5):
            msgs.append(_user_msg(f"u-final-{i}"))
            msgs.append(_assistant_msg(f"a-final-{i}"))
        await _add_messages(session, context, msgs)

        # Reset strategy to succeed now — but counter should cause skip
        strategy._fail = False
        call_count_before = len(strategy.calls)

        result = await cleanup_session(
            session=session,
            archive=layer_set.archive,
            context=context,
            max_messages=5,
            max_tokens=None,
            keep_ratio=0.5,
            archive_strategy=strategy,
            archive_fail_threshold=threshold,
        )

        assert result.triggered is True
        assert result.archive_skipped is True
        # Strategy should not have been called on this attempt (skipped due to counter)
        assert len(strategy.calls) == call_count_before


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
            max_messages=6,  # trigger cleanup
            max_tokens=None,
            keep_ratio=0.4,  # keep ~4 messages
            archive_strategy=None,
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
    async def test_always_keeps_recent_user_message(self, registry: InMemoryStoreRegistry) -> None:
        """The most recent user message should always be kept as anchor."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = []
        for i in range(10):
            msgs.append(_user_msg(f"user-{i}"))
            msgs.append(_assistant_msg(f"asst-{i}"))
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_messages=10,
            max_tokens=None,
            keep_ratio=0.2,  # keep very few
            archive_strategy=None,
        )

        assert result.triggered is True
        remaining = await session.get_all_messages(context)
        last_user = msgs[-2]  # last user message
        user_contents = [m.content for m in remaining if m.role == "user"]
        assert last_user["content"] in user_contents, (
            "Most recent user message should be kept as anchor"
        )


class TestKeepStartsWithUser:
    """Keep region must always start with a user message (turn boundary)."""

    @pytest.mark.asyncio
    async def test_keep_starts_with_user_after_tool_chain_adjust(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """When tool chain adjustment moves boundary backward, keep still starts with user."""
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
            max_messages=5,
            max_tokens=None,
            keep_ratio=0.4,
            archive_strategy=None,
        )

        assert result.triggered is True
        remaining = await session.get_all_messages(context)
        assert len(remaining) > 0
        first_role = remaining[0].role
        assert first_role == "user", (
            f"Keep region must start with 'user', got '{first_role}': "
            f"{[m.role for m in remaining]}"
        )

    @pytest.mark.asyncio
    async def test_keep_starts_with_user_plain_assistant_boundary(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """When boundary falls on assistant(plain), move forward to user."""
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = [
            _user_msg("1"),
            _assistant_msg("r1"),
            _user_msg("2"),
            _assistant_msg("r2"),  # boundary would fall here
            _user_msg("3"),
            _assistant_msg("r3"),
        ]
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_messages=4,
            max_tokens=None,
            keep_ratio=0.4,
            archive_strategy=None,
        )

        assert result.triggered is True
        remaining = await session.get_all_messages(context)
        first_role = remaining[0].role
        assert first_role == "user", (
            f"Keep region must start with 'user', got '{first_role}'"
        )


class TestPendingExtraction:
    """Pending inputs must be extracted from pruned messages and persisted."""

    @pytest.mark.asyncio
    async def test_pruned_user_without_response_saved_as_pending(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """User message pruned during ReAct loop (no final response) saved as pending.

        Scenario: q1 asked, assistant started tool call but no plain response.
        Boundary moves forward past tool chain to user q2. q1 becomes pending.
        """
        layer_set = _make_layer_set(registry)
        context = _ctx()
        session = layer_set.session

        msgs = [
            _user_msg("q1"),           # will be pruned — no plain assistant after it
            _tool_call_msg("c1"),      # tool call (not plain assistant → doesn't clear pending)
            _tool_result_msg("c1"),
            _user_msg("q2"),           # most recent user (in keep)
        ]
        await _add_messages(session, context, msgs)

        result = await cleanup_session(
            session=session,
            archive=None,
            context=context,
            max_messages=2,
            max_tokens=None,
            keep_ratio=0.5,
            archive_strategy=None,
            pending=layer_set.pending,
        )

        assert result.triggered is True
        assert result.pending_extracted >= 1

        entries = await layer_set.pending.get_entries(context)
        assert len(entries) >= 1
        pending_contents = [str(e.content) for e in entries]
        assert any("q1" in c for c in pending_contents), (
            f"Expected q1 in pending, got {pending_contents}"
        )

    @pytest.mark.asyncio
    async def test_completed_turn_not_saved_as_pending(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """User messages with a plain assistant response NOT pending."""
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
            max_messages=5,
            max_tokens=None,
            keep_ratio=0.4,
            archive_strategy=None,
            pending=layer_set.pending,
        )

        assert result.triggered is True
        # All pruned user messages have plain assistant responses → no pending
        assert result.pending_extracted == 0

    @pytest.mark.asyncio
    async def test_no_pending_when_pending_manager_is_none(
        self, registry: InMemoryStoreRegistry,
    ) -> None:
        """When pending=None, cleanup still succeeds (no extraction)."""
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
            max_messages=5,
            max_tokens=None,
            keep_ratio=0.5,
            archive_strategy=None,
            pending=None,
        )

        assert result.triggered is True
        assert result.pending_extracted == 0


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
            max_messages=5,
            max_tokens=None,
            keep_ratio=0.6,
            archive_strategy=None,
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
