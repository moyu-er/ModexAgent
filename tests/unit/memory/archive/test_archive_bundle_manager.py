from __future__ import annotations

from framework.memory.archive_models import ArchiveChannel, ArchiveWrite
from framework.memory.core.scope import MemoryContext, MemoryLayerName
from framework.memory.layers.archive import ScopedArchiveMemoryManager
from framework.memory.layers.config import ArchiveMemoryConfig
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.registry.in_memory import InMemoryStoreRegistry


async def test_append_bundle_writes_same_archive_id_to_both_channels() -> None:
    registry = InMemoryStoreRegistry()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="s1", user_id="u1")

    result = await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="context"),
        ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary="knowledge"),
    ))

    context_entries = await manager.get_recent(ctx, channel=ArchiveChannel.CONTEXT, limit=5)
    knowledge_entries = await manager.get_recent(ctx, channel=ArchiveChannel.KNOWLEDGE, limit=5)

    assert result.archive_id == 1
    assert context_entries[0].metadata["archive_id"] == 1
    assert knowledge_entries[0].metadata["archive_id"] == 1
    assert context_entries[0].summary == "context"
    assert knowledge_entries[0].summary == "knowledge"


async def test_knowledge_cursor_uses_archive_id() -> None:
    registry = InMemoryStoreRegistry()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="s1", user_id="u1")

    await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="context 1"),
        ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary="knowledge 1"),
    ))
    await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="context 2"),
        ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary="knowledge 2"),
    ))

    first = await manager.get_unprocessed(
        ctx,
        cursor_name="dream",
        channel=ArchiveChannel.KNOWLEDGE,
    )
    await manager.commit_cursor(
        ctx,
        cursor_name="dream",
        cursor=1,
        channel=ArchiveChannel.KNOWLEDGE,
    )
    second = await manager.get_unprocessed(
        ctx,
        cursor_name="dream",
        channel=ArchiveChannel.KNOWLEDGE,
    )

    assert first.cursor == 2
    assert [entry.summary for entry in first.entries] == ["knowledge 1", "knowledge 2"]
    assert [entry.summary for entry in second.entries] == ["knowledge 2"]


async def test_prune_consumed_pairs_keeps_three_consumed_pairs() -> None:
    registry = InMemoryStoreRegistry()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(
        factory,
        ArchiveMemoryConfig(retained_consumed_archive_pairs=3),
    )
    ctx = MemoryContext(session_id="s1", user_id="u1")

    for index in range(1, 8):
        await manager.append_bundle(ctx, (
            ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary=f"context {index}"),
            ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary=f"knowledge {index}"),
        ))

    await manager.commit_cursor(
        ctx,
        cursor_name="dream",
        cursor=5,
        channel=ArchiveChannel.KNOWLEDGE,
    )
    await manager.prune_consumed_pairs(ctx)

    context_entries = await manager.get_recent(ctx, channel=ArchiveChannel.CONTEXT, limit=10)
    knowledge_entries = await manager.get_recent(ctx, channel=ArchiveChannel.KNOWLEDGE, limit=10)

    assert [entry.metadata["archive_id"] for entry in context_entries] == [3, 4, 5, 6, 7]
    assert [entry.metadata["archive_id"] for entry in knowledge_entries] == [3, 4, 5, 6, 7]
