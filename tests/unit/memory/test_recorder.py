"""Tests for MemoryAppendRecorder."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from framework.memory.core.scope import MemoryContext
from framework.memory.core.message import ChatMessage
from framework.memory.recorder import MemoryAppendRecorder


class FakeProvider:
    """Fake MemoryProvider for testing."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.add = AsyncMock()
        self.shutdown = AsyncMock()


def make_context(agent_id: str | None = None) -> MemoryContext:
    return MemoryContext(session_id="s1", user_id="u1", agent_id=agent_id)


class TestMemoryAppendRecorderDedup:
    @pytest.mark.asyncio
    async def test_dedup_same_message(self):
        recorder = MemoryAppendRecorder([FakeProvider()])
        ctx = make_context()
        msg = {"role": "user", "content": "hello"}
        await recorder.record([msg], ctx)
        await recorder.record([msg], ctx)
        await recorder.flush()
        # provider.add called once despite two record() calls
        assert recorder._providers[0].add.call_count == 1

    @pytest.mark.asyncio
    async def test_different_messages_not_deduped(self):
        recorder = MemoryAppendRecorder([FakeProvider()])
        ctx = make_context()
        await recorder.record([{"role": "user", "content": "a"}], ctx)
        await recorder.record([{"role": "user", "content": "b"}], ctx)
        await recorder.flush()
        assert recorder._providers[0].add.call_count == 2

    @pytest.mark.asyncio
    async def test_metadata_excluded_from_hash(self):
        recorder = MemoryAppendRecorder([FakeProvider()])
        ctx = make_context()
        msg1 = {"role": "user", "content": "hello", "metadata": {"ts": 1}}
        msg2 = {"role": "user", "content": "hello", "metadata": {"ts": 2}}
        await recorder.record([msg1], ctx)
        await recorder.record([msg2], ctx)
        await recorder.flush()
        assert recorder._providers[0].add.call_count == 1


class TestMemoryAppendRecorderSubagent:
    @pytest.mark.asyncio
    async def test_subagent_skips_provider(self):
        provider = FakeProvider()
        recorder = MemoryAppendRecorder([provider])
        ctx = MemoryContext(session_id="s1", agent_id="subagent_a")
        await recorder.record([{"role": "user", "content": "hello"}], ctx)
        await recorder.flush()
        provider.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_subagent_skips_provider(self):
        provider = FakeProvider()
        recorder = MemoryAppendRecorder([provider])
        ctx = MemoryContext(session_id="s1", agent_id="subagent_1")
        await recorder.record([{"role": "user", "content": "hello"}], ctx)
        await recorder.flush()
        provider.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_main_agent_triggers_provider(self):
        provider = FakeProvider()
        recorder = MemoryAppendRecorder([provider])
        ctx = MemoryContext(session_id="s1", agent_id="main")
        await recorder.record([{"role": "user", "content": "hello"}], ctx)
        await recorder.flush()
        provider.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_agent_id_defaults_to_main(self):
        provider = FakeProvider()
        recorder = MemoryAppendRecorder([provider])
        ctx = MemoryContext(session_id="s1")
        await recorder.record([{"role": "user", "content": "hello"}], ctx)
        await recorder.flush()
        provider.add.assert_called_once()


class TestMemoryAppendRecorderFailureIsolation:
    @pytest.mark.asyncio
    async def test_provider_failure_does_not_break_others(self):
        good = FakeProvider("good")
        bad = FakeProvider("bad")
        bad.add.side_effect = RuntimeError("boom")
        recorder = MemoryAppendRecorder([good, bad])
        ctx = make_context()
        await recorder.record([{"role": "user", "content": "hello"}], ctx)
        await recorder.flush()
        good.add.assert_called_once()
        bad.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_flush_awaits_pending_tasks(self):
        provider = FakeProvider()
        # Make add slow so it is still pending when flush is called
        async def slow_add(msgs, ctx):
            await asyncio.sleep(0.05)
        provider.add = AsyncMock(side_effect=slow_add)
        recorder = MemoryAppendRecorder([provider])
        ctx = make_context()
        await recorder.record([{"role": "user", "content": "hello"}], ctx)
        # At this point the task may or may not be done
        await recorder.flush()
        # After flush, add must have been called
        provider.add.assert_called_once()


class TestMemoryAppendRecorderAddProvider:
    def test_add_provider_dynamic(self):
        recorder = MemoryAppendRecorder()
        assert recorder.providers == []
        provider = FakeProvider()
        recorder.add_provider(provider)
        assert recorder.providers == [provider]


class TestMemoryAppendRecorderChatMessageCompat:
    @pytest.mark.asyncio
    async def test_chat_message_converted_to_dict(self):
        provider = FakeProvider()
        recorder = MemoryAppendRecorder([provider])
        ctx = make_context()
        msg = ChatMessage(role="user", content="hello")
        await recorder.record([msg], ctx)
        await recorder.flush()
        call_args = provider.add.call_args
        assert call_args is not None
        sent_messages = call_args[0][0]
        assert all(isinstance(m, dict) for m in sent_messages)
