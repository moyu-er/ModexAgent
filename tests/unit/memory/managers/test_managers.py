"""Tests for memory layer managers."""

import pytest

from framework.memory.core.scope import MemoryContext, SessionScope, UserScope
from framework.memory.managers.history import HistoryArchiveManager
from framework.memory.managers.long_term import LongTermMemoryManager
from framework.memory.managers.short_term import ShortTermConfig, ShortTermMemoryManager
from framework.memory.stores.in_memory import InMemoryStorage


@pytest.fixture
async def storage():
    s = InMemoryStorage()
    await s.initialize()
    yield s
    await s.close()




@pytest.mark.asyncio
class TestShortTermMemoryManager:
    async def test_add_and_get(self, storage):
        mgr = ShortTermMemoryManager(storage, SessionScope())
        ctx = MemoryContext(session_id="s1")
        await mgr.add_message(ctx, {"role": "user", "content": "hello"})
        msgs = await mgr.get_messages(ctx)
        assert len(msgs) == 1

    async def test_max_messages_compression(self, storage):
        mgr = ShortTermMemoryManager(
            storage, SessionScope(), config=ShortTermConfig(max_messages=3)
        )
        ctx = MemoryContext(session_id="s1")
        for i in range(5):
            await mgr.add_message(ctx, {"role": "user", "content": str(i)})
        msgs = await mgr.get_messages(ctx)
        assert len(msgs) == 3
        assert msgs[0]["content"] == "2"

    async def test_max_tokens_compression(self, storage):
        # 每条消息约 100 chars => ~25 tokens, 2 messages ~50 tokens
        mgr = ShortTermMemoryManager(
            storage, SessionScope(), config=ShortTermConfig(max_tokens=30)
        )
        ctx = MemoryContext(session_id="s1")
        await mgr.add_message(ctx, {"role": "user", "content": "a" * 100})
        await mgr.add_message(ctx, {"role": "user", "content": "b" * 100})
        await mgr.add_message(ctx, {"role": "user", "content": "c" * 100})
        msgs = await mgr.get_messages(ctx)
        # 因为是从头部移除，应该只保留能满足 token 限制的最新消息
        assert len(msgs) >= 1

    async def test_scope_isolation(self, storage):
        mgr = ShortTermMemoryManager(storage, SessionScope())
        await mgr.add_message(MemoryContext(session_id="s1"), {"role": "user", "content": "a"})
        await mgr.add_message(MemoryContext(session_id="s2"), {"role": "user", "content": "b"})
        assert len(await mgr.get_messages(MemoryContext(session_id="s1"))) == 1

    async def test_compression_archives_to_history(self, storage):
        history = HistoryArchiveManager(storage, UserScope())
        mgr = ShortTermMemoryManager(
            storage, SessionScope(), config=ShortTermConfig(max_messages=2), history_manager=history
        )
        ctx = MemoryContext(session_id="s1", user_id="u1")
        for i in range(5):
            await mgr.add_message(ctx, {"role": "user", "content": str(i)})

        # short_term 只保留最近 2 条
        msgs = await mgr.get_messages(ctx)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "3"
        assert msgs[1]["content"] == "4"

        # history 中应该有增量归档记录（每超限制一次就归档一次）
        _, entries = await history.get_unprocessed(ctx)
        assert len(entries) == 3
        total_pruned = sum(e["metadata"]["pruned_count"] for e in entries)
        assert total_pruned == 3
        assert all(e["summary"].strip() for e in entries)
        assert all("(no semantic content)" not in e["summary"] for e in entries)

    async def test_compression_archives_heuristic_summary_when_no_summary(self, storage):
        """当压缩策略未生成有意义摘要时，默认语义归档策略应生成启发式摘要而非占位文本。"""
        history = HistoryArchiveManager(storage, UserScope())
        mgr = ShortTermMemoryManager(
            storage, SessionScope(), config=ShortTermConfig(max_messages=2), history_manager=history
        )
        ctx = MemoryContext(session_id="s1", user_id="u1")
        for i in range(5):
            await mgr.add_message(ctx, {"role": "user", "content": f"msg {i}"})

        _, entries = await history.get_unprocessed(ctx)
        assert len(entries) == 3
        for entry in entries:
            summary = entry["summary"]
            assert "[Auto Archive]" not in summary, f"got placeholder: {summary}"
            assert "(no semantic content)" not in summary, f"got empty placeholder: {summary}"
            assert "msg" in summary.lower(), f"expected heuristic summary with message data, got: {summary}"

    async def test_max_messages_does_not_split_tool_chain(self, storage):
        mgr = ShortTermMemoryManager(
            storage, SessionScope(), config=ShortTermConfig(max_messages=3)
        )
        ctx = MemoryContext(session_id="s1")
        await mgr.add_message(ctx, {"role": "user", "content": "0"})
        await mgr.add_message(
            ctx, {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]}
        )
        await mgr.add_message(
            ctx, {"role": "tool", "content": "result", "tool_call_id": "tc1"}
        )
        await mgr.add_message(ctx, {"role": "user", "content": "3"})
        await mgr.add_message(ctx, {"role": "user", "content": "4"})

        msgs = await mgr.get_messages(ctx)
        # 简单截断会保留 [2,3,4]，其中 [2] 是孤儿 tool result。
        # 安全截断应移除 [0,1,2]，保留 [3,4]
        assert len(msgs) == 2
        assert msgs[0]["content"] == "3"
        assert msgs[1]["content"] == "4"

    async def test_max_tokens_does_not_split_tool_chain(self, storage):
        mgr = ShortTermMemoryManager(
            storage, SessionScope(), config=ShortTermConfig(max_tokens=30)
        )
        ctx = MemoryContext(session_id="s1")
        long_text = "a" * 100
        await mgr.add_message(ctx, {"role": "user", "content": long_text})
        await mgr.add_message(
            ctx, {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]}
        )
        await mgr.add_message(
            ctx, {"role": "tool", "content": "result", "tool_call_id": "tc1"}
        )
        await mgr.add_message(ctx, {"role": "user", "content": long_text})

        msgs = await mgr.get_messages(ctx)
        # 验证没有孤儿 tool result
        valid_tool_call_ids = {
            tc.get("id")
            for m in msgs
            if m.get("role") == "assistant" and m.get("tool_calls")
            for tc in m["tool_calls"]
        }
        for m in msgs:
            if m.get("role") == "tool":
                assert m.get("tool_call_id") in valid_tool_call_ids, "发现孤儿 tool result"

    async def test_uses_remaining_messages_when_strategy_provides_it(self, storage):
        """当 CompressionStrategy 返回 remaining_messages 时，应直接使用而非 not in 反推。"""
        from framework.memory.core.compression import (
            CompressionResult,
            CompressionStrategy,
        )

        class MockStrategy(CompressionStrategy):
            async def compress(self, messages, context):
                # 自定义：只保留最后一条
                return CompressionResult(
                    summary="mock",
                    pruned_messages=messages[:-1],
                    remaining_messages=messages[-1:],
                )

        mgr = ShortTermMemoryManager(
            storage,
            SessionScope(),
            config=ShortTermConfig(
                max_messages=None,  # 不触发内置截断
                max_tokens=None,
                compression_strategy=MockStrategy(),
            ),
        )
        ctx = MemoryContext(session_id="s1")
        # 批量添加 5 条以越过 cooldown 阈值
        await mgr.add_messages(ctx, [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "user", "content": "d"},
            {"role": "user", "content": "e"},
        ])

        msgs = await mgr.get_messages(ctx)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "e"
        # summary 现在存入 KV，不再作为 system 消息污染 history
        scope_key = mgr._scope.get_scope_key(ctx)
        summary = await storage.get(scope_key, ".compression_summary")
        assert summary == "mock"

    async def test_fallback_to_not_in_when_remaining_messages_missing(self, storage):
        """当 CompressionStrategy 不提供 remaining_messages（为 None）时，应回退到 not in 反推。"""
        from framework.memory.core.compression import (
            CompressionResult,
            CompressionStrategy,
        )

        class LegacyStrategy(CompressionStrategy):
            async def compress(self, messages, context):
                return CompressionResult(
                    summary="legacy",
                    pruned_messages=messages[:-1],
                    remaining_messages=None,
                )

        mgr = ShortTermMemoryManager(
            storage,
            SessionScope(),
            config=ShortTermConfig(
                max_messages=None,
                max_tokens=None,
                compression_strategy=LegacyStrategy(),
            ),
        )
        ctx = MemoryContext(session_id="s1")
        await mgr.add_messages(ctx, [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "user", "content": "d"},
            {"role": "user", "content": "e"},
        ])

        msgs = await mgr.get_messages(ctx)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "e"
        scope_key = mgr._scope.get_scope_key(ctx)
        summary = await storage.get(scope_key, ".compression_summary")
        assert summary == "legacy"

    async def test_empty_remaining_messages_clears_all(self, storage):
        """当 CompressionStrategy 显式返回 remaining_messages=[] 时，应清空所有消息。"""
        from framework.memory.core.compression import (
            CompressionResult,
            CompressionStrategy,
        )

        class EmptyStrategy(CompressionStrategy):
            async def compress(self, messages, context):
                return CompressionResult(
                    summary="empty",
                    pruned_messages=messages,
                    remaining_messages=[],
                )

        mgr = ShortTermMemoryManager(
            storage,
            SessionScope(),
            config=ShortTermConfig(
                max_messages=None,
                max_tokens=None,
                compression_strategy=EmptyStrategy(),
            ),
        )
        ctx = MemoryContext(session_id="s1")
        await mgr.add_messages(ctx, [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "user", "content": "d"},
            {"role": "user", "content": "e"},
        ])

        msgs = await mgr.get_messages(ctx)
        assert len(msgs) == 0
        scope_key = mgr._scope.get_scope_key(ctx)
        summary = await storage.get(scope_key, ".compression_summary")
        assert summary == "empty"

    async def test_concurrent_add_messages_does_not_lose_messages(self, storage):
        """Two concurrent writes to same session must not lose messages."""
        import asyncio

        mgr = ShortTermMemoryManager(storage, SessionScope())
        ctx = MemoryContext(session_id="s1")

        async def add_batch(start: int):
            msgs = [{"role": "user", "content": str(start + i)} for i in range(5)]
            await mgr.add_messages(ctx, msgs)

        await asyncio.gather(add_batch(0), add_batch(100))
        msgs = await mgr.get_messages(ctx)
        contents = {m["content"] for m in msgs}
        for i in range(5):
            assert str(i) in contents
            assert str(100 + i) in contents

    async def test_compression_cooldown_skips_when_delta_below_threshold(self, storage):
        """Fewer than 5 new messages should skip compression check."""
        from framework.memory.core.compression import CompressionResult, CompressionStrategy

        class NoOpStrategy(CompressionStrategy):
            async def compress(self, messages, context):
                return CompressionResult(summary="noop", pruned_messages=messages[:-1])

        mgr = ShortTermMemoryManager(
            storage,
            SessionScope(),
            config=ShortTermConfig(
                max_messages=None,
                max_tokens=None,
                compression_strategy=NoOpStrategy(),
            ),
        )
        ctx = MemoryContext(session_id="s1")

        # 首次添加 5 条，越过 cooldown 阈值，触发压缩
        await mgr.add_messages(ctx, [{"role": "user", "content": str(i)} for i in range(5)])
        msgs = await mgr.get_messages(ctx)
        assert len(msgs) == 1  # last message only (summary now stored in KV, not history)

        # 再添加 3 条（delta=3 < 5），应跳过压缩
        await mgr.add_messages(ctx, [{"role": "user", "content": str(i + 100)} for i in range(3)])
        msgs = await mgr.get_messages(ctx)
        # 跳过了压缩，所以新增 3 条都保留了
        assert len(msgs) == 4  # 1 old + 3 new


