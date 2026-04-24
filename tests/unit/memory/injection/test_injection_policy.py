"""Tests for MemoryInjectionPolicy implementations."""

import tempfile
from pathlib import Path

import pytest

from framework.memory.archive import PreserveSummaryArchiveStrategy
from framework.memory.core.scope import MemoryContext
from framework.memory.injection import DefaultMemoryInjectionPolicy
from framework.memory.system import MemorySystem, MemorySystemContextManager


@pytest.fixture
async def memory_system():
    with tempfile.TemporaryDirectory() as tmp:
        ms = MemorySystem(workspace=Path(tmp))
        await ms.initialize()
        yield ms
        await ms.close()


@pytest.mark.asyncio
class TestDefaultMemoryInjectionPolicy:
    async def test_assemble_returns_context_state(self, memory_system):
        policy = DefaultMemoryInjectionPolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        state = await policy.assemble(memory_system, ctx, "Base prompt")
        assert state.system_prompt == "Base prompt"
        assert len(await state.history.to_list()) == 0

    async def test_history_is_short_term(self, memory_system):
        policy = DefaultMemoryInjectionPolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1")

        # short_term messages
        await memory_system.add_message(ctx, {"role": "user", "content": "short1"})
        await memory_system.add_message(ctx, {"role": "assistant", "content": "short2"})

        state = await policy.assemble(memory_system, ctx)
        history = state.history
        assert len(await history.to_list()) == 2
        assert (await history.to_list())[0]["content"] == "short1"
        assert (await history.to_list())[1]["content"] == "short2"

    async def test_respects_max_short_term_messages(self, memory_system):
        policy = DefaultMemoryInjectionPolicy(max_short_term_messages=3)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        for i in range(10):
            await memory_system.add_message(ctx, {"role": "user", "content": str(i)})

        state = await policy.assemble(memory_system, ctx)
        history = state.history
        # max_short_term_messages=3 limits get_history, so assembled history has at most 3
        assert len(await history.to_list()) == 3
        assert (await history.to_list())[0]["content"] == "7"
        assert (await history.to_list())[1]["content"] == "8"
        assert (await history.to_list())[2]["content"] == "9"

    async def test_system_prompt_includes_long_term_and_base(self, memory_system):
        policy = DefaultMemoryInjectionPolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        await memory_system._managers.long_term.update(ctx, {"soul": "friendly"})

        state = await policy.assemble(memory_system, ctx, "Base prompt")
        assert "Base prompt" in state.system_prompt
        assert "friendly" in state.system_prompt

    async def test_empty_system_prompt_when_no_base_and_no_memory(self, memory_system):
        policy = DefaultMemoryInjectionPolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        state = await policy.assemble(memory_system, ctx)
        assert state.system_prompt == ""

    async def test_adapter_load_uses_policy(self, memory_system):
        policy = DefaultMemoryInjectionPolicy()
        adapter = MemorySystemContextManager(memory_system, injection_policy=policy)
        ctx = MemoryContext(session_id="s1", user_id="u1")

        await memory_system.add_message(ctx, {"role": "user", "content": "old"})
        await memory_system.add_message(ctx, {"role": "user", "content": "new"})

        state = await adapter.load("s1")
        history = state.history
        assert len(await history.to_list()) == 2
        assert (await history.to_list())[0]["content"] == "old"
        assert (await history.to_list())[1]["content"] == "new"

    async def test_compression_summary_in_system_prompt_not_history(self, memory_system):
        """压缩摘要应出现在 system_prompt 中，而不是 history 消息里。"""
        from framework.memory.compaction.pipeline import (
            ConsolidatorSummaryStrategy,
            MemoryCompactionPipeline,
        )
        from framework.memory.core.compression import CompressionResult, CompressionStrategy

        class FakeCompressor(CompressionStrategy):
            async def compress(self, messages, context):
                pruned = messages[:-1]
                remaining = messages[-1:]
                return CompressionResult(
                    summary="Summary of old messages",
                    pruned_messages=pruned,
                    remaining_messages=remaining,
                )

        # 覆盖 short_term 配置以强制触发压缩
        archive = memory_system.layers["short_term"].archive_strategy or PreserveSummaryArchiveStrategy()
        pipeline = MemoryCompactionPipeline(
            summary_strategy=ConsolidatorSummaryStrategy(FakeCompressor()),
            archive_strategy=archive,
        )
        memory_system.layers["short_term"].pipeline = pipeline
        memory_system.layers["short_term"].archive_strategy = archive
        memory_system.layers["short_term"].max_messages = 3
        memory_system.layers["short_term"].max_tokens = None
        # 重建 manager 使配置生效
        memory_system._managers = memory_system._build_managers()

        ctx = MemoryContext(session_id="s_compress", user_id="u1")
        for i in range(6):
            await memory_system.add_message(ctx, {"role": "user", "content": str(i)})

        # 通过 policy 组装上下文
        policy = DefaultMemoryInjectionPolicy()
        state = await policy.assemble(memory_system, ctx, base_system_prompt="Base prompt")

        # system_prompt 应包含压缩摘要
        assert "Summary of old messages" in state.system_prompt
        # history 中不应出现 system 消息
        history = state.history
        for msg in (await history.to_list()):
            assert msg.get("role") != "system"
