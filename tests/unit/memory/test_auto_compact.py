"""Tests for AutoCompactService."""

import time

import pytest

from framework.memory.auto_compact import AutoCompactService
from framework.memory.stores.in_memory import InMemoryStorage


@pytest.fixture
async def storage():
    s = InMemoryStorage()
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_auto_compact_skips_recently_active(storage):
    """活跃会话不应被压缩."""
    service = AutoCompactService(storage, idle_threshold_seconds=60)
    await storage.set("session_1", ".last_activity", time.time())
    await storage.save_messages("session_1", [{"role": "user", "content": "hello"}])

    result = await service.scan_once()
    assert result == []


@pytest.mark.asyncio
async def test_auto_compact_compresses_idle_session(storage):
    """空闲超阈值的会话应被压缩，保留最近 N 条."""
    service = AutoCompactService(storage, idle_threshold_seconds=1, keep_recent_messages=2)

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
    await storage.save_messages("session_1", messages)
    await storage.set("session_1", ".last_activity", time.time() - 10)

    result = await service.scan_once()
    assert result == ["session_1"]

    kept = await storage.load_messages("session_1")
    assert len(kept) == 2
    assert kept[0]["content"] == "msg 3"
    assert kept[1]["content"] == "msg 4"


@pytest.mark.asyncio
async def test_auto_compact_skips_when_messages_few(storage):
    """消息数不足 keep_recent 时不压缩."""
    service = AutoCompactService(storage, idle_threshold_seconds=1, keep_recent_messages=10)

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(3)]
    await storage.save_messages("session_1", messages)
    await storage.set("session_1", ".last_activity", time.time() - 10)

    result = await service.scan_once()
    assert result == []

    kept = await storage.load_messages("session_1")
    assert len(kept) == 3


@pytest.mark.asyncio
async def test_auto_compact_sets_summary(storage):
    """压缩后应设置 .auto_compact_summary KV."""
    service = AutoCompactService(storage, idle_threshold_seconds=1, keep_recent_messages=2)

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
    await storage.save_messages("session_1", messages)
    await storage.set("session_1", ".last_activity", time.time() - 10)

    await service.scan_once()

    summary = await storage.get("session_1", ".auto_compact_summary")
    assert summary is not None
    assert "Resumed Session" in summary
    assert "3 older messages" in summary


@pytest.mark.asyncio
async def test_auto_compact_updates_last_activity(storage):
    """压缩后应更新 .last_activity，防止立即再次触发."""
    service = AutoCompactService(storage, idle_threshold_seconds=1, keep_recent_messages=2)

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
    await storage.save_messages("session_1", messages)
    await storage.set("session_1", ".last_activity", time.time() - 10)

    await service.scan_once()

    # 再次扫描不应触发（last_activity 已更新）
    result = await service.scan_once()
    assert result == []


@pytest.mark.asyncio
async def test_auto_compact_multiple_scopes(storage):
    """一次扫描可压缩多个 scope."""
    service = AutoCompactService(storage, idle_threshold_seconds=1, keep_recent_messages=2)

    for name in ["session_a", "session_b", "session_c"]:
        messages = [{"role": "user", "content": f"{name} msg {i}"} for i in range(5)]
        await storage.save_messages(name, messages)
        await storage.set(name, ".last_activity", time.time() - 10)

    result = await service.scan_once()
    assert set(result) == {"session_a", "session_b", "session_c"}


@pytest.mark.asyncio
async def test_auto_compact_ignores_scope_without_last_activity(storage):
    """没有 .last_activity 的 scope 不应被处理."""
    service = AutoCompactService(storage, idle_threshold_seconds=1, keep_recent_messages=2)

    messages = [{"role": "user", "content": "msg"} for _ in range(5)]
    await storage.save_messages("session_1", messages)
    # 不设置 .last_activity

    result = await service.scan_once()
    assert result == []


@pytest.mark.asyncio
async def test_auto_compact_handles_empty_storage(storage):
    """空存储不应报错."""
    service = AutoCompactService(storage, idle_threshold_seconds=1)
    result = await service.scan_once()
    assert result == []


@pytest.mark.asyncio
async def test_auto_compact_does_not_mutate_original(storage):
    """压缩应通过 save_messages 保存新列表，不修改传入的原始列表."""
    service = AutoCompactService(storage, idle_threshold_seconds=1, keep_recent_messages=2)

    original = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
    await storage.save_messages("session_1", original)
    await storage.set("session_1", ".last_activity", time.time() - 10)

    await service.scan_once()

    # 原始列表不应被修改（InMemoryStorage 会复制，但验证一下）
    assert len(original) == 5
