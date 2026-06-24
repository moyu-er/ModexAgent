"""Tests for DreamEngine deadlock and race-condition bugs."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from modex_agent.memory.archive_models import ArchiveChannel, ArchiveWrite
from modex_agent.memory.core.lock import AioRWLock
from modex_agent.memory.core.scope import MemoryContext, MemoryLayerName
from modex_agent.memory.layers.archive import ScopedArchiveMemoryManager
from modex_agent.memory.layers.config import ArchiveMemoryConfig
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.registry.in_memory import InMemoryStoreRegistry


async def test_commit_cursor_race_with_append_bundle():
    """commit_cursor() must not lose updates made by concurrent append_bundle().

    Bug: commit_cursor() does NOT acquire the storage lock. It reads state,
    then writes state — but append_bundle() modifies state between read and
    write. This causes next_archive_id to be overwritten, leading to:
    - duplicate archive_ids
    - get_unprocessed() returning wrong entries
    - DreamEngine entering infinite reprocessing loops
    """
    registry = InMemoryStoreRegistry()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="race", user_id="u1")

    # Pre-populate with some entries
    for i in range(5):
        await manager.append_bundle(ctx, (
            ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary=f"ctx{i}"),
            ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary=f"knl{i}"),
        ))

    unprocessed_before = await manager.get_unprocessed(ctx, "dream", limit=100)
    assert len(unprocessed_before.entries) == 5

    # Simulate concurrent commit_cursor and append_bundle
    async def commit_task():
        for _ in range(20):
            await manager.commit_cursor(ctx, "dream", 2, channel=ArchiveChannel.KNOWLEDGE)
            await asyncio.sleep(0)

    async def append_task():
        for i in range(20):
            await manager.append_bundle(ctx, (
                ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary=f"race_ctx{i}"),
                ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary=f"race_knl{i}"),
            ))
            await asyncio.sleep(0)

    await asyncio.gather(commit_task(), append_task())

    # Verify next_archive_id advanced correctly (5 initial + 20 appended = 25)
    # If commit_cursor() overwrote the state, next_archive_id would be wrong
    recent = await manager.get_recent(ctx, limit=50, channel=ArchiveChannel.KNOWLEDGE)
    archive_ids = {e.entry_id for e in recent}
    # All 25 entries should have unique archive_ids
    assert len(recent) == 25, f"Expected 25 entries, got {len(recent)}"
    assert len(archive_ids) == 25, f"Duplicate archive_ids detected: {len(archive_ids)} unique from {len(recent)} entries"


async def test_commit_cursor_advances_monotonically_under_race():
    """Even with concurrent commit_cursor calls, next_archive_id must never decrease."""
    registry = InMemoryStoreRegistry()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="monotonic", user_id="u1")

    # Start with 3 entries
    for i in range(3):
        await manager.append_bundle(ctx, (
            ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary=f"c{i}"),
            ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary=f"k{i}"),
        ))

    # Concurrent commit_cursor from different sources
    async def committer(name: str, cursor: int):
        for _ in range(10):
            await manager.commit_cursor(ctx, name, cursor, channel=ArchiveChannel.KNOWLEDGE)
            await asyncio.sleep(0)

    await asyncio.gather(
        committer("dream", 1),
        committer("other", 2),
    )

    # Append one more entry — its archive_id must be > 3
    result = await manager.append_bundle(ctx, (
        ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary="after_race"),
    ))
    assert result.archive_id > 3, (
        f"archive_id regressed: got {result.archive_id}, expected > 3. "
        "commit_cursor() likely overwrote next_archive_id."
    )


async def test_prune_does_not_starve_concurrent_reads():
    """prune_consumed_pairs() must not block readers indefinitely.

    Bug: prune_consumed_pairs() acquires the archive storage write lock and
    rewrites ALL channel logs. During this time, ALL readers are blocked
    (AioRWLock is writer-preferring). With a large archive, this causes
    severe latency for message injection and new message processing.
    """
    registry = InMemoryStoreRegistry()
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.ARCHIVE)
    manager = ScopedArchiveMemoryManager(factory, ArchiveMemoryConfig())
    ctx = MemoryContext(session_id="prune", user_id="u1")

    # Populate with many entries
    for i in range(50):
        await manager.append_bundle(ctx, (
            ArchiveWrite(channel=ArchiveChannel.CONTEXT, summary=f"c{i}" * 50),
            ArchiveWrite(channel=ArchiveChannel.KNOWLEDGE, summary=f"k{i}" * 50),
        ))

    # Commit some cursors so pruning has work to do
    await manager.commit_cursor(ctx, "dream", 40, channel=ArchiveChannel.KNOWLEDGE)

    prune_started = asyncio.Event()
    prune_done = asyncio.Event()

    async def prune_task():
        prune_started.set()
        await manager.prune_consumed_pairs(ctx)
        prune_done.set()

    async def reader_task():
        # Wait for prune to start
        await prune_started.wait()
        # Reader should be able to complete within reasonable time
        # even while prune is running
        await asyncio.wait_for(
            manager.get_recent(ctx, limit=5, channel=ArchiveChannel.CONTEXT),
            timeout=2.0,
        )

    prune = asyncio.create_task(prune_task())
    try:
        await asyncio.wait_for(reader_task(), timeout=3.0)
    except asyncio.TimeoutError:
        pytest.fail("Reader was starved by prune_consumed_pairs() — lock held too long")
    finally:
        await prune_done.wait()
        await prune


async def test_aio_rwlock_writer_releases_on_cancel():
    """AioRWLock must release write lock when the holding task is cancelled.

    Regression guard: ensures that cancellation of a prune or append task
    does not leak the write lock, which would deadlock all subsequent access.
    """
    lock = AioRWLock()
    acquired = asyncio.Event()

    async def holder():
        async with lock.write():
            acquired.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(holder())
    await acquired.wait()

    # Cancel the holder
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Lock must be free — a new acquirer should succeed immediately
    async with asyncio.timeout(0.5):
        async with lock.read():
            pass


async def test_aio_rwlock_writer_releases_on_exception():
    """AioRWLock must release write lock when the holding task raises.

    Regression guard: ensures exceptions inside append_bundle or prune
    do not leak the lock.
    """
    lock = AioRWLock()

    async def failing_holder():
        async with lock.write():
            raise RuntimeError("intentional failure")

    with pytest.raises(RuntimeError, match="intentional failure"):
        await failing_holder()

    # Lock must be free
    async with asyncio.timeout(0.5):
        async with lock.read():
            pass
