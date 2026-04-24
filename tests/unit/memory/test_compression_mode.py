"""Tests for ShortTermConfig.compression_mode (delete vs cursor)."""

import pytest

from framework.memory.compaction.pipeline import MemoryCompactionPipeline, MemoryCompactionResult
from framework.memory.core.scope import MemoryContext, SessionScope
from framework.memory.managers.short_term import ShortTermConfig, ShortTermMemoryManager
from framework.memory.stores.in_memory import InMemoryStorage


@pytest.fixture
def context():
    return MemoryContext(session_id="sess_1")


@pytest.fixture
def scope():
    return SessionScope()


@pytest.fixture
async def storage():
    s = InMemoryStorage()
    await s.initialize()
    return s


@pytest.fixture
def dummy_pipeline():
    """模拟压缩 pipeline：移除前半部分消息。"""

    class DummyPipeline:
        async def run(self, context, messages, reason, keep_recent_messages=None):
            split = len(messages) // 2
            return MemoryCompactionResult(
                remaining_messages=[dict(m) for m in messages[split:]],
                pruned_messages=[dict(m) for m in messages[:split]],
                summary="dummy summary",
                archived=True,
                archive_success=True,
            )

    return DummyPipeline()


@pytest.mark.asyncio
async def test_cursor_mode_get_messages_filters_cursor(context, scope, storage, dummy_pipeline):
    """cursor 模式下 get_messages() 只返回 cursor 之后的消息。"""
    config = ShortTermConfig(
        compression_mode="cursor",
        pipeline=dummy_pipeline,
        max_messages=3,
    )
    mgr = ShortTermMemoryManager(storage, scope, config=config)

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(6)]
    await mgr.add_messages(context, messages)

    # 触发压缩：前半部分被压缩
    await mgr._maybe_compress(context)

    # get_messages 应该只返回后半部分
    visible = await mgr.get_messages(context)
    assert len(visible) == 3
    assert visible[0].content == "msg 3"

    # 但底层消息仍然存在
    raw = await storage.load_messages("sess_1")
    assert len(raw) == 6


@pytest.mark.asyncio
async def test_cursor_mode_get_all_messages_returns_everything(context, scope, storage, dummy_pipeline):
    """cursor 模式下 get_all_messages() 返回所有消息。"""
    config = ShortTermConfig(
        compression_mode="cursor",
        pipeline=dummy_pipeline,
        max_messages=3,
    )
    mgr = ShortTermMemoryManager(storage, scope, config=config)

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(6)]
    await mgr.add_messages(context, messages)
    await mgr._maybe_compress(context)

    all_msgs = await mgr.get_all_messages(context)
    assert len(all_msgs) == 6


@pytest.mark.asyncio
async def test_cursor_mode_no_physical_delete(context, scope, storage, dummy_pipeline):
    """cursor 模式下压缩不物理删除消息。"""
    config = ShortTermConfig(
        compression_mode="cursor",
        pipeline=dummy_pipeline,
        max_messages=3,
    )
    mgr = ShortTermMemoryManager(storage, scope, config=config)

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(4)]
    await mgr.add_messages(context, messages)
    await mgr._maybe_compress(context)

    raw = await storage.load_messages("sess_1")
    assert len(raw) == 4
    assert raw[0]["content"] == "msg 0"


@pytest.mark.asyncio
async def test_cursor_mode_updates_compression_cursor(context, scope, storage, dummy_pipeline):
    """cursor 模式下应更新 .compression_cursor KV。"""
    config = ShortTermConfig(
        compression_mode="cursor",
        pipeline=dummy_pipeline,
        max_messages=3,
    )
    mgr = ShortTermMemoryManager(storage, scope, config=config)

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(6)]
    await mgr.add_messages(context, messages)
    await mgr._maybe_compress(context)

    cursor = await storage.get("sess_1", ".compression_cursor")
    assert cursor == 3  # 前半部分（3条）被压缩


@pytest.mark.asyncio
async def test_delete_mode_physical_deletes(context, scope, storage, dummy_pipeline):
    """delete 模式下压缩会物理删除消息。"""
    config = ShortTermConfig(
        compression_mode="delete",
        pipeline=dummy_pipeline,
        max_messages=3,
    )
    mgr = ShortTermMemoryManager(storage, scope, config=config)

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(6)]
    await mgr.add_messages(context, messages)
    await mgr._maybe_compress(context)

    raw = await storage.load_messages("sess_1")
    assert len(raw) == 3
    assert raw[0]["content"] == "msg 3"


@pytest.mark.asyncio
async def test_default_compression_mode_is_delete():
    """默认压缩模式应为 delete。"""
    config = ShortTermConfig()
    assert config.compression_mode == "delete"


@pytest.mark.asyncio
async def test_cursor_mode_no_cursor_when_no_compression(context, scope, storage):
    """cursor 模式下若未触发压缩，不应设置 cursor。"""
    config = ShortTermConfig(
        compression_mode="cursor",
        max_messages=3,
    )
    mgr = ShortTermMemoryManager(storage, scope, config=config)

    messages = [{"role": "user", "content": "hello"}]
    await mgr.add_messages(context, messages)

    # 不触发压缩（消息数不足 COOLDOWN_MSG_DELTA 且未超限制）
    cursor = await storage.get("sess_1", ".compression_cursor")
    assert cursor is None


@pytest.mark.asyncio
async def test_cursor_mode_hard_limit_truncate_updates_cursor(context, scope, storage):
    """cursor 模式下硬截断（max_messages）也应更新 cursor。"""
    config = ShortTermConfig(
        compression_mode="cursor",
        max_messages=3,
    )
    mgr = ShortTermMemoryManager(storage, scope, config=config)

    messages = [{"role": "user", "content": f"msg {i}"} for i in range(6)]
    await mgr.add_messages(context, messages)

    # 触发硬截断：6条超3条限制
    await mgr._maybe_compress(context)

    visible = await mgr.get_messages(context)
    assert len(visible) == 3

    cursor = await storage.get("sess_1", ".compression_cursor")
    assert cursor == 3  # 前3条被截断

    # 原始消息仍保留
    raw = await storage.load_messages("sess_1")
    assert len(raw) == 6
