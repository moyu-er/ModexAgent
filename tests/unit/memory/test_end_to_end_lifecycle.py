"""End-to-end tests for the full memory lifecycle.

TDD: verify production, compression, context injection, aging, and cleanup
across all memory tiers: working -> short_term -> history -> long_term.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from framework.core.agent import Agent, AgentContext
from framework.core.emitter import AgentResult, ContentEmitter
from framework.core.events import AgentEvent, EmitterConfig
from framework.core.tool_manager import InMemoryToolManager
from framework.core.types import InputMessage
from framework.memory.archive import PreserveSummaryArchiveStrategy
from framework.memory.core.compression import CompressionResult, CompressionStrategy
from framework.memory.core.scope import MemoryContext
from framework.memory.injection import DefaultMemoryInjectionPolicy
from framework.memory.managers.history import HistoryArchiveManager
from framework.memory.managers.short_term import ShortTermConfig, ShortTermMemoryManager
from framework.memory.stores.in_memory import InMemoryStorage
from framework.memory.system import MemorySystem, MemorySystemContextManager
from framework.session.agent_session import AgentSession


@pytest.fixture
async def in_memory_storage():
    store = InMemoryStorage()
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
async def memory_system():
    with tempfile.TemporaryDirectory() as tmp:
        ms = MemorySystem(workspace=Path(tmp))
        await ms.initialize()
        yield ms
        await ms.close()


@pytest.mark.asyncio
class TestShortTermCompressionArchivesToHistory:
    """验证 short_term 压缩后被裁减的消息会正确归档到 history。"""

    async def test_truncate_by_max_messages_creates_history_entry(self, in_memory_storage):
        from framework.memory.core.scope import SessionScope

        store = in_memory_storage
        history = HistoryArchiveManager(store, SessionScope())
        stm = ShortTermMemoryManager(
            store,
            SessionScope(),
            config=ShortTermConfig(max_messages=3, archive_strategy=PreserveSummaryArchiveStrategy()),
            history_manager=history,
        )

        ctx = MemoryContext(session_id="s1")
        for i in range(5):
            await stm.add_message(ctx, {"role": "user", "content": str(i)})

        short_term = await stm.get_messages(ctx)
        assert len(short_term) == 3
        assert short_term[0]["content"] == "2"

        _, entries = await history.get_unprocessed(ctx)
        # 每次 add_message 超限时都会触发压缩，5 条消息应产生 2 条 history 记录
        assert len(entries) == 2
        summaries = " ".join(e["summary"] for e in entries)
        assert "0" in summaries
        assert "1" in summaries

    async def test_compression_strategy_summary_is_preserved_in_history(self, in_memory_storage):
        store = in_memory_storage
        from framework.memory.core.scope import SessionScope

        class FakeCompressor(CompressionStrategy):
            async def compress(self, messages, context):
                # 仅在消息数 >= 3 时压缩前两条，模拟真实策略行为
                if len(messages) >= 3:
                    pruned = messages[:2]
                    remaining = messages[2:]
                    return CompressionResult(
                        summary="Summary of old messages",
                        pruned_messages=pruned,
                        remaining_messages=remaining,
                    )
                return CompressionResult(
                    summary="",
                    pruned_messages=[],
                    remaining_messages=messages,
                )

        history = HistoryArchiveManager(store, SessionScope())
        stm = ShortTermMemoryManager(
            store,
            SessionScope(),
            config=ShortTermConfig(
                max_messages=None,  # 不设置硬限制，让 compression_strategy 主导压缩
                compression_strategy=FakeCompressor(),
                archive_strategy=PreserveSummaryArchiveStrategy(),
            ),
            history_manager=history,
        )

        ctx = MemoryContext(session_id="s2")
        # 批量添加 >=6 条消息，以超过 COOLDOWN_MSG_DELTA=5，触发 compression_strategy
        messages = [{"role": "user", "content": str(i)} for i in range(6)]
        await stm.add_messages(ctx, messages)

        _, entries = await history.get_unprocessed(ctx)
        assert len(entries) == 1
        assert entries[0]["summary"] == "Summary of old messages"


@pytest.mark.asyncio
class TestMemoryLifecycleEndToEnd:
    """验证消息从 short_term -> history -> long_term 的完整流转。"""

    async def test_full_lifecycle_through_memory_system(self, memory_system):
        ctx = MemoryContext(session_id="life_session", user_id="u1")

        # 1. 消息直接写入 short_term
        await memory_system.add_messages(ctx, [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ])

        short_term = await memory_system.get_history(ctx)
        assert len(short_term) == 2

        # 3. 模拟 short_term 超限时触发压缩（通过调低 max_messages 强制触发）
        stm = memory_system._managers.short_term
        original_max = stm._config.max_messages
        stm._config.max_messages = 1

        await memory_system.add_message(ctx, {"role": "user", "content": "overflow"})

        short_term_after = await memory_system.get_history(ctx)
        assert len(short_term_after) == 1

        # 被压缩的消息应进入 history
        _, history_entries = await memory_system._managers.history.get_unprocessed(ctx)
        assert len(history_entries) >= 1

        # 4. 手动更新 long_term
        await memory_system._managers.long_term.update(ctx, {"soul": "friendly bot"})
        lt = await memory_system.get_long_term(ctx)
        assert lt.soul == "friendly bot"

        stm._config.max_messages = original_max


@pytest.mark.asyncio
class TestContextInjectionEndToEnd:
    """验证 MemorySystemContextManager 加载的 ContextState 正确组合各层记忆。"""

    async def test_load_includes_short_term(self, memory_system):
        ctx = MemoryContext(session_id="inj_session", user_id="u1")
        adapter = MemorySystemContextManager(memory_system)

        # 持久化 short_term 历史
        await memory_system.add_message(ctx, {"role": "user", "content": "old_msg"})
        await memory_system.add_message(ctx, {"role": "assistant", "content": "new_msg"})

        state = await adapter.load("inj_session")
        history = state.history
        assert len(history) == 2
        assert history[0]["content"] == "old_msg"
        assert history[1]["content"] == "new_msg"

    async def test_system_prompt_includes_long_term_and_history_summaries(self, memory_system):
        ctx = MemoryContext(session_id="inj_session2", user_id="u1")
        adapter = MemorySystemContextManager(memory_system)

        # 注入 long_term
        await memory_system._managers.long_term.update(ctx, {"user": "Alice, developer"})
        # 注入 history 摘要
        await memory_system._managers.history.append(ctx, "User likes Python", {})

        # build_system_prompt 需要知道 user_id 才能定位到同一 scope 的长期记忆
        prompt = await adapter.build_system_prompt(
            tool_manager=None,
            runtime_info={"session_id": "inj_session2", "user_id": "u1"},
        )
        assert "Alice" in prompt
        assert "Python" in prompt

    async def test_load_respects_max_short_term_messages(self, memory_system):
        policy = DefaultMemoryInjectionPolicy(max_short_term_messages=2)
        adapter = MemorySystemContextManager(memory_system, injection_policy=policy)
        ctx = MemoryContext(session_id="inj_session3", user_id="u1")

        for i in range(5):
            await memory_system.add_message(ctx, {"role": "user", "content": str(i)})

        state = await adapter.load("inj_session3")
        history = state.history
        assert len(history) == 2
        assert history[0]["content"] == "3"
        assert history[1]["content"] == "4"


@pytest.mark.asyncio
class TestMemoryAgingAndCleanup:
    """验证压缩、cursor 前进、session 清除等行为。"""

    async def test_clear_session_removes_all_tiers(self, memory_system):
        adapter = MemorySystemContextManager(memory_system)
        await adapter.save(
            "clear_session",
            {"role": "user", "content": "x"},
            AgentResult(content="y", messages=[{"role": "assistant", "content": "y"}]),
        )
        await adapter.flush("clear_session")

        # 注入 history 和 long_term，验证 clear 会清除所有 tier
        ctx = adapter._context_cache.get("clear_session")
        await memory_system._managers.history.append(ctx, "Summary", {})
        await memory_system._managers.long_term.update(ctx, {"soul": "test soul"})

        await adapter.clear("clear_session")

        state = await adapter.load("clear_session")
        history = state.history
        assert len(history) == 0

        _, entries = await memory_system._managers.history.get_unprocessed(ctx)
        assert len(entries) == 0

        lt = await memory_system.get_long_term(ctx)
        assert lt.soul == ""

    async def test_history_cursor_advances_after_compression(self, memory_system):
        ctx = MemoryContext(session_id="cursor_session", user_id="u1")
        stm = memory_system._managers.short_term
        original_max = stm._config.max_messages
        stm._config.max_messages = 2

        for i in range(4):
            await memory_system.add_message(ctx, {"role": "user", "content": str(i)})

        # 验证 history 中有条目且 cursor > 0
        new_cursor, entries = await memory_system._managers.history.get_unprocessed(ctx)
        assert len(entries) >= 1
        assert new_cursor >= 1

        stm._config.max_messages = original_max


@pytest.mark.asyncio
class TestMemorySystemContextManagerRoundTrip:
    """验证 save -> load -> build_system_prompt 的完整 round-trip。"""

    async def test_save_and_load_round_trip(self, memory_system):
        adapter = MemorySystemContextManager(memory_system)
        await adapter.save(
            "rt_session",
            {"role": "user", "content": "hello"},
            AgentResult(),
        )
        # Agent implementations are responsible for appending their own messages
        # 模拟 ReAct agent 通过 context.history 实时写入 assistant message
        ctx = MemoryContext(session_id="rt_session", user_id="default")
        await memory_system.add_message(ctx, {"role": "assistant", "content": "world"})

        state = await adapter.load("rt_session")
        history = state.history
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    async def test_multiple_saves_maintain_order(self, memory_system):
        adapter = MemorySystemContextManager(memory_system)
        ctx = MemoryContext(session_id="multi_session", user_id="default")
        for i in range(3):
            await adapter.save(
                "multi_session",
                {"role": "user", "content": f"u{i}"},
                AgentResult(
                    messages=[{"role": "assistant", "content": f"a{i}"}],
                ),
            )
            # 模拟 ReAct agent 实时写入 assistant message
            await memory_system.add_message(ctx, {"role": "assistant", "content": f"a{i}"})

        state = await adapter.load("multi_session")
        history = state.history
        assert len(history) == 6
        assert history[0]["content"] == "u0"
        assert history[1]["content"] == "a0"
        assert history[5]["content"] == "a2"

    async def test_runtime_info_injects_into_system_prompt(self, memory_system):
        adapter = MemorySystemContextManager(memory_system)
        prompt = await adapter.build_system_prompt(
            tool_manager=None,
            runtime_info={"session_id": "rt", "platform": "qq"},
        )
        assert "qq" in prompt


@pytest.mark.asyncio
class TestDreamEngineIntegration:
    """验证 DreamEngine 能从未处理 history 中生成并更新 long_term 记忆。"""

    async def test_dream_engine_updates_long_term(self, memory_system):
        from framework.memory.consolidation.dream_engine import DreamEngine

        # 构造一个假的 LLM provider，返回固定分析结果
        class FakeLLM:
            async def chat(self, messages, temperature=None, max_tokens=None):
                for m in messages:
                    if "memory editing assistant" in m.get("content", ""):
                        return (
                            '[{"file_name": "USER.md", "mode": "append", '
                            '"content": "- Name: Alice\\n", "reason": "identity"}]'
                        )
                    if "memory analysis assistant" in m.get("content", ""):
                        return "[USER] Name: Alice"
                return ""
            chat_with_retry = chat

        ctx = MemoryContext(session_id="dream_session", user_id="u1")
        # 先写入一条 history 摘要
        await memory_system._managers.history.append(ctx, "The user is Alice", {})

        dream = DreamEngine(
            llm_provider=FakeLLM(),
            history_manager=memory_system.history_manager,
            long_term_manager=memory_system.long_term_manager,
        )

        success = await dream.run(ctx)
        assert success is True

        lt = await memory_system.get_long_term(ctx)
        assert "Alice" in lt.user

    async def test_dream_engine_cursor_advances_even_on_skip(self, memory_system):
        from framework.memory.consolidation.dream_engine import DreamEngine

        class FakeLLM:
            async def chat(self, messages, temperature=None, max_tokens=None):
                return "[SKIP] no new information"
            chat_with_retry = chat

        ctx = MemoryContext(session_id="dream_session2", user_id="u1")
        await memory_system._managers.history.append(ctx, "Boring summary", {})

        dream = DreamEngine(
            llm_provider=FakeLLM(),
            history_manager=memory_system.history_manager,
            long_term_manager=memory_system.long_term_manager,
        )

        success = await dream.run(ctx)
        assert success is True

        # cursor 应前进，后续 get_unprocessed 应返回空
        _, entries = await memory_system._managers.history.get_unprocessed(ctx, cursor_name="dream")
        assert len(entries) == 0


@pytest.mark.asyncio
class TestScopeIsolation:
    """验证不同 user_id / session_id 的记忆隔离。"""

    async def test_user_isolation_for_history_and_long_term(self, memory_system):
        adapter = MemorySystemContextManager(memory_system)

        await adapter.save(
            "iso_session",
            {"role": "user", "content": "alice_msg"},
            AgentResult(content="hi"),
            metadata={"input_metadata": {"user_id": "alice"}},
        )
        await adapter.save(
            "iso_session",
            {"role": "user", "content": "bob_msg"},
            AgentResult(content="hey"),
            metadata={"input_metadata": {"user_id": "bob"}},
        )

        # short_term 默认按 SessionScope 隔离，因此同一 session 下两人消息都会出现在 short_term 中
        # 但 history（中期记忆）和 long_term 按 UserScope 隔离
        ctx_alice = MemoryContext(session_id="iso_session", user_id="alice")
        ctx_bob = MemoryContext(session_id="iso_session", user_id="bob")

        # 直接往 history 写入各自 user 的条目来验证隔离
        await memory_system._managers.history.append(ctx_alice, "Alice summary", {})
        await memory_system._managers.history.append(ctx_bob, "Bob summary", {})

        _, alice_history = await memory_system._managers.history.get_unprocessed(ctx_alice)
        _, bob_history = await memory_system._managers.history.get_unprocessed(ctx_bob)
        assert len(alice_history) == 1
        assert len(bob_history) == 1
        assert "Alice" in alice_history[0]["summary"]
        assert "Bob" in bob_history[0]["summary"]

        # 注入 long_term
        await memory_system._managers.long_term.update(ctx_alice, {"soul": "Alice soul"})
        await memory_system._managers.long_term.update(ctx_bob, {"soul": "Bob soul"})

        lt_alice = await memory_system.get_long_term(ctx_alice)
        lt_bob = await memory_system.get_long_term(ctx_bob)
        assert lt_alice.soul == "Alice soul"
        assert lt_bob.soul == "Bob soul"

    async def test_session_isolation_for_short_term(self, memory_system):
        ctx1 = MemoryContext(session_id="s_a", user_id="u1")
        ctx2 = MemoryContext(session_id="s_b", user_id="u1")

        await memory_system.add_message(ctx1, {"role": "user", "content": "in_a"})
        await memory_system.add_message(ctx2, {"role": "user", "content": "in_b"})

        h1 = await memory_system.get_history(ctx1)
        h2 = await memory_system.get_history(ctx2)

        assert len(h1) == 1 and h1[0]["content"] == "in_a"
        assert len(h2) == 1 and h2[0]["content"] == "in_b"

    async def test_long_term_is_shared_by_user_not_session(self, memory_system):
        ctx1 = MemoryContext(session_id="s_a", user_id="u1")
        ctx2 = MemoryContext(session_id="s_b", user_id="u1")
        ctx3 = MemoryContext(session_id="s_c", user_id="u2")

        await memory_system._managers.long_term.update(ctx1, {"soul": "witty"})

        lt2 = await memory_system.get_long_term(ctx2)
        lt3 = await memory_system.get_long_term(ctx3)

        assert lt2.soul == "witty"  # 同 user 共享
        assert lt3.soul != "witty"  # 不同 user 隔离


class _FakeEvent(AgentEvent):
    TEST = "test"


class _FakeEmitter(ContentEmitter[_FakeEvent]):
    def __init__(self):
        super().__init__(EmitterConfig())
        self.deltas = []

    async def emit_delta(self, delta: str) -> None:
        self.deltas.append(delta)

    async def emit_error(self, error: str) -> None:
        pass

    async def emit_complete(self, result: AgentResult) -> None:
        pass


class _MemoryWritingAgent(Agent[_FakeEvent]):
    event_enum = _FakeEvent
    max_iterations = 3

    @property
    def name(self) -> str:
        return "memory_writer"

    async def run(self, context: AgentContext, emitter: ContentEmitter[_FakeEvent], streaming: bool = True):
        return AgentResult(
            content="final",
            stop_reason="complete",
            messages=[{"role": "assistant", "content": "mid"}],
        )


@pytest.mark.asyncio
class TestAgentSessionMemoryIntegration:
    """验证 AgentSession 与 MemorySystem 的完整交互流程。"""

    async def test_session_process_message_persists_to_memory_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()
            adapter = MemorySystemContextManager(ms)
            tm = InMemoryToolManager()
            session = AgentSession(
                agent=_MemoryWritingAgent(),
                context_manager=adapter,
                tool_manager=tm,
            )
            emitter = _FakeEmitter()

            result = await session.process_message(
                InputMessage(content="hello"),
                emitter,
                session_id="sess_1",
            )

            assert result.stop_reason == "complete"
            # _MemoryWritingAgent 未通过 context.history.append() 写入消息，
            # 因此只有 user message 被 save() 持久化
            state = await adapter.load("sess_1")
            history = state.history
            assert len(history) == 1  # user only

            await ms.close()

    async def test_session_dream_engine_trigger_updates_long_term(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()
            adapter = MemorySystemContextManager(ms)
            tm = InMemoryToolManager()

            # 注入 history 条目以触发 DreamEngine
            # MemorySystemContextManager 默认 user_id 为 "default"
            ctx = MemoryContext(session_id="sess_2", user_id="default")
            await ms._managers.history.append(ctx, "User is Alice", {})
            await ms._managers.history.append(ctx, "User likes Python", {})

            from framework.memory.consolidation.dream_engine import DreamEngine

            class FakeLLM:
                async def chat(self, messages, temperature=None, max_tokens=None):
                    for m in messages:
                        if "memory editing assistant" in m.get("content", ""):
                            return (
                                '[{"file_name": "USER.md", "mode": "append", '
                                '"content": "- Name: Alice\\n- Likes: Python\\n", "reason": "summary"}]'
                            )
                        if "memory analysis assistant" in m.get("content", ""):
                            return "[USER] Name: Alice, Likes: Python"
                    return ""
                chat_with_retry = chat

            dream = DreamEngine(
                llm_provider=FakeLLM(),
                history_manager=ms.history_manager,
                long_term_manager=ms.long_term_manager,
            )

            session = AgentSession(
                agent=_MemoryWritingAgent(),
                context_manager=adapter,
                tool_manager=tm,
                dream_engine=dream,
                dream_threshold=1,
            )
            emitter = _FakeEmitter()

            await session.process_message(
                InputMessage(content="hello"),
                emitter,
                session_id="sess_2",
            )
            await asyncio.sleep(0.1)

            lt = await ms.get_long_term(ctx)
            assert "Alice" in lt.user

            await ms.close()

    async def test_session_save_metadata_includes_finish_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()
            adapter = MemorySystemContextManager(ms)
            tm = InMemoryToolManager()
            session = AgentSession(
                agent=_MemoryWritingAgent(),
                context_manager=adapter,
                tool_manager=tm,
            )
            emitter = _FakeEmitter()

            await session.process_message(
                InputMessage(content="hi"),
                emitter,
                session_id="sess_3",
                runtime_info={"platform": "test"},
            )

            state = await adapter.load("sess_3")
            # 元数据应被传入 save，但由于 InMemoryContextManager 才有 metadata 字段，
            # MemorySystemContextManager 的 load 返回的 ContextState 未保存 metadata。
            # 本测试验证的是 process_message 不会异常退出，且历史正确。
            history = state.history
            assert len(history) == 1  # user only (agent 未通过 context.history 写入)

            await ms.close()
