"""Tests for MemorySystem + MemoryAppendRecorder integration."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryContext, SessionScope
from framework.memory.stores.in_memory import InMemoryStorage
from framework.memory.system import MemorySystem, LayerConfig


class FakeProvider:
    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.add = AsyncMock()
        self.search = AsyncMock(return_value=[])
        self.prefetch = AsyncMock(return_value=None)
        self.shutdown = AsyncMock()
        self.system_prompt_block = AsyncMock(return_value="")
        self.on_pre_compress = AsyncMock()


@pytest.fixture
def system() -> MemorySystem:
    store = InMemoryStorage()
    return MemorySystem(
        workspace=None,
        layers={
            "short_term": LayerConfig(
                scope=SessionScope(),
                storage=store,
            )
        },
    )


class TestMemorySystemRecorderIntegration:
    @pytest.mark.asyncio
    async def test_add_messages_triggers_provider(self, system):
        provider = FakeProvider()
        system.add_provider(provider)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        await system.add_messages(ctx, [{"role": "user", "content": "hello"}])
        await system._recorder.flush()
        provider.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_message_history_append_triggers_provider(self, system):
        provider = FakeProvider()
        system.add_provider(provider)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        history = system.create_message_history(ctx)
        await history.append(ChatMessage(role="assistant", content="hi"))
        await system._recorder.flush()
        provider.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_peer_scope_skips_provider(self, system):
        provider = FakeProvider()
        system.add_provider(provider)
        ctx = MemoryContext(session_id="s1", user_id="u1", agent_id="peer_a")
        await system.add_messages(ctx, [{"role": "user", "content": "hello"}])
        await system._recorder.flush()
        provider.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_subagent_scope_skips_provider(self, system):
        provider = FakeProvider()
        system.add_provider(provider)
        ctx = MemoryContext(session_id="s1", user_id="u1", agent_id="subagent_1")
        await system.add_messages(ctx, [{"role": "user", "content": "hello"}])
        await system._recorder.flush()
        provider.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_flushes_recorder(self, system):
        provider = FakeProvider()
        system.add_provider(provider)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        await system.add_messages(ctx, [{"role": "user", "content": "hello"}])
        await system.close()
        provider.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_provider_failure_does_not_break_short_term(self, system):
        provider = FakeProvider()
        provider.add.side_effect = RuntimeError("boom")
        system.add_provider(provider)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        # Should not raise
        await system.add_messages(ctx, [{"role": "user", "content": "hello"}])
        await system._recorder.flush()
        # Short-term messages should still be saved
        msgs = await system.get_history(ctx)
        assert len(msgs) == 1
        assert msgs[0].content == "hello"

    @pytest.mark.asyncio
    async def test_replace_all_does_not_trigger_provider(self, system):
        provider = FakeProvider()
        system.add_provider(provider)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        history = system.create_message_history(ctx)
        await history.replace_all([{"role": "user", "content": "x"}])
        await system._recorder.flush()
        provider.add.assert_not_called()


class TestMemorySystemRecorderDedup:
    @pytest.mark.asyncio
    async def test_same_message_not_added_twice(self, system):
        provider = FakeProvider()
        system.add_provider(provider)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        msg = {"role": "user", "content": "hello"}
        await system.add_messages(ctx, [msg])
        await system.add_messages(ctx, [msg])
        await system._recorder.flush()
        # Only one provider add despite two MemorySystem.add_messages calls
        assert provider.add.call_count == 1