@pytest.mark.asyncio
class TestHistoryArchiveManager:
    async def test_append_and_read(self, storage):
        mgr = HistoryArchiveManager(storage, UserScope())
        ctx = MemoryContext(user_id="u1")
        c1 = await mgr.append(ctx, "summary1", {"timestamp": "2024-01-01"})
        c2 = await mgr.append(ctx, "summary2", {"timestamp": "2024-01-02"})
        assert c2 > c1

        new_cursor, entries = await mgr.get_unprocessed(ctx)
        assert len(entries) == 2
        assert entries[0]["summary"] == "summary1"

    async def test_cursor_commit(self, storage):
        mgr = HistoryArchiveManager(storage, UserScope())
        ctx = MemoryContext(user_id="u1")
        await mgr.append(ctx, "s1", {})
        new_cursor, _ = await mgr.get_unprocessed(ctx, cursor_name="dream")
        await mgr.commit_cursor(ctx, "dream", new_cursor)
        _, entries = await mgr.get_unprocessed(ctx, cursor_name="dream")
        assert len(entries) == 0

    async def test_append_generates_timestamp_when_missing(self, storage):
        mgr = HistoryArchiveManager(storage, UserScope())
        ctx = MemoryContext(user_id="u1")
        await mgr.append(ctx, "summary", {})  # no timestamp in metadata
        _, entries = await mgr.get_unprocessed(ctx)
        assert len(entries) == 1
        ts = entries[0]["timestamp"]
        assert ts is not None
        assert isinstance(ts, str)
        assert len(ts) > 0

    async def test_append_preserves_provided_timestamp(self, storage):
        mgr = HistoryArchiveManager(storage, UserScope())
        ctx = MemoryContext(user_id="u1")
        await mgr.append(ctx, "summary", {"timestamp": "2024-01-01T00:00:00"})
        _, entries = await mgr.get_unprocessed(ctx)
        assert entries[0]["timestamp"] == "2024-01-01T00:00:00"

    async def test_get_recent_returns_last_n_entries(self, storage):
        mgr = HistoryArchiveManager(storage, UserScope())
        ctx = MemoryContext(user_id="u1")
        for i in range(5):
            await mgr.append(ctx, f"summary_{i}", {})
        recent = await mgr.get_recent(ctx, limit=3)
        assert len(recent) == 3
        assert recent[0]["summary"] == "summary_2"
        assert recent[1]["summary"] == "summary_3"
        assert recent[2]["summary"] == "summary_4"

    async def test_get_recent_returns_all_when_limit_exceeds(self, storage):
        mgr = HistoryArchiveManager(storage, UserScope())
        ctx = MemoryContext(user_id="u1")
        await mgr.append(ctx, "s1", {})
        await mgr.append(ctx, "s2", {})
        recent = await mgr.get_recent(ctx, limit=10)
        assert len(recent) == 2

    async def test_max_entries_prunes_oldest(self, storage):
        mgr = HistoryArchiveManager(storage, UserScope(), max_entries=3)
        ctx = MemoryContext(user_id="u1")
        c1 = await mgr.append(ctx, "first", {})
        await mgr.append(ctx, "second", {})
        await mgr.append(ctx, "third", {})
        c4 = await mgr.append(ctx, "fourth", {})

        entries = await mgr.get_recent(ctx, limit=10)
        assert len(entries) == 3
        cursors = [e["cursor"] for e in entries]
        assert c1 not in cursors
        assert c4 in cursors

    async def test_no_pruning_when_max_entries_none(self, storage):
        mgr = HistoryArchiveManager(storage, UserScope())
        ctx = MemoryContext(user_id="u1")
        for i in range(10):
            await mgr.append(ctx, f"s{i}", {})
        entries = await mgr.get_recent(ctx, limit=100)
        assert len(entries) == 10


