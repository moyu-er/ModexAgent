"""Tests for ScopedMessageHistory cache behavior.

Verifies: incremental cache update on append, cache hit on to_list,
cache None fallback, initial_messages preservation, clear/replace_all invalidation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from modex_agent.core.message import ChatMessage, MessageRole, ToolCall
from modex_agent.memory.budget import ContextBudget
from modex_agent.memory.cleanup import CleanupResult, CompactionSource
from modex_agent.memory.history import ScopedMessageHistory
from modex_agent.memory.scope import MemoryContext

from .conftest import NoCompactionMemorySystem

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> MemoryContext:
    return MemoryContext(session_id="test-session", user_id="test-user")


def _msg(
    role: MessageRole = MessageRole.USER,
    content: str = "hello",
    token_count: int | None = None,
) -> ChatMessage:
    return ChatMessage(role=role, content=content, token_count=token_count)


def _make_manager_mock(
    messages: list[ChatMessage] | None = None,
) -> MagicMock:
    """Create a mock SessionMemoryManager that tracks calls."""
    mgr = MagicMock()
    mgr.add_messages = AsyncMock()
    mgr.get_recent_messages = AsyncMock(return_value=messages or [])
    mgr.clear = AsyncMock()
    mgr.replace_messages = AsyncMock()
    return mgr


# Empty cleanup_config: max_context_tokens is None → check_cleanup_trigger returns None → no trigger
_NO_CLEANUP: dict[str, int | float] = {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAppendUpdatesCache:
    """append/extend incrementally update cache instead of invalidating."""

    async def test_append_preserves_cache(self, noop_memory_system) -> None:
        msg1 = _msg(content="first")
        msg2 = _msg(content="second")
        mgr = _make_manager_mock()
        history = ScopedMessageHistory(
            manager=mgr,
            context=_ctx(),
            memory_system=noop_memory_system,
            initial_messages=[msg1],
            cleanup_config=_NO_CLEANUP,
        )
        await history.append(msg2)
        msgs = await history.to_list()
        assert len(msgs) == 2
        assert msgs[0].content == "first"
        assert msgs[1].content == "second"
        # Cache was populated from initial_messages, append updated it → no DB read needed
        mgr.get_recent_messages.assert_not_called()

    async def test_extend_preserves_cache(self, noop_memory_system) -> None:
        msg1 = _msg(content="first")
        msg2 = _msg(content="second")
        msg3 = _msg(content="third")
        mgr = _make_manager_mock()
        history = ScopedMessageHistory(
            manager=mgr,
            context=_ctx(),
            memory_system=noop_memory_system,
            initial_messages=[msg1],
            cleanup_config=_NO_CLEANUP,
        )
        await history.extend([msg2, msg3])
        msgs = await history.to_list()
        assert len(msgs) == 3
        assert [m.content for m in msgs] == ["first", "second", "third"]
        mgr.get_recent_messages.assert_not_called()


class _CompactedMemorySystem(NoCompactionMemorySystem):
    """The write-side compaction completed and changed the backing store."""

    async def compact_session(
        self,
        context: MemoryContext,
        *,
        budget: ContextBudget | None = None,
        source: CompactionSource,
    ) -> CleanupResult:
        _ = context, budget, source
        return CleanupResult(triggered=True, messages_pruned=1)


class TestPostAppendCompactionCache:
    async def test_append_reloads_store_after_compaction(self) -> None:
        assistant = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="delegating",
            tool_calls=[
                ToolCall(
                    tool_name="task",
                    arguments={"target_agent": "mid", "content": "investigate"},
                    call_id="delegate-1",
                )
            ],
        )
        mgr = _make_manager_mock(messages=[assistant])
        history = ScopedMessageHistory(
            manager=mgr,
            context=_ctx(),
            memory_system=_CompactedMemorySystem(),
            initial_messages=[],
            cleanup_config={"max_context_tokens": 100},
        )

        await history.append(assistant)

        # The same history instance is handed from LLMNode to ToolNode. A
        # completed cleanup invalidates its read cache; ToolNode must see the
        # assistant's tool-call message persisted in the backing store.
        assert await history.to_list() == [assistant]
        mgr.get_recent_messages.assert_called_once()


class TestToListCacheHit:
    """Consecutive to_list calls don't re-read from store."""

    async def test_consecutive_to_list_no_reread(self, noop_memory_system) -> None:
        mgr = _make_manager_mock(messages=[_msg()])
        history = ScopedMessageHistory(
            manager=mgr,
            context=_ctx(),
            memory_system=noop_memory_system,
            cleanup_config=_NO_CLEANUP,
        )
        # First to_list: cache is None → reads from store
        await history.to_list()
        assert mgr.get_recent_messages.call_count == 1
        # Second to_list: cache populated → no read
        await history.to_list()
        assert mgr.get_recent_messages.call_count == 1  # still 1


