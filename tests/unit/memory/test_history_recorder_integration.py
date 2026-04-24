"""Tests for ShortTermMessageHistory + MemoryAppendRecorder integration."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from framework.memory.core.scope import MemoryContext
from framework.memory.core.message import ChatMessage
from framework.memory.history import ShortTermMessageHistory


class FakeManager:
    """Minimal fake for ShortTermMemoryManager."""

    def __init__(self) -> None:
        self.add_message = AsyncMock()
        self.add_messages = AsyncMock()
        self.clear_messages = AsyncMock()
        self.replace_all_messages = AsyncMock()
        self.get_messages = AsyncMock(return_value=[])


class FakeRecorder:
    def __init__(self) -> None:
        self.record = AsyncMock()
        self.flush = AsyncMock()


@pytest.fixture
def ctx() -> MemoryContext:
    return MemoryContext(session_id="s1", user_id="u1")


@pytest.fixture
def manager() -> FakeManager:
    return FakeManager()


@pytest.fixture
def recorder() -> FakeRecorder:
    return FakeRecorder()


class TestShortTermMessageHistoryRecorder:
    @pytest.mark.asyncio
    async def test_append_triggers_recorder(self, ctx, manager, recorder):
        history = ShortTermMessageHistory(
            manager=manager,  # type: ignore[arg-type]
            context=ctx,
            recorder=recorder,  # type: ignore[arg-type]
        )
        msg = ChatMessage(role="assistant", content="hi")
        await history.append(msg)
        manager.add_message.assert_called_once()
        recorder.record.assert_called_once()

    @pytest.mark.asyncio
    async def test_extend_triggers_recorder(self, ctx, manager, recorder):
        history = ShortTermMessageHistory(
            manager=manager,  # type: ignore[arg-type]
            context=ctx,
            recorder=recorder,  # type: ignore[arg-type]
        )
        msgs = [ChatMessage(role="assistant", content="a"), ChatMessage(role="tool", content="r")]
        await history.extend(msgs)
        manager.add_messages.assert_called_once()
        recorder.record.assert_called_once()
        assert len(recorder.record.call_args[0][0]) == 2

    @pytest.mark.asyncio
    async def test_replace_all_does_not_trigger_recorder(self, ctx, manager, recorder):
        history = ShortTermMessageHistory(
            manager=manager,  # type: ignore[arg-type]
            context=ctx,
            recorder=recorder,  # type: ignore[arg-type]
        )
        await history.replace_all([{"role": "user", "content": "x"}])
        manager.replace_all_messages.assert_called_once()
        recorder.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_recorder_does_not_crash(self, ctx, manager):
        history = ShortTermMessageHistory(
            manager=manager,  # type: ignore[arg-type]
            context=ctx,
            recorder=None,
        )
        msg = ChatMessage(role="user", content="hello")
        await history.append(msg)
        manager.add_message.assert_called_once()
