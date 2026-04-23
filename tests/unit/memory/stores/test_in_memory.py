"""Tests for InMemoryStorage."""

import pytest

from framework.memory.stores.in_memory import InMemoryStorage


@pytest.fixture
async def storage():
    s = InMemoryStorage()
    await s.initialize()
    yield s
    await s.close()


@pytest.mark.asyncio
class TestInMemoryStorage:
    async def test_kv_roundtrip(self, storage):
        await storage.set("scope1", "key1", "value1")
        assert await storage.get("scope1", "key1") == "value1"

    async def test_delete(self, storage):
        await storage.set("scope1", "key1", "value1")
        assert await storage.delete("scope1", "key1") is True
        assert await storage.delete("scope1", "key1") is False
        assert await storage.get("scope1", "key1") is None

    async def test_list_keys(self, storage):
        await storage.set("scope1", "aaa", 1)
        await storage.set("scope1", "aab", 2)
        await storage.set("scope1", "bbb", 3)
        assert await storage.list_keys("scope1", "aa") == ["aaa", "aab"]

    async def test_messages_roundtrip(self, storage):
        await storage.save_messages("scope1", [{"role": "user", "content": "hi"}])
        msgs = await storage.load_messages("scope1")
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hi"

    async def test_append_message(self, storage):
        await storage.append_message("scope1", {"role": "user", "content": "a"})
        await storage.append_message("scope1", {"role": "assistant", "content": "b"})
        msgs = await storage.load_messages("scope1")
        assert len(msgs) == 2

    async def test_logs_and_cursor(self, storage):
        c1 = await storage.append_log("scope1", {"summary": "s1"})
        c2 = await storage.append_log("scope1", {"summary": "s2"})
        assert c2 == c1 + 1

        logs = await storage.read_logs("scope1", since_cursor=c1)
        assert len(logs) == 1
        assert logs[0]["summary"] == "s2"

    async def test_cursor_persistence(self, storage):
        await storage.set_last_cursor("scope1", "dream", 42)
        assert await storage.get_last_cursor("scope1", "dream") == 42

    async def test_cursor_monotonic_after_clear(self, storage):
        """Cursor should remain monotonic even after logs are cleared."""
        c1 = await storage.append_log("scope1", {"summary": "s1"})
        c2 = await storage.append_log("scope1", {"summary": "s2"})
        c3 = await storage.append_log("scope1", {"summary": "s3"})
        assert c1 == 1
        assert c2 == 2
        assert c3 == 3

        # Simulate clearing logs by direct manipulation
        storage._logs["scope1"] = []
        c4 = await storage.append_log("scope1", {"summary": "s4"})
        # Cursor should NOT reset, matching FileStorage behavior
        assert c4 == 4

    async def test_get_last_cursor_matches_append_log(self, storage):
        """get_last_cursor('default') must reflect append_log cursor progression.

        This ensures HistoryArchiveManager.get_unprocessed() works correctly
        instead of always returning all entries.
        """
        assert await storage.get_last_cursor("scope1", "default") == 0
        c1 = await storage.append_log("scope1", {"summary": "s1"})
        assert await storage.get_last_cursor("scope1", "default") == c1
        c2 = await storage.append_log("scope1", {"summary": "s2"})
        assert await storage.get_last_cursor("scope1", "default") == c2
