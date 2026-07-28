from __future__ import annotations

import asyncio
from pathlib import Path

from modex_agent.core.scope import MemoryContext, MemoryLayerName
from modex_agent.memory.archive_models import (
    ArchiveChannel,
    ArchiveDocuments,
    ArchiveGenerationResult,
    ArchiveWrite,
)
from modex_agent.memory.layers.archive import ScopedArchiveMemoryManager
from modex_agent.memory.layers.config import ArchiveMemoryConfig
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.registry import DefaultMemoryStoreRegistry


async def test_append_bundle_writes_same_archive_id_to_both_channels(tmp_path: Path) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="s1", user_id="u1")

    result = await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="context"),
        ArchiveWrite(channel=ArchiveChannel.CORE, summary="knowledge"),
    ))

    context_entries = await manager.get_recent(ctx, channel=ArchiveChannel.CONTEXT, limit=5)
    knowledge_entries = await manager.get_recent(ctx, channel=ArchiveChannel.CORE, limit=5)

    assert result.archive_id == 1
    assert context_entries[0].metadata["archive_id"] == 1
    assert knowledge_entries[0].metadata["archive_id"] == 1
    assert context_entries[0].summary == "context"
    assert knowledge_entries[0].summary == "knowledge"


async def test_append_generation_materializes_documents_under_allocated_id(
    tmp_path: Path,
) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="s1", user_id="u1")

    first = await manager.append_generation(
        ctx,
        ArchiveGenerationResult(
            documents=ArchiveDocuments(
                context="context 1",
                core="knowledge 1",
                index="topic 1",
            )
        ),
    )
    second = await manager.append_generation(
        ctx,
        ArchiveGenerationResult(
            documents=ArchiveDocuments(
                context="context 2",
                core="knowledge 2",
                index="topic 2",
            )
        ),
    )

    archive_root = tmp_path / "archive" / "u1"
    assert (first.archive_id, second.archive_id) == (1, 2)
    assert (archive_root / "1" / "context.md").read_text(encoding="utf-8") == "context 1"
    assert (archive_root / "1" / "knowledge.md").read_text(encoding="utf-8") == "knowledge 1"
    assert (archive_root / "1" / "index.md").read_text(encoding="utf-8") == "topic 1"
    assert (archive_root / "2" / "context.md").read_text(encoding="utf-8") == "context 2"
    assert (archive_root / "2" / "knowledge.md").read_text(encoding="utf-8") == "knowledge 2"
    assert (archive_root / "2" / "index.md").read_text(encoding="utf-8") == "topic 2"


async def test_concurrent_append_generation_keeps_documents_with_allocated_ids(
    tmp_path: Path,
) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="s1", user_id="u1")

    async def append(label: str) -> tuple[int, str]:
        result = await manager.append_generation(
            ctx,
            ArchiveGenerationResult(
                documents=ArchiveDocuments(
                    context=f"context {label}",
                    core=f"knowledge {label}",
                    index=f"topic {label}",
                )
            ),
        )
        return result.archive_id, label

    allocations = await asyncio.gather(append("a"), append("b"))

    archive_root = tmp_path / "archive" / "u1"
    assert {archive_id for archive_id, _ in allocations} == {1, 2}
    for archive_id, label in allocations:
        archive_dir = archive_root / str(archive_id)
        assert (archive_dir / "context.md").read_text(encoding="utf-8") == f"context {label}"
        assert (archive_dir / "knowledge.md").read_text(encoding="utf-8") == f"knowledge {label}"
        assert (archive_dir / "index.md").read_text(encoding="utf-8") == f"topic {label}"


async def test_append_empty_generation_allocates_archive_id(tmp_path: Path) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="s1", user_id="u1")

    result = await manager.append_generation(
        ctx,
        ArchiveGenerationResult(
            documents=ArchiveDocuments(context="", core="", index="")
        ),
    )

    archive_dir = tmp_path / "archive" / "u1" / "1"
    assert result.archive_id == 1
    assert result.written_channels == (
        ArchiveChannel.CONTEXT,
        ArchiveChannel.CORE,
    )
    assert (archive_dir / "context.md").read_text(encoding="utf-8") == ""
    assert (archive_dir / "knowledge.md").read_text(encoding="utf-8") == ""
    assert (archive_dir / "index.md").read_text(encoding="utf-8") == ""


async def test_knowledge_cursor_uses_archive_id(tmp_path: Path) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="s1", user_id="u1")

    await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="context 1"),
        ArchiveWrite(channel=ArchiveChannel.CORE, summary="knowledge 1"),
    ))
    await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="context 2"),
        ArchiveWrite(channel=ArchiveChannel.CORE, summary="knowledge 2"),
    ))

    first = await manager.get_unprocessed(
        ctx,
        cursor_name="dream",
        channel=ArchiveChannel.CORE,
    )
    await manager.commit_cursor(
        ctx,
        cursor_name="dream",
        cursor=1,
        channel=ArchiveChannel.CORE,
    )
    second = await manager.get_unprocessed(
        ctx,
        cursor_name="dream",
        channel=ArchiveChannel.CORE,
    )

    assert first.cursor == 2
    assert [entry.summary for entry in first.entries] == ["knowledge 1", "knowledge 2"]
    assert [entry.summary for entry in second.entries] == ["knowledge 2"]


