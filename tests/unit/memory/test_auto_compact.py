"""Tests for AutoCompactService."""

import time

import pytest

from framework.memory.archive import SemanticArchiveStrategy
from framework.memory.auto_compact import AutoCompactService
from framework.memory.core.scope import (
    MemoryAgentRole,
    MemoryContext,
    MemoryLayerName,
    UserScope,
)
from framework.memory.managers.history import HistoryArchiveManager
from framework.memory.stores.in_memory import InMemoryStorage


@pytest.fixture
async def storage():
    s = InMemoryStorage()
    await s.initialize()
    return s


async def register_short_scope(
    storage: InMemoryStorage,
    scope_key: str,
    role: MemoryAgentRole = MemoryAgentRole.MAIN,
) -> None:
    await storage.ensure_scope_metadata(
        scope_key,
        layer=MemoryLayerName.SHORT_TERM,
        context=MemoryContext(session_id=scope_key, agent_id=role),
        agent_role=role,
        agent_id=role,
    )


@pytest.mark.asyncio
async def test_auto_compact_skips_recently_active(storage):
    """活跃会话不应被压缩."""
    service = AutoCompactService(storage, idle_threshold_seconds=60)
    await register_short_scope(storage, "session_1")
    await storage.set("session_1", ".last_activity", time.time())
    await storage.save_messages("session_1", [{"role": "user", "content": "hello"}])

    result = await service.scan_once()
    assert result == []


@pytest.mark.asyncio
async def test_auto_compact_compresses_idle_session(storage):
    """空闲超阈值的会话应被压缩，保留最近 N 条."""
    service = AutoCompactService(storage, idle_threshold_seconds=1, keep_recent_messages=2)

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
    await register_short_scope(storage, "session_1")
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
    await register_short_scope(storage, "session_1")
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
    await register_short_scope(storage, "session_1")
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
    await register_short_scope(storage, "session_1")
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
        await register_short_scope(storage, name)
        await storage.save_messages(name, messages)
        await storage.set(name, ".last_activity", time.time() - 10)

    result = await service.scan_once()
    assert set(result) == {"session_a", "session_b", "session_c"}


@pytest.mark.asyncio
async def test_auto_compact_ignores_scope_without_last_activity(storage):
    """没有 .last_activity 的 scope 不应被处理."""
    service = AutoCompactService(storage, idle_threshold_seconds=1, keep_recent_messages=2)

    messages = [{"role": "user", "content": "msg"} for _ in range(5)]
    await register_short_scope(storage, "session_1")
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
    await register_short_scope(storage, "session_1")
    await storage.save_messages("session_1", original)
    await storage.set("session_1", ".last_activity", time.time() - 10)

    await service.scan_once()

    # 原始列表不应被修改（InMemoryStorage 会复制，但验证一下）
    assert len(original) == 5


@pytest.mark.asyncio
async def test_auto_compact_ignores_peer_subagent_scopes(storage):
    service = AutoCompactService(storage, idle_threshold_seconds=1, keep_recent_messages=2)

    for scope_key, role in [
        ("main_session", MemoryAgentRole.MAIN),
        ("peer_session", MemoryAgentRole.PEER),
        ("subagent_session", MemoryAgentRole.SUBAGENT),
    ]:
        await register_short_scope(storage, scope_key, role)
        await storage.save_messages(
            scope_key,
            [{"role": "user", "content": f"{scope_key} msg {i}"} for i in range(5)],
        )
        await storage.set(scope_key, ".last_activity", time.time() - 10)

    result = await service.scan_once()

    assert result == ["main_session"]
    assert len(await storage.load_messages("main_session")) == 2
    assert len(await storage.load_messages("peer_session")) == 5
    assert len(await storage.load_messages("subagent_session")) == 5


@pytest.mark.asyncio
async def test_auto_compact_archives_before_prune(storage):
    history = HistoryArchiveManager(storage, UserScope())
    service = AutoCompactService(
        storage,
        idle_threshold_seconds=1,
        keep_recent_messages=2,
        history_manager=history,
        archive_strategy=SemanticArchiveStrategy(),
    )
    scope_key = "session_1"
    ctx = MemoryContext(session_id=scope_key, user_id="u1", agent_id=MemoryAgentRole.MAIN)
    await storage.ensure_scope_metadata(
        scope_key,
        layer=MemoryLayerName.SHORT_TERM,
        context=ctx,
        agent_role=MemoryAgentRole.MAIN,
        agent_id=MemoryAgentRole.MAIN,
    )
    await storage.save_messages(
        scope_key,
        [{"role": "user", "content": f"important msg {i}"} for i in range(5)],
    )
    await storage.set(scope_key, ".last_activity", time.time() - 10)

    result = await service.scan_once()

    assert result == [scope_key]
    kept = await storage.load_messages(scope_key)
    assert [msg["content"] for msg in kept] == ["important msg 3", "important msg 4"]
    entries = await history.get_recent(ctx, limit=10)
    assert len(entries) == 1
    assert entries[0]["metadata"]["pruned_count"] == 3


@pytest.mark.asyncio
async def test_auto_compact_keeps_short_term_on_archive_failure(storage):
    class FailingArchiveStrategy(SemanticArchiveStrategy):
        async def archive(self, context, pruned_messages, compression_result, history_manager):
            raise RuntimeError("archive failed")

    history = HistoryArchiveManager(storage, UserScope())
    service = AutoCompactService(
        storage,
        idle_threshold_seconds=1,
        keep_recent_messages=2,
        history_manager=history,
        archive_strategy=FailingArchiveStrategy(),
    )
    scope_key = "session_1"
    ctx = MemoryContext(session_id=scope_key, user_id="u1", agent_id=MemoryAgentRole.MAIN)
    await storage.ensure_scope_metadata(
        scope_key,
        layer=MemoryLayerName.SHORT_TERM,
        context=ctx,
        agent_role=MemoryAgentRole.MAIN,
        agent_id=MemoryAgentRole.MAIN,
    )
    original = [{"role": "user", "content": f"msg {i}"} for i in range(5)]
    await storage.save_messages(scope_key, original)
    await storage.set(scope_key, ".last_activity", time.time() - 10)

    result = await service.scan_once()

    assert result == []
    assert await storage.load_messages(scope_key) == original
