"""Tests for FileStorage."""

import tempfile
from pathlib import Path

import pytest

from framework.memory.core.scope import MemoryContext
from framework.memory.stores.file import FileStorage


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as tmp:
        s = FileStorage(Path(tmp))
        await s.initialize()
        yield s
        await s.close()


@pytest.mark.asyncio
class TestFileStorage:
    async def test_kv_roundtrip(self, storage):
        await storage.set("user1", "SOUL.md", "# Soul")
        assert await storage.get("user1", "SOUL.md") == "# Soul"

    async def test_delete(self, storage):
        await storage.set("user1", "key1", "val1")
        assert await storage.delete("user1", "key1") is True
        assert await storage.delete("user1", "key1") is False

    async def test_list_keys(self, storage):
        await storage.set("s1", "aaa", 1)
        await storage.set("s1", "aab", 2)
        keys = await storage.list_keys("s1", "aa")
        assert set(keys) == {"aaa", "aab"}

    async def test_messages_roundtrip(self, storage):
        msgs = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        await storage.save_messages("sess1", msgs)
        loaded = await storage.load_messages("sess1")
        assert loaded == msgs

    async def test_append_message(self, storage):
        await storage.append_message("sess1", {"role": "user", "content": "a"})
        await storage.append_message("sess1", {"role": "assistant", "content": "b"})
        loaded = await storage.load_messages("sess1")
        assert len(loaded) == 2
        assert loaded[0]["content"] == "a"

    async def test_append_log_returns_cursor(self, storage):
        c1 = await storage.append_log("user1", {"summary": "first"})
        c2 = await storage.append_log("user1", {"summary": "second"})
        assert c2 == c1 + 1

    async def test_read_logs_since_cursor(self, storage):
        await storage.append_log("user1", {"summary": "first"})
        c2 = await storage.append_log("user1", {"summary": "second"})
        await storage.append_log("user1", {"summary": "third"})

        logs = await storage.read_logs("user1", since_cursor=c2)
        assert len(logs) == 1
        assert logs[0]["summary"] == "third"

    async def test_cursor_persistence(self, storage):
        await storage.set_last_cursor("user1", "dream", 10)
        assert await storage.get_last_cursor("user1", "dream") == 10

        # Re-init with same workspace should preserve cursor
        s2 = FileStorage(storage.workspace)
        await s2.initialize()
        assert await s2.get_last_cursor("user1", "dream") == 10
        await s2.close()

    async def test_scope_isolation(self, storage):
        await storage.set("s1", "key", "val1")
        await storage.set("s2", "key", "val2")
        assert await storage.get("s1", "key") == "val1"
        assert await storage.get("s2", "key") == "val2"

    async def test_special_chars_scope_key(self, storage):
        scope = "tenant:user:session:abc"
        await storage.set(scope, "key", "value")
        assert await storage.get(scope, "key") == "value"

    async def test_kv_concurrent_writes(self, storage):
        """并发写入同一 KV 文件不应导致数据丢失或损坏。"""
        import asyncio

        async def worker(worker_id: int):
            for i in range(10):
                await storage.set("concurrent", f"worker_{worker_id}", i)

        # 启动多个并发 worker
        await asyncio.gather(*[worker(wid) for wid in range(5)])

        # 数据应该完整可读，没有 JSON 损坏
        for wid in range(5):
            val = await storage.get("concurrent", f"worker_{wid}")
            assert val == 9, f"Expected 9 for worker_{wid}, got {val}"

    async def test_kv_concurrent_mixed_ops(self, storage):
        """并发混合 set/delete 操作应保持文件一致性。"""
        import asyncio

        # 预置一些 key
        for i in range(5):
            await storage.set("mixed", f"key_{i}", i)

        async def flipper(key_idx: int):
            for _ in range(5):
                await storage.set("mixed", f"key_{key_idx}", "set")
                await storage.delete("mixed", f"key_{key_idx}")

        await asyncio.gather(*[flipper(i) for i in range(5)])

        # 最终状态不确定，但文件必须能正常解析
        keys = await storage.list_keys("mixed", "key_")
        # 只要没有 JSON 解析错误就算通过
        assert isinstance(keys, list)

    async def test_failed_log_write_does_not_advance_cursor(self, storage, monkeypatch):
        """日志写入失败时，cursor 不应提前推进（避免产生空洞）。"""
        c1 = await storage.append_log("crash_test", {"summary": "s1"})

        # 模拟 JSONL 写入失败
        original_open = open

        def failing_open(path, *args, **kwargs):
            if str(path).endswith("history.jsonl"):
                raise OSError("simulated write failure")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", failing_open)

        with pytest.raises(OSError):
            await storage.append_log("crash_test", {"summary": "s2"})

        # cursor 不应因写入失败而推进
        cursor_after = await storage.get_last_cursor("crash_test", "default")
        assert cursor_after == c1, f"cursor advanced despite write failure: {cursor_after} != {c1}"

    async def test_append_log_cursor_update_before_jsonl(self, storage):
        """模拟崩溃场景：cursor 已更新但 JSONL 未写入，不应导致重复 cursor。"""
        c1 = await storage.append_log("crash_test", {"summary": "s1"})

        # 手动将 cursor 文件回滚到 0（模拟写入 JSONL 后 cursor 未更新的旧 bug）
        # 但实际上我们的新实现应该是 cursor 先更新，所以这里直接验证下一次不重复
        await storage.set_last_cursor("crash_test", "default", 0)

        c2 = await storage.append_log("crash_test", {"summary": "s2"})
        # 即使 cursor 文件被回滚，也不应复用已有 cursor
        assert c2 != c1, f"cursor reused after rollback: {c2} == {c1}"
        # 读取日志应无重复 cursor
        logs = await storage.read_logs("crash_test", since_cursor=0)
        cursors = [log["cursor"] for log in logs]
        assert len(cursors) == len(set(cursors)), f"duplicate cursors: {cursors}"

    async def test_get_last_cursor_recovers_from_log_file(self, storage):
        """当 cursor 文件丢失或损坏时，能从历史日志末尾恢复 cursor。"""
        await storage.append_log("recover", {"summary": "s1"})
        await storage.append_log("recover", {"summary": "s2"})
        c3 = await storage.append_log("recover", {"summary": "s3"})

        # 删除 cursor 文件
        scope_dir = storage._scope_dir("recover")
        cursor_path = scope_dir / ".cursor_default"
        cursor_path.unlink()

        recovered = await storage.get_last_cursor("recover", "default")
        assert recovered == c3, f"expected {c3}, got {recovered}"

        # 继续追加应使用 c3 + 1
        c4 = await storage.append_log("recover", {"summary": "s4"})
        assert c4 == c3 + 1

    async def test_concurrent_append_message(self, storage):
        """并发 append_message 不应导致 JSONL 损坏或行丢失。"""
        import asyncio

        async def worker(worker_id: int):
            for i in range(20):
                await storage.append_message(
                    "concurrent_append",
                    {"role": "user", "content": f"w{worker_id}_i{i}"},
                )

        await asyncio.gather(*[worker(wid) for wid in range(5)])

        loaded = await storage.load_messages("concurrent_append")
        assert len(loaded) == 100, f"expected 100 messages, got {len(loaded)}"
        # 验证所有行都是合法 JSON
        for msg in loaded:
            assert "role" in msg and "content" in msg

    async def test_concurrent_save_messages(self, storage):
        """并发 save_messages 覆盖写入不应产生损坏文件。"""
        import asyncio

        async def worker(worker_id: int):
            msgs = [{"role": "user", "content": f"w{worker_id}"} for _ in range(10)]
            for _ in range(10):
                await storage.save_messages("concurrent_save", msgs)

        await asyncio.gather(*[worker(wid) for wid in range(5)])

        loaded = await storage.load_messages("concurrent_save")
        # 最终文件应该能被完整解析，长度为 10
        assert len(loaded) == 10, f"expected 10 messages, got {len(loaded)}"
        for msg in loaded:
            assert "role" in msg and "content" in msg

    async def test_initialize_cleans_up_tmp_files(self, storage):
        """initialize() 应清理 workspace 中残留的 .tmp 文件。"""
        # 人为创建一个 .tmp 残留文件
        tmp_file = storage.workspace / "session_123" / "messages.jsonl.tmp"
        tmp_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file.write_text("trap", encoding="utf-8")
        assert tmp_file.exists()

        # 重新初始化同一 workspace
        s2 = FileStorage(storage.workspace)
        await s2.initialize()
        assert not tmp_file.exists()
        await s2.close()

    async def test_scope_record_roundtrip_with_sanitized_path(self, storage):
        scope_key = "tenant:用户:session/with*chars"
        context = MemoryContext(
            session_id="session/with*chars",
            user_id="用户",
            tenant_id="tenant",
            agent_id="main",
            channel="qq",
            chat_id="group:123",
        )

        await storage.ensure_scope_metadata(
            scope_key,
            layer="short_term",
            context=context,
            agent_role="main",
            agent_id="main",
        )
        records = await storage.list_scope_records(layer="short_term", has_file=None)

        assert len(records) == 1
        record = records[0]
        assert record.scope_key == scope_key
        assert record.layer == "short_term"
        assert record.agent_role == "main"
        assert record.context.chat_id == "group:123"
        assert Path(record.storage_path).name != scope_key

    async def test_scope_record_default_filters_main_role(self, storage):
        await storage.ensure_scope_metadata(
            "main-session",
            layer="short_term",
            context=MemoryContext(session_id="main-session", agent_id="main"),
            agent_role="main",
            agent_id="main",
        )
        await storage.ensure_scope_metadata(
            "peer-session",
            layer="short_term",
            context=MemoryContext(session_id="peer-session", agent_id="peer-a"),
            agent_role="peer",
            agent_id="peer-a",
        )

        default_records = await storage.list_scope_records(layer="short_term")
        all_records = await storage.list_scope_records(layer="short_term", agent_roles=None)
        peer_records = await storage.list_scope_records(layer="short_term", agent_roles={"peer"})

        assert [r.scope_key for r in default_records] == ["main-session"]
        assert {r.scope_key for r in all_records} == {"main-session", "peer-session"}
        assert [r.scope_key for r in peer_records] == ["peer-session"]

    async def test_scope_record_filters_by_layer_and_file(self, storage):
        await storage.ensure_scope_metadata(
            "short",
            layer="short_term",
            context=MemoryContext(session_id="short", agent_id="main"),
            agent_role="main",
            agent_id="main",
        )
        await storage.ensure_scope_metadata(
            "history",
            layer="history",
            context=MemoryContext(user_id="u1", agent_id="main"),
            agent_role="main",
            agent_id="main",
        )
        await storage.save_messages("short", [{"role": "user", "content": "hello"}])

        records = await storage.list_scope_records(layer="short_term", has_file="messages")

        assert [r.scope_key for r in records] == ["short"]
