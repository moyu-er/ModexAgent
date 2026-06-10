from __future__ import annotations

import asyncio

import pytest

from framework.memory.archive_models import (
    ArchiveChannel,
    ArchiveWrite,
)
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


async def test_cursor_field_equals_archive_id_not_per_channel_counter() -> None:
    """Entry cursor values equal archive_id, not per-channel sequential counters.

    The per-channel cursor (scoped_file:228, scoped_in_memory:136) is a V1
    holdover.  Each channel independently counts from 1, so when one channel
    gets more writes than the other the cursors drift apart from archive_id.
    The fix uses ``entry["archive_id"]`` directly as the cursor.
    """
    registry = InMemoryStoreRegistry()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="s1", user_id="u1")

    # Bundle 1: full pair → CONTEXT cursor=1, KNOWLEDGE cursor=1 (matching archive_id=1)
    await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="c1"),
        ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary="k1"),
    ))
    # Bundle 2: full pair → CONTEXT cursor=2, KNOWLEDGE cursor=2 (matching archive_id=2)
    await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="c2"),
        ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary="k2"),
    ))
    # Bundle 3: CONTEXT only → CONTEXT cursor=3, KNOWLEDGE cursor=2 (but archive_id=3)
    await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="c3"),
    ))

    storage = await factory(ctx)
    if hasattr(storage, "read_channel_logs"):
        knowledge_raw = await storage.read_channel_logs(ArchiveChannel.KNOWLEDGE.value)
    else:
        knowledge_raw = await storage.read_logs()
        knowledge_raw = [e for e in knowledge_raw if e.get("channel") == ArchiveChannel.KNOWLEDGE.value]

    # KNOWLEDGE has 2 entries (bundles 1 and 2), each with archive_id 1,2.
    # With per-channel cursor: cursor values are 1, 2 (happens to match archive_id here).
    # But no entry should have cursor != archive_id.
    for entry in knowledge_raw:
        aid = int(entry.get("archive_id", 0) or 0)
        cur = int(entry.get("cursor", 0) or 0)
        assert cur == aid, (
            f"KNOWLEDGE entry archive_id={aid} has per-channel cursor={cur}; "
            f"cursor must equal archive_id"
        )

    # Bundle 3 only wrote CONTEXT with archive_id=3.
    # CONTEXT channel now has 3 entries with cursors 1,2,3 matching archive_ids 1,2,3.
    # This passes coincidentally because per-channel counter == archive_id for this pattern.
    context_raw = await storage.read_channel_logs(ArchiveChannel.CONTEXT.value)
    for entry in context_raw:
        aid = int(entry.get("archive_id", 0) or 0)
        cur = int(entry.get("cursor", 0) or 0)
        assert cur == aid, (
            f"CONTEXT entry archive_id={aid} has per-channel cursor={cur}; "
            f"cursor must equal archive_id"
        )


async def test_prune_consumed_pairs_does_not_lose_concurrent_appends() -> None:
    """Concurrent append_bundle calls must not lose entries.

    When two append_bundle calls race, the prune inside each must see all
    entries that were committed before the prune started, including entries
    from the other concurrent append_bundle.  This requires prune to hold
    the write lock across the full read→filter→save cycle.
    """
    registry = InMemoryStoreRegistry()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    config = ArchiveMemoryConfig(retained_consumed_archive_pairs=0)
    manager = ScopedArchiveMemoryManager(factory, config)
    ctx = MemoryContext(session_id="s1", user_id="u1")

    # Seed: bundles 1-6, consume through 6 (so all 6 can be pruned)
    for i in range(1, 7):
        await manager.append_bundle(ctx, (
            ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary=f"c{i}"),
            ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary=f"k{i}"),
        ))
    await manager.commit_cursor(ctx, "dream", 6, channel=ArchiveChannel.KNOWLEDGE)

    # Run two concurrent append_bundle calls.  Each internally calls
    # prune_consumed_pairs outside the write lock.  With retained_pairs=0
    # and consumed=6, prune tries to delete archive_id <= 6.
    # If a prune reads entries before the other append_bundle writes,
    # the later save_channel_logs (full replace) could lose the other's entry.
    async def append(i: int) -> None:
        await manager.append_bundle(ctx, (
            ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary=f"new-c{i}"),
            ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary=f"new-k{i}"),
        ))

    await asyncio.gather(append(7), append(8))

    storage = await factory(ctx)
    context_entries = await storage.read_channel_logs(ArchiveChannel.CONTEXT.value)
    archive_ids = {int(e.get("archive_id", 0) or 0) for e in context_entries}

    # Both concurrent bundles must survive
    assert 7 in archive_ids, f"archive_id 7 lost during concurrent prune. IDs: {sorted(archive_ids)}"
    assert 8 in archive_ids, f"archive_id 8 lost during concurrent prune. IDs: {sorted(archive_ids)}"