class TestCacheNoneFallback:
    """When cache is None, operations fall back to store reads."""

    async def test_cache_none_to_list_reads_db(self, noop_memory_system) -> None:
        mgr = _make_manager_mock(messages=[_msg(content="from-db")])
        history = ScopedMessageHistory(
            manager=mgr,
            context=_ctx(),
            memory_system=noop_memory_system,
            cleanup_config=_NO_CLEANUP,
        )
        # _cache starts as None (no initial_messages)
        assert history._cache is None
        msgs = await history.to_list()
        # Falls back to store read
        mgr.get_recent_messages.assert_called_once()
        assert len(msgs) == 1
        assert msgs[0].content == "from-db"

    async def test_cache_none_append_triggers_conservative(self, noop_memory_system) -> None:
        """_is_trigger_condition_met returns True when cache is None (conservative)."""
        mgr = _make_manager_mock()
        history = ScopedMessageHistory(
            manager=mgr,
            context=_ctx(),
            memory_system=noop_memory_system,
            cleanup_config=_NO_CLEANUP,
        )
        assert history._cache is None
        # Conservative: cache unavailable → trigger returns True
        assert history._is_trigger_condition_met() is True


class TestInitialMessagesPreserved:
    """initial_messages survive append (not cleared)."""

    async def test_initial_messages_survive_append(self, noop_memory_system) -> None:
        msg1 = _msg(content="first")
        msg2 = _msg(content="second")
        msg3 = _msg(content="third")
        mgr = _make_manager_mock()
        history = ScopedMessageHistory(
            manager=mgr,
            context=_ctx(),
            memory_system=noop_memory_system,
            initial_messages=[msg1, msg2],
            cleanup_config=_NO_CLEANUP,
        )
        await history.append(msg3)
        msgs = await history.to_list()
        assert len(msgs) == 3
        assert [m.content for m in msgs] == ["first", "second", "third"]


class TestClearReplaceAllInvalidate:
    """clear/replace_all invalidate cache → next to_list reads from store."""

    async def test_clear_invalidates_cache(self, noop_memory_system) -> None:
        msg1 = _msg(content="first")
        mgr = _make_manager_mock(messages=[])
        history = ScopedMessageHistory(
            manager=mgr,
            context=_ctx(),
            memory_system=noop_memory_system,
            initial_messages=[msg1],
            cleanup_config=_NO_CLEANUP,
        )
        # Verify cache is populated
        msgs = await history.to_list()
        assert len(msgs) == 1
        assert mgr.get_recent_messages.call_count == 0  # cache hit
        # Clear invalidates cache
        await history.clear()
        assert history._cache is None
        # Next to_list reads from store
        await history.to_list()
        assert mgr.get_recent_messages.call_count == 1

    async def test_replace_all_invalidates_cache(self, noop_memory_system) -> None:
        msg1 = _msg(content="first")
        msg2 = _msg(content="replaced")
        mgr = _make_manager_mock(messages=[msg2])
        history = ScopedMessageHistory(
            manager=mgr,
            context=_ctx(),
            memory_system=noop_memory_system,
            initial_messages=[msg1],
            cleanup_config=_NO_CLEANUP,
        )
        # Verify cache is populated
        msgs = await history.to_list()
        assert len(msgs) == 1
        assert mgr.get_recent_messages.call_count == 0  # cache hit
        # replace_all invalidates cache
        await history.replace_all([msg2])
        assert history._cache is None
        # Next to_list reads from store
        msgs = await history.to_list()
        assert mgr.get_recent_messages.call_count == 1
        assert msgs[0].content == "replaced"