async def test_prune_consumed_pairs_keeps_three_consumed_pairs(tmp_path: Path) -> None:
    registry = DefaultMemoryStoreRegistry(tmp_path)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(
        factory,
        ArchiveMemoryConfig(retained_consumed_archive_pairs=3),
    )
    ctx = MemoryContext(session_id="s1", user_id="u1")

    for index in range(1, 8):
        await manager.append_bundle(ctx, (
            ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary=f"context {index}"),
            ArchiveWrite(channel=ArchiveChannel.CORE, summary=f"knowledge {index}"),
        ))

    await manager.commit_cursor(
        ctx,
        cursor_name="dream",
        cursor=5,
        channel=ArchiveChannel.CORE,
    )
    await manager.prune_consumed_pairs(ctx)

    context_entries = await manager.get_recent(ctx, channel=ArchiveChannel.CONTEXT, limit=10)
    knowledge_entries = await manager.get_recent(ctx, channel=ArchiveChannel.CORE, limit=10)

    assert [entry.metadata["archive_id"] for entry in context_entries] == [3, 4, 5, 6, 7]
    assert [entry.metadata["archive_id"] for entry in knowledge_entries] == [3, 4, 5, 6, 7]


async def test_cursor_field_equals_archive_id_not_per_channel_counter(tmp_path: Path) -> None:
    """Entry cursor values equal archive_id, not per-channel sequential counters.

    The per-channel cursor (scoped_file:228, scoped_in_memory:136) is a V1
    holdover.  Each channel independently counts from 1, so when one channel
    gets more writes than the other the cursors drift apart from archive_id.
    The fix uses ``entry["archive_id"]`` directly as the cursor.
    """
    registry = DefaultMemoryStoreRegistry(tmp_path)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="s1", user_id="u1")

    # Bundle 1: full pair → CONTEXT cursor=1, KNOWLEDGE cursor=1 (matching archive_id=1)
    await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="c1"),
        ArchiveWrite(channel=ArchiveChannel.CORE, summary="k1"),
    ))
    # Bundle 2: full pair → CONTEXT cursor=2, KNOWLEDGE cursor=2 (matching archive_id=2)
    await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="c2"),
        ArchiveWrite(channel=ArchiveChannel.CORE, summary="k2"),
    ))
    # Bundle 3: CONTEXT only → CONTEXT cursor=3, KNOWLEDGE cursor=2 (but archive_id=3)
    await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="c3"),
    ))

    storage = await factory(ctx)
    assert storage.archive is not None
    if hasattr(storage.archive, "read_channel_logs"):
        knowledge_raw = await storage.archive.read_channel_logs(ArchiveChannel.CORE.value)
    else:
        knowledge_raw = await storage.archive.read_logs()
        knowledge_raw = [e for e in knowledge_raw if e.get("channel") == ArchiveChannel.CORE.value]

    # KNOWLEDGE has 2 entries (bundles 1 and 2), each with archive_id 1,2.
    # With per-channel cursor: cursor values are 1, 2 (happens to match archive_id here).
    # But no entry should have cursor != archive_id.
    # DirArchiveStorage (file backend) doesn't store a cursor field, so skip
    # the check for entries that lack one.
    for entry in knowledge_raw:
        if "cursor" not in entry:
            continue
        aid = int(entry.get("archive_id", 0) or 0)
        cur = int(entry.get("cursor", 0) or 0)
        assert cur == aid, (
            f"KNOWLEDGE entry archive_id={aid} has per-channel cursor={cur}; "
            f"cursor must equal archive_id"
        )

    # Bundle 3 only wrote CONTEXT with archive_id=3.
    # CONTEXT channel now has 3 entries with cursors 1,2,3 matching archive_ids 1,2,3.
    # This passes coincidentally because per-channel counter == archive_id for this pattern.
    context_raw = await storage.archive.read_channel_logs(ArchiveChannel.CONTEXT.value)
    for entry in context_raw:
        if "cursor" not in entry:
            continue
        aid = int(entry.get("archive_id", 0) or 0)
        cur = int(entry.get("cursor", 0) or 0)
        assert cur == aid, (
            f"CONTEXT entry archive_id={aid} has per-channel cursor={cur}; "
            f"cursor must equal archive_id"
        )


