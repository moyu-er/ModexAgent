"""Tests for MemorySystem and MemorySystemContextManager."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from framework.core.emitter import AgentResult
from framework.memory.core.scope import MemoryContext
from framework.memory.system import (
    MemorySystem,
    MemorySystemContextManager,
    _derive_memory_budget,
)
from framework.session.agent_session import AgentSession


@pytest.fixture
async def memory_system():
    with tempfile.TemporaryDirectory() as tmp:
        ms = MemorySystem(workspace=Path(tmp))
        await ms.initialize()
        yield ms
        await ms.close()


@pytest.mark.asyncio
class TestMemorySystem:
    async def test_default_single_user_layers(self, memory_system):
        assert "working" in memory_system.layers
        assert "short_term" in memory_system.layers
        assert "history" in memory_system.layers
        assert "long_term" in memory_system.layers

    async def test_add_and_get_history(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        await memory_system.add_message(ctx, {"role": "user", "content": "hi"})
        history = await memory_system.get_history(ctx)
        assert len(history) == 1
        assert history[0]["content"] == "hi"

    async def test_get_history_respects_max_messages(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        for i in range(60):
            await memory_system.add_message(ctx, {"role": "user", "content": str(i)})
        history = await memory_system.get_history(ctx, max_messages=50)
        assert len(history) == 50
        assert history[0]["content"] == "10"
        assert history[-1]["content"] == "59"

    async def test_system_prompt_with_long_term(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        await memory_system._managers.long_term.update(ctx, {"soul": " concise"})
        prompt = await memory_system.build_system_prompt(ctx)
        assert "concise" in prompt

    async def test_system_prompt_includes_history(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        await memory_system._managers.history.append(ctx, "SUMMARY_A", {"pruned_count": 1})
        await memory_system._managers.history.append(ctx, "SUMMARY_B", {"pruned_count": 1})
        prompt = await memory_system.build_system_prompt(ctx)
        assert "SUMMARY_A" in prompt
        assert "SUMMARY_B" in prompt

    async def test_system_prompt_respects_max_history_entries(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        for i in range(10):
            await memory_system._managers.history.append(ctx, f"SUMMARY_{i}", {})
        prompt = await memory_system.build_system_prompt(ctx, max_history_entries=3)
        assert "SUMMARY_9" in prompt
        assert "SUMMARY_8" in prompt
        assert "SUMMARY_7" in prompt
        assert "SUMMARY_6" not in prompt


@pytest.mark.asyncio
class TestMemorySystemContextManager:
    async def test_load_returns_empty_system_prompt(self, memory_system):
        adapter = MemorySystemContextManager(memory_system)
        state = await adapter.load("s1")
        assert state.system_prompt == ""

    async def test_save_adds_messages(self, memory_system):
        adapter = MemorySystemContextManager(memory_system)
        user_msg = {"role": "user", "content": "hello"}
        result = AgentResult(content="hi", messages=[{"role": "assistant", "content": "hi"}])
        await adapter.save("s1", user_msg, result)

        state = await adapter.load("s1")
        assert len(state.history) == 2
        assert state.history[0]["content"] == "hello"
        assert state.history[1]["content"] == "hi"

    async def test_build_system_prompt_uses_runtime_session_id(self, memory_system):
        adapter = MemorySystemContextManager(memory_system)
        ctx = MemoryContext(session_id="s1", user_id="default")
        await memory_system._managers.long_term.update(ctx, {"soul": "friendly"})

        prompt = await adapter.build_system_prompt(
            tool_manager=None,
            runtime_info={"session_id": "s1"},
        )
        assert "friendly" in prompt

    async def test_save_uses_user_id_from_metadata(self, memory_system):
        # 临时降低 max_messages 以触发压缩，从而验证 history 的 user_id 隔离
        stm = memory_system._managers.short_term
        original_max = stm._config.max_messages
        stm._config.max_messages = 1

        adapter = MemorySystemContextManager(memory_system)
        await adapter.save(
            "s1",
            {"role": "user", "content": "hello"},
            AgentResult(),  # No messages — test is about user_id scoping
            metadata={"input_metadata": {"user_id": "u42"}},
        )
        # 再保存一条消息以触发压缩（save() 直接写入 short_term）
        await adapter.save(
            "s1",
            {"role": "user", "content": "hello2"},
            AgentResult(content="hi2"),
            metadata={"input_metadata": {"user_id": "u42"}},
        )
        # history is scoped by user_id, so we should see the entry under u42
        ctx = MemoryContext(session_id="s1", user_id="u42")
        _, entries = await memory_system._managers.history.get_unprocessed(ctx)
        assert len(entries) == 1
        assert "hello" in entries[0]["summary"].lower()

        # 同时验证默认用户看不到这条记录
        ctx_default = MemoryContext(session_id="s1", user_id="default")
        _, entries_default = await memory_system._managers.history.get_unprocessed(ctx_default)
        assert len(entries_default) == 0

        stm._config.max_messages = original_max

    async def test_build_system_prompt_uses_user_id_from_runtime_info(self, memory_system):
        adapter = MemorySystemContextManager(memory_system)
        # long_term for user u99
        ctx = MemoryContext(session_id="s1", user_id="u99")
        await memory_system._managers.long_term.update(ctx, {"soul": "witty"})

        prompt = await adapter.build_system_prompt(
            tool_manager=None,
            runtime_info={"session_id": "s1", "user_id": "u99"},
        )
        assert "witty" in prompt

    async def test_load_uses_cached_user_id_from_prior_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            from framework.memory.core.scope import UserScope
            from framework.memory.stores.in_memory import InMemoryStorage
            from framework.memory.system import LayerConfig

            store = InMemoryStorage()
            layers = {
                "working": LayerConfig(scope=UserScope(), storage=InMemoryStorage()),
                "short_term": LayerConfig(scope=UserScope(), storage=store),
                "history": LayerConfig(scope=UserScope(), storage=store),
                "long_term": LayerConfig(scope=UserScope(), storage=store),
            }
            ms = MemorySystem(workspace=Path(tmp), layers=layers)
            await ms.initialize()

            adapter = MemorySystemContextManager(ms)
            await adapter.save(
                "s1",
                {"role": "user", "content": "hello"},
                AgentResult(),  # No messages — test is about user_id caching
                metadata={"input_metadata": {"user_id": "alice"}},
            )
            state = await adapter.load("s1")
            assert len(state.history) == 1
            await ms.close()

    async def test_load_uses_user_id_from_runtime_info_when_no_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            from framework.memory.core.scope import UserScope
            from framework.memory.stores.in_memory import InMemoryStorage
            from framework.memory.system import LayerConfig

            store = InMemoryStorage()
            layers = {
                "working": LayerConfig(scope=UserScope(), storage=InMemoryStorage()),
                "short_term": LayerConfig(scope=UserScope(), storage=store),
                "history": LayerConfig(scope=UserScope(), storage=store),
                "long_term": LayerConfig(scope=UserScope(), storage=store),
            }
            ms = MemorySystem(workspace=Path(tmp), layers=layers)
            await ms.initialize()
            adapter = MemorySystemContextManager(ms)

            ctx = MemoryContext(session_id="s1", user_id="alice")
            await ms.add_message(ctx, {"role": "user", "content": "hello"})
            await ms.add_message(ctx, {"role": "assistant", "content": "hi"})

            await adapter.build_system_prompt(
                tool_manager=None,
                runtime_info={"session_id": "s1", "user_id": "alice"},
            )
            state = await adapter.load("s1")
            assert len(state.history) == 2
            await ms.close()

    async def test_clear_uses_cached_user_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            from framework.memory.core.scope import UserScope
            from framework.memory.stores.in_memory import InMemoryStorage
            from framework.memory.system import LayerConfig

            store = InMemoryStorage()
            layers = {
                "working": LayerConfig(scope=UserScope(), storage=InMemoryStorage()),
                "short_term": LayerConfig(scope=UserScope(), storage=store),
                "history": LayerConfig(scope=UserScope(), storage=store),
                "long_term": LayerConfig(scope=UserScope(), storage=store),
            }
            ms = MemorySystem(workspace=Path(tmp), layers=layers)
            await ms.initialize()
            adapter = MemorySystemContextManager(ms)

            await adapter.save(
                "s1",
                {"role": "user", "content": "x"},
                AgentResult(content="y", messages=[{"role": "assistant", "content": "y"}]),
                metadata={"input_metadata": {"user_id": "bob"}},
            )
            await adapter.clear("s1")
            state = await adapter.load("s1")
            assert len(state.history) == 0
            await ms.close()

    async def test_clear_legacy(self, memory_system):
        adapter = MemorySystemContextManager(memory_system)
        await adapter.save("s1", {"role": "user", "content": "x"}, AgentResult(content="y"))
        await adapter.clear("s1")
        state = await adapter.load("s1")
        assert len(state.history) == 0

    async def test_save_only_persists_user_message(self, memory_system):
        adapter = MemorySystemContextManager(memory_system)
        await adapter.save(
            "s1",
            {"role": "user", "content": "hello"},
            AgentResult(content="fallback reply", messages=[]),
        )
        state = await adapter.load("s1")
        history = state.history
        assert len(history) == 1
        assert history[0]["content"] == "hello"

    async def test_default_layers_have_no_compression_strategy(self, memory_system):
        """默认配置不应设置 compression_strategy，避免与 max_tokens 双重裁剪。"""
        assert memory_system.layers["short_term"].compression_strategy is None

    async def test_context_cache_has_size_limit(self, memory_system):
        """_context_cache 应有上限，防止长期运行内存无限增长。"""
        adapter = MemorySystemContextManager(memory_system)
        for i in range(1001):
            await adapter.save(
                f"s{i}",
                {"role": "user", "content": "hi"},
                AgentResult(content="hello"),
            )
        assert len(adapter._context_cache) <= 1000

    async def test_system_prompt_includes_all_non_empty_summaries(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        await memory_system._managers.history.append(ctx, "[Auto Archive] 3 messages pruned", {})
        await memory_system._managers.history.append(ctx, "Meaningful summary here", {})
        prompt = await memory_system.build_system_prompt(ctx)
        assert "Meaningful summary here" in prompt
        assert "[Auto Archive] 3 messages pruned" in prompt

    async def test_add_messages_batches_and_compresses_once(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        # 降低限制使压缩容易触发
        stm = memory_system._managers.short_term
        original_max = stm._config.max_messages
        stm._config.max_messages = 3

        # 先写入 2 条，留 1 条余量
        await memory_system.add_message(ctx, {"role": "user", "content": "a"})
        await memory_system.add_message(ctx, {"role": "assistant", "content": "b"})

        # 批量追加 3 条，应只触发一次压缩，结果保留最近 3 条
        await memory_system.add_messages(ctx, [
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "d"},
            {"role": "user", "content": "e"},
        ])

        history = await memory_system.get_history(ctx)
        assert len(history) == 3
        assert history[0]["content"] == "c"
        assert history[1]["content"] == "d"
        assert history[2]["content"] == "e"

        stm._config.max_messages = original_max


@pytest.mark.asyncio
class TestMemorySystemConfiguration:
    async def test_custom_max_messages_and_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            from framework.memory.core.scope import SessionScope
            from framework.memory.stores.in_memory import InMemoryStorage
            from framework.memory.system import LayerConfig

            file_store = InMemoryStorage()
            layers = {
                "working": LayerConfig(scope=SessionScope(), storage=InMemoryStorage()),
                "short_term": LayerConfig(
                    scope=SessionScope(),
                    storage=file_store,
                    max_messages=3,
                    max_tokens=500,
                ),
                "history": LayerConfig(scope=SessionScope(), storage=file_store),
                "long_term": LayerConfig(scope=SessionScope(), storage=file_store),
            }
            ms = MemorySystem(workspace=Path(tmp), layers=layers)
            await ms.initialize()

            stm = ms._managers.short_term
            assert stm._config.max_messages == 3
            assert stm._config.max_tokens == 500

            await ms.close()

    async def test_end_to_end_tool_chain_integrity_after_compression(self):
        """模拟 QQ Bot 第二轮触发压缩后 tool-call 链保持完整。"""
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()
            ctx = MemoryContext(session_id="qq_session", user_id="u1")

            # 构建包含 tool call 的会话历史
            base_messages = [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
            for m in base_messages:
                await ms.add_message(ctx, m)

            # 添加一条 tool-call 链
            await ms.add_message(
                ctx,
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call_1", "function": {"name": "lookup"}}],
                },
            )
            await ms.add_message(
                ctx, {"role": "tool", "tool_call_id": "call_1", "content": "result_a"}
            )

            # 添加更多消息直到触发默认 max_messages=100 或 max_tokens=8000
            # 为了快速触发，直接操作 short_term storage 缩短限制
            stm = ms._managers.short_term
            original_max = stm._config.max_messages
            stm._config.max_messages = 3  # 强制触发压缩

            await ms.add_message(ctx, {"role": "user", "content": "overflow msg"})

            history = await ms.get_history(ctx)

            # 验证没有孤儿 tool result
            valid_call_ids = {
                tc.get("id")
                for m in history
                if m.get("role") == "assistant" and m.get("tool_calls")
                for tc in m["tool_calls"]
            }
            for m in history:
                if m.get("role") == "tool":
                    assert (
                        m.get("tool_call_id") in valid_call_ids
                    ), f"orphan tool result: {m}"

            # 恢复
            stm._config.max_messages = original_max
            await ms.close()


@pytest.mark.asyncio
class TestAgentSessionWithMemorySystem:
    async def test_accepts_memory_system(self, memory_system):
        mock_agent = MagicMock()
        mock_tool_manager = MagicMock()
        session = AgentSession(
            agent=mock_agent,
            tool_manager=mock_tool_manager,
            memory_system=memory_system,
        )
        assert session.context_manager is not None


@pytest.mark.asyncio
class TestMemorySystemReadAPIs:
    async def test_get_working_messages(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        memory_system.stage_working(ctx, [{"role": "user", "content": "w1"}])
        msgs = memory_system.get_working_messages(ctx)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "w1"

    async def test_get_history_entries(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        await memory_system._managers.history.append(ctx, "summary1", {})
        await memory_system._managers.history.append(ctx, "summary2", {})
        entries = await memory_system.get_history_entries(ctx, limit=1)
        assert len(entries) == 1
        assert entries[0]["summary"] == "summary2"

    async def test_get_long_term(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        await memory_system._managers.long_term.update(ctx, {"soul": "witty", "user": "developer"})
        lt = await memory_system.get_long_term(ctx)
        assert lt.soul == "witty"
        assert lt.user == "developer"

    async def test_clear_working(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        memory_system.stage_working(ctx, [{"role": "user", "content": "x"}])
        removed = memory_system.clear_working(ctx)
        assert len(removed) == 1
        assert memory_system.get_working_messages(ctx) == []

    async def test_flush_working_moves_to_short_term(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        memory_system.stage_working(ctx, [{"role": "user", "content": "to_flush"}])
        flushed = await memory_system.flush_working(ctx)
        assert len(flushed) == 1
        assert memory_system.get_working_messages(ctx) == []

        short_term = await memory_system.get_history(ctx)
        assert len(short_term) == 1
        assert short_term[0]["content"] == "to_flush"

    async def test_add_messages_only_writes_short_term(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        await memory_system.add_messages(ctx, [{"role": "user", "content": "only_short"}])
        assert memory_system.get_working_messages(ctx) == []
        short_term = await memory_system.get_history(ctx)
        assert len(short_term) == 1

    async def test_save_writes_directly_to_short_term(self, memory_system):
        adapter = MemorySystemContextManager(memory_system)
        await adapter.save(
            "s1",
            {"role": "user", "content": "hello"},
            AgentResult(content="hi"),
        )
        ctx = MemoryContext(session_id="s1", user_id="default")
        # save() 直接写入 short_term，不会 stage 到 working
        working = memory_system.get_working_messages(ctx)
        assert len(working) == 0
        history = await memory_system.get_history(ctx)
        assert len(history) == 1
        # flush() 不会影响已持久化的消息
        await adapter.flush("s1")
        assert memory_system.get_working_messages(ctx) == []
        history_after = await memory_system.get_history(ctx)
        assert len(history_after) == 1

    async def test_get_unprocessed_history_count(self, memory_system):
        ctx = MemoryContext(session_id="s1", user_id="u1")
        await memory_system._managers.history.append(ctx, "s1", {})
        await memory_system._managers.history.append(ctx, "s2", {})
        count = await memory_system.get_unprocessed_history_count(ctx)
        assert count == 2

    async def test_default_single_user_layers_injects_consolidator_when_llm_provided(self):
        mock_llm = MagicMock()
        layers = MemorySystem.default_single_user_layers(llm_provider=mock_llm)
        assert layers["short_term"].compression_strategy is not None
        assert layers["short_term"].archive_strategy is not None

    async def test_default_single_user_layers_no_compression_without_llm(self):
        layers = MemorySystem.default_single_user_layers()
        assert layers["short_term"].compression_strategy is None
        assert layers["short_term"].archive_strategy is None


class TestDeriveMemoryBudget:
    def test_none_fallback(self):
        assert _derive_memory_budget(None) == (100, 8000)

    def test_normal_scaling(self):
        assert _derive_memory_budget(10000, budget_ratio=0.5) == (100, 5000)

    def test_fractional_ratio(self):
        assert _derive_memory_budget(10000, budget_ratio=0.3) == (100, 3000)

    def test_caps_at_128k(self):
        assert _derive_memory_budget(300000, budget_ratio=0.5) == (100, 128000)

    def test_default_ratio_is_half(self):
        assert _derive_memory_budget(20000) == (100, 10000)
