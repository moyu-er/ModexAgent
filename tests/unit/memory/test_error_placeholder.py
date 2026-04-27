"""Tests for error placeholder and pending user turn memory recovery (P0-a 11.5)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.memory.core.message import ChatMessage


class TestPendingUserTurn:
    """DefaultMemorySystem pending user turn tracking."""

    @pytest.fixture
    def mock_registry(self):
        registry = MagicMock()
        storage = MagicMock()
        storage.set = AsyncMock()
        storage.delete = AsyncMock()
        storage.get = AsyncMock(return_value=None)
        registry.resolve = AsyncMock(return_value=storage)
        return registry

    @pytest.fixture
    def memory_system(self, mock_registry):
        from framework.memory.default_system import DefaultMemorySystem
        from framework.memory.core.layers import MemoryLayerSet

        layers = MagicMock(spec=MemoryLayerSet)
        return DefaultMemorySystem(
            layer_set=layers,
            store_registry=mock_registry,
        )

    @pytest.mark.asyncio
    async def test_set_pending_user_turn(self, memory_system, mock_registry):
        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="sess-1")
        await memory_system.set_pending_user_turn(ctx, "msg-1", 1234567890.0)

        storage = await mock_registry.resolve()
        storage.set.assert_called_once()
        call_args = storage.set.call_args[0]
        assert call_args[0] == ".pending_user_turn"
        assert call_args[1]["message_id"] == "msg-1"
        assert call_args[1]["session_id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_clear_pending_user_turn(self, memory_system, mock_registry):
        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="sess-1")
        await memory_system.clear_pending_user_turn(ctx)

        storage = await mock_registry.resolve()
        storage.delete.assert_called_once_with(".pending_user_turn")

    @pytest.mark.asyncio
    async def test_get_pending_user_turn_returns_none_when_absent(self, memory_system, mock_registry):
        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="sess-1")
        result = await memory_system.get_pending_user_turn(ctx)

        assert result is None
        storage = await mock_registry.resolve()
        storage.get.assert_called_once_with(".pending_user_turn")

    @pytest.mark.asyncio
    async def test_get_pending_user_turn_returns_dict_when_present(self, memory_system, mock_registry):
        from framework.memory.core.scope import MemoryContext

        storage = await mock_registry.resolve()
        storage.get.return_value = {"message_id": "msg-1", "session_id": "sess-1"}

        ctx = MemoryContext(session_id="sess-1")
        result = await memory_system.get_pending_user_turn(ctx)

        assert result is not None
        assert result["message_id"] == "msg-1"


class TestAddAssistantPlaceholder:
    """MemorySystemContextManager.add_assistant_placeholder() error recovery."""

    @pytest.fixture
    def mock_memory_system(self):
        ms = MagicMock()
        ms.get_history = AsyncMock(return_value=[])
        ms.add_messages = AsyncMock()
        ms.clear_pending_user_turn = AsyncMock()
        return ms

    @pytest.fixture
    def ctx_manager(self, mock_memory_system):
        from framework.memory.system import MemorySystemContextManager

        return MemorySystemContextManager(
            memory_system=mock_memory_system,
            default_user_id="user-1",
        )

    @pytest.mark.asyncio
    async def test_writes_placeholder_when_last_is_user(self, ctx_manager, mock_memory_system):
        user_msg = ChatMessage.coerce({"role": "user", "content": "hello"})
        mock_memory_system.get_history.return_value = [user_msg]

        await ctx_manager.add_assistant_placeholder("sess-1", "LLM timeout")

        mock_memory_system.add_messages.assert_called_once()
        msgs = mock_memory_system.add_messages.call_args[0][1]
        assert len(msgs) == 1
        placeholder = msgs[0]
        assert placeholder.role == "assistant"
        assert "Assistant reply unavailable" in placeholder.content
        assert placeholder.metadata.get("is_error_placeholder") is True

    @pytest.mark.asyncio
    async def test_skips_when_last_is_not_user(self, ctx_manager, mock_memory_system):
        assistant_msg = ChatMessage.coerce({"role": "assistant", "content": "I already replied"})
        mock_memory_system.get_history.return_value = [assistant_msg]

        await ctx_manager.add_assistant_placeholder("sess-1", "LLM timeout")

        mock_memory_system.add_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_history_is_empty(self, ctx_manager, mock_memory_system):
        mock_memory_system.get_history.return_value = []

        await ctx_manager.add_assistant_placeholder("sess-1", "LLM timeout")

        mock_memory_system.add_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_clears_pending_user_turn_on_success(self, ctx_manager, mock_memory_system):
        user_msg = ChatMessage.coerce({"role": "user", "content": "hello"})
        mock_memory_system.get_history.return_value = [user_msg]

        await ctx_manager.add_assistant_placeholder("sess-1", "LLM timeout")

        mock_memory_system.clear_pending_user_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_early_return_when_get_history_fails(self, ctx_manager, mock_memory_system):
        """When get_history fails, returns early without writing or clearing."""
        mock_memory_system.get_history.side_effect = RuntimeError("storage down")

        await ctx_manager.add_assistant_placeholder("sess-1", "LLM timeout")

        # Early return before both add_messages and clear_pending_user_turn
        mock_memory_system.add_messages.assert_not_called()
        mock_memory_system.clear_pending_user_turn.assert_not_called()
