from __future__ import annotations

from framework.memory.archive import PreserveSummaryArchiveStrategy
from framework.memory.core.scope import MemoryContext
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.registry.in_memory import InMemoryStoreRegistry


async def test_preserve_summary_archive_strategy_uses_archive_entry_api():
    registry = InMemoryStoreRegistry()
    archive = MemoryLayerFactory.single_user(registry=registry).archive
    ctx = MemoryContext(session_id="archive-strategy")
    strategy = PreserveSummaryArchiveStrategy()

    await strategy.archive(
        ctx,
        [{"role": "user", "content": "hello"}],
        "summary",
        archive,
    )

    entries = await archive.get_recent(ctx)
    assert len(entries) == 1
    assert entries[0].summary == "summary"
    assert entries[0].metadata["source"] == "compression_summary"
