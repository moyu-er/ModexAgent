"""Tests for checkpoint recovery deduplication (P1 Step 15.3)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.memory.core.message import ChatMessage
from framework.memory.system import MemorySystemContextManager


class TestCheckpointDedup:
    """Checkpoint recovery with dedup via checkpoint_id and overlap."""

    @pytest.fixture
    def mock_memory_system(self):
        ms = MagicMock()
        ms.get_checkpoint_id = AsyncMock(return_value=None)
        ms.get_last_recovered_checkpoint_id = AsyncMock(return_value=None)
        ms.load_checkpoint = AsyncMock(return_value=None)
        ms.add_messages = AsyncMock()
        ms.set_last_recovered_checkpoint_id = AsyncMock()
        ms.clear_checkpoint = AsyncMock()
        ms.get_history = AsyncMock(return_value=[])
        return ms

    @pytest.fixture
    def ctx_manager(self, mock_memory_system):
        return MemorySystemContextManager(
            memory_system=mock_memory_system,
            default_user_id="user-1",
        )

    @pytest.mark.asyncio
    async def test_no_checkpoint_returns_none(self, ctx_manager, mock_memory_system):
        result, was_recovered = await ctx_manager.recover_checkpoint("sess-1")

        assert result is None
        assert was_recovered is False
        mock_memory_system.add_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovers_when_checkpoint_exists(self, ctx_manager, mock_memory_system):
        mock_memory_system.get_checkpoint_id.return_value = "ckpt-abc"
        recovered = [
            ChatMessage.coerce({"role": "user", "content": "hello"}),
            ChatMessage.coerce({"role": "assistant", "content": "hi"}),
        ]
        mock_memory_system.load_checkpoint.return_value = recovered

        result, was_recovered = await ctx_manager.recover_checkpoint("sess-1")

        assert result == recovered
        assert was_recovered is True
        mock_memory_system.add_messages.assert_called_once()
        mock_memory_system.set_last_recovered_checkpoint_id.assert_called_once()
        mock_memory_system.clear_checkpoint.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_already_recovered(self, ctx_manager, mock_memory_system):
        mock_memory_system.get_checkpoint_id.return_value = "ckpt-abc"
        mock_memory_system.get_last_recovered_checkpoint_id.return_value = "ckpt-abc"

        result, was_recovered = await ctx_manager.recover_checkpoint("sess-1")

        assert result is None
        assert was_recovered is False
        mock_memory_system.load_checkpoint.assert_not_called()
        mock_memory_system.add_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_messages_already_in_history(self, ctx_manager, mock_memory_system):
        mock_memory_system.get_checkpoint_id.return_value = "ckpt-abc"
        recovered = [
            ChatMessage.coerce({"role": "user", "content": "hello"}),
        ]
        mock_memory_system.load_checkpoint.return_value = recovered
        # History already contains the same message
        mock_memory_system.get_history.return_value = [
            ChatMessage.coerce({"role": "user", "content": "hello"}),
        ]

        result, was_recovered = await ctx_manager.recover_checkpoint("sess-1")

        assert result is None
        assert was_recovered is False
        mock_memory_system.add_messages.assert_not_called()
        # Should still mark as recovered and clear checkpoint
        mock_memory_system.set_last_recovered_checkpoint_id.assert_called_once()
        mock_memory_system.clear_checkpoint.assert_called_once()

    @pytest.mark.asyncio
    async def test_recovers_when_history_is_different(self, ctx_manager, mock_memory_system):
        mock_memory_system.get_checkpoint_id.return_value = "ckpt-abc"
        recovered = [
            ChatMessage.coerce({"role": "user", "content": "hello"}),
        ]
        mock_memory_system.load_checkpoint.return_value = recovered
        mock_memory_system.get_history.return_value = [
            ChatMessage.coerce({"role": "user", "content": "different"}),
        ]

        result, was_recovered = await ctx_manager.recover_checkpoint("sess-1")

        assert result == recovered
        assert was_recovered is True
        mock_memory_system.add_messages.assert_called_once()
