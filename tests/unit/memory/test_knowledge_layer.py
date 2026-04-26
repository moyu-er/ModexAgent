from __future__ import annotations

import logging

from framework.memory.core.consolidation import MemoryUpdate, MemoryUpdateMode
from framework.memory.core.scope import MemoryContext, MemoryLayerName, UserScope
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.layers.knowledge import ScopedKnowledgeMemoryManager
from framework.memory.registry.in_memory import InMemoryStoreRegistry


async def test_append_update_is_idempotent():
    registry = InMemoryStoreRegistry()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.KNOWLEDGE)
    manager = ScopedKnowledgeMemoryManager(factory)
    ctx = MemoryContext(session_id="knowledge", user_id="user")
    update = MemoryUpdate(
        file_name="memory",
        mode=MemoryUpdateMode.APPEND,
        content="用户喜欢 Python 数据分析",
        reason="test",
    )

    await manager.apply_update(ctx, update)
    await manager.apply_update(ctx, update)

    assert await manager.get_file(ctx, "memory") == "用户喜欢 Python 数据分析"


async def test_replace_text_retry_is_idempotent():
    registry = InMemoryStoreRegistry()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.KNOWLEDGE)
    manager = ScopedKnowledgeMemoryManager(factory)
    ctx = MemoryContext(session_id="knowledge", user_id="user")
    storage = await registry.resolve(
        layer=MemoryLayerName.KNOWLEDGE,
        scope=UserScope(),
        context=ctx,
    )
    await storage.set("MEMORY.md", "用户喜欢 Java")
    update = MemoryUpdate(
        file_name="memory",
        mode=MemoryUpdateMode.REPLACE_TEXT,
        search_text="Java",
        content="Python",
        reason="test",
    )

    await manager.apply_update(ctx, update)
    await manager.apply_update(ctx, update)

    assert await manager.get_file(ctx, "memory") == "用户喜欢 Python"


async def test_retrieve_defaults_to_get_all():
    registry = InMemoryStoreRegistry()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.KNOWLEDGE)
    manager = ScopedKnowledgeMemoryManager(factory)
    ctx = MemoryContext(session_id="knowledge", user_id="user")
    await manager.apply_update(
        ctx,
        MemoryUpdate(
            file_name="memory",
            mode=MemoryUpdateMode.APPEND,
            content="用户喜欢 Python 数据分析",
            reason="test",
        ),
    )

    retrieved = await manager.retrieve(ctx, query="irrelevant")
    all_memory = await manager.get_all(ctx)

    assert retrieved.memory == all_memory.memory
    assert retrieved.custom == all_memory.custom


async def test_replace_text_fallback_append_logs_warning(caplog):
    registry = InMemoryStoreRegistry()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.KNOWLEDGE)
    manager = ScopedKnowledgeMemoryManager(factory)
    ctx = MemoryContext(session_id="knowledge", user_id="user")
    storage = await registry.resolve(
        layer=MemoryLayerName.KNOWLEDGE,
        scope=UserScope(),
        context=ctx,
    )
    await storage.set("MEMORY.md", "existing fact")
    update = MemoryUpdate(
        file_name="memory",
        mode=MemoryUpdateMode.REPLACE_TEXT,
        search_text="missing fact",
        content="new fact",
        reason="test",
    )

    with caplog.at_level(logging.WARNING, logger="framework.memory.layers.knowledge"):
        result = await manager.apply_update(ctx, update)

    assert result == "existing fact\nnew fact"
    assert "replace_text fallback append" in caplog.text


async def test_remove_skipped_logs_warning(caplog):
    registry = InMemoryStoreRegistry()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.KNOWLEDGE)
    manager = ScopedKnowledgeMemoryManager(factory)
    ctx = MemoryContext(session_id="knowledge", user_id="user")
    storage = await registry.resolve(
        layer=MemoryLayerName.KNOWLEDGE,
        scope=UserScope(),
        context=ctx,
    )
    await storage.set("MEMORY.md", "existing fact")
    update = MemoryUpdate(
        file_name="memory",
        mode=MemoryUpdateMode.REMOVE,
        search_text="missing fact",
        content="",
        reason="test",
    )

    with caplog.at_level(logging.WARNING, logger="framework.memory.layers.knowledge"):
        result = await manager.apply_update(ctx, update)

    assert result == "existing fact"
    assert "remove update skipped" in caplog.text