@pytest.mark.asyncio
class TestLongTermMemoryManager:
    async def test_get_all_empty(self, storage):
        mgr = LongTermMemoryManager(storage, UserScope())
        ctx = MemoryContext(user_id="u1")
        mem = await mgr.get_all(ctx)
        assert mem.soul == ""
        assert mem.user == ""
        assert mem.memory == ""

    async def test_update_and_get(self, storage):
        mgr = LongTermMemoryManager(storage, UserScope())
        ctx = MemoryContext(user_id="u1")
        await mgr.update(ctx, {"soul": "soul_content", "user": "user_content"})
        mem = await mgr.get_all(ctx)
        assert mem.soul == "soul_content"
        assert mem.user == "user_content"

    async def test_custom_files(self, storage):
        mgr = LongTermMemoryManager(storage, UserScope())
        ctx = MemoryContext(user_id="u1")
        await mgr.update(ctx, {"CUSTOM.md": "custom"})
        mem = await mgr.get_all(ctx)
        assert mem.custom.get("CUSTOM.md") == "custom"

    async def test_apply_update_respects_max_lines(self, storage):
        from framework.memory.core.consolidation import MemoryUpdate

        mgr = LongTermMemoryManager(storage, UserScope(), max_lines=5)
        ctx = MemoryContext(user_id="u1")

        for i in range(10):
            await mgr.apply_update(
                ctx,
                MemoryUpdate(file_name="MEMORY.md", content=f"line {i}", mode="append"),
            )

        result = await mgr.get_file(ctx, "memory")
        lines = [ln for ln in result.split("\n") if ln.strip()]
        assert len(lines) == 5
        assert lines[0] == "line 5"
        assert lines[-1] == "line 9"

    async def test_apply_update_no_truncation_when_under_limit(self, storage):
        from framework.memory.core.consolidation import MemoryUpdate

        mgr = LongTermMemoryManager(storage, UserScope(), max_lines=100)
        ctx = MemoryContext(user_id="u1")
        await mgr.apply_update(
            ctx,
            MemoryUpdate(file_name="MEMORY.md", content="line 1\nline 2", mode="append"),
        )
        result = await mgr.get_file(ctx, "memory")
        assert "line 1" in result
        assert "line 2" in result

    async def test_max_lines_none_means_unbounded(self, storage):
        from framework.memory.core.consolidation import MemoryUpdate

        mgr = LongTermMemoryManager(storage, UserScope())
        ctx = MemoryContext(user_id="u1")
        for i in range(20):
            await mgr.apply_update(
                ctx,
                MemoryUpdate(file_name="MEMORY.md", content=f"line {i}", mode="append"),
            )
        result = await mgr.get_file(ctx, "memory")
        lines = [ln for ln in result.split("\n") if ln.strip()]
        assert len(lines) == 20