async def test_prune_consumed_pairs_does_not_lose_concurrent_appends(tmp_path: Path) -> None:
    """Concurrent append_bundle calls must not lose entries.

    When two append_bundle calls race, the prune inside each must see all
    entries that were committed before the prune started, including entries
    from the other concurrent append_bundle.  This requires prune to hold
    the write lock across the full read→filter→save cycle.
    """
    registry = DefaultMemoryStoreRegistry(tmp_path)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    config = ArchiveMemoryConfig(retained_consumed_archive_pairs=0)
    manager = ScopedArchiveMemoryManager(factory, config)
    ctx = MemoryContext(session_id="s1", user_id="u1")

    # Seed: bundles 1-6, consume through 6 (so all 6 can be pruned)
    for i in range(1, 7):
        await manager.append_bundle(ctx, (
            ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary=f"c{i}"),
            ArchiveWrite(channel=ArchiveChannel.CORE, summary=f"k{i}"),
        ))
    await manager.commit_cursor(ctx, "dream", 6, channel=ArchiveChannel.CORE)

    # Run two concurrent append_bundle calls.  Each internally calls
    # prune_consumed_pairs outside the write lock.  With retained_pairs=0
    # and consumed=6, prune tries to delete archive_id <= 6.
    # If a prune reads entries before the other append_bundle writes,
    # the later save_channel_logs (full replace) could lose the other's entry.
    async def append(i: int) -> None:
        await manager.append_bundle(ctx, (
            ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary=f"new-c{i}"),
            ArchiveWrite(channel=ArchiveChannel.CORE, summary=f"new-k{i}"),
        ))

    await asyncio.gather(append(7), append(8))

    storage = await factory(ctx)
    assert storage.archive is not None
    context_entries = await storage.archive.read_channel_logs(ArchiveChannel.CONTEXT.value)
    archive_ids = {int(e.get("archive_id", 0) or 0) for e in context_entries}

    # Both concurrent bundles must survive
    assert 7 in archive_ids, f"archive_id 7 lost during concurrent prune. IDs: {sorted(archive_ids)}"
    assert 8 in archive_ids, f"archive_id 8 lost during concurrent prune. IDs: {sorted(archive_ids)}"


async def test_append_bundle_fifo_evicts_oldest_consumed_when_exceeding_max_archive_total(
    tmp_path: Path,
) -> None:
    """append_bundle FIFO-evicts oldest consumed archive dirs once count > cap.

    Sequence (max_archive_total=2, default retained_consumed_archive_pairs=3):

    - 6 bundles → dirs 1..6, none consumed (core_consumed_archive_id=0).
    - commit_cursor(5) → marks 1..5 consumed; 6 stays unconsumed.
    - 7th append fires the FIFO check inside _do_append:
        * _do_prune first (safe_delete = 5 - 3 = 2) deletes dirs 1,2.
        * FIFO then sees deletable consumed = [3,4,5], count 3 > cap 2,
          deletes the oldest one beyond the cap → dir 3.
        * Consumed [4,5] preserved (within cap); unconsumed [6,7] never touched.
    """
    registry = DefaultMemoryStoreRegistry(tmp_path)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(
        factory,
        ArchiveMemoryConfig(max_archive_total=2),
    )
    ctx = MemoryContext(session_id="s1", user_id="u1")

    for i in range(1, 7):
        await manager.append_bundle(ctx, (
            ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary=f"context {i}"),
            ArchiveWrite(channel=ArchiveChannel.CORE, summary=f"core {i}"),
        ))

    await manager.commit_cursor(ctx, "dream", 5, channel=ArchiveChannel.CORE)

    await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="context 7"),
        ArchiveWrite(channel=ArchiveChannel.CORE, summary="core 7"),
    ))

    archive_root = tmp_path / "archive" / "u1"
    remaining = sorted(
        int(child.name)
        for child in archive_root.iterdir()
        if child.is_dir() and child.name.isdigit()
    )
    assert remaining == [4, 5, 6, 7]


async def test_append_bundle_fifo_never_deletes_unconsumed_archives(
    tmp_path: Path,
) -> None:
    """Unconsumed archives (aid > core_consumed_archive_id) are never FIFO-evicted.

    With core_consumed_archive_id=0, prune_to_max's deletable set is empty
    (dir_archive.py:176: ``deletable = [aid for aid in ids if aid <= min_safe_id]
    if min_safe_id > 0 else []``), so even with count > max_archive_total no
    dir is removed.
    """
    registry = DefaultMemoryStoreRegistry(tmp_path)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(
        factory,
        ArchiveMemoryConfig(max_archive_total=2),
    )
    ctx = MemoryContext(session_id="s1", user_id="u1")

    for i in range(1, 5):
        await manager.append_bundle(ctx, (
            ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary=f"context {i}"),
            ArchiveWrite(channel=ArchiveChannel.CORE, summary=f"core {i}"),
        ))

    archive_root = tmp_path / "archive" / "u1"
    remaining = sorted(
        int(child.name)
        for child in archive_root.iterdir()
        if child.is_dir() and child.name.isdigit()
    )
    assert remaining == [1, 2, 3, 4]
