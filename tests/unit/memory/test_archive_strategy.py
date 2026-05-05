from __future__ import annotations

from framework.memory.archive import PreserveSummaryArchiveStrategy, SemanticArchiveStrategy
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


async def test_semantic_archive_skips_when_sanitize_empty():
    """No archive write when sanitize collapses everything to empty list."""
    registry = InMemoryStoreRegistry()
    archive = MemoryLayerFactory.single_user(registry=registry).archive
    ctx = MemoryContext(session_id="semantic-empty2")
    strategy = SemanticArchiveStrategy()

    # Empty pruned list → sanitize returns [] → no archive write
    await strategy.archive(
        ctx,
        [],
        summary="",  # no LLM summary
        history_manager=archive,
    )

    entries = await archive.get_recent(ctx, limit=10)
    assert len(entries) == 0, f"expected no entries, got {len(entries)}"


async def test_semantic_archive_skips_empty_summary_string():
    """Empty string summary also skipped (no write)."""
    registry = InMemoryStoreRegistry()
    archive = MemoryLayerFactory.single_user(registry=registry).archive
    ctx = MemoryContext(session_id="semantic-empty3")
    strategy = SemanticArchiveStrategy()

    # summary="" with empty pruned list → no entry
    await strategy.archive(
        ctx,
        [],
        summary="",
        history_manager=archive,
    )

    entries = await archive.get_recent(ctx, limit=10)
    assert len(entries) == 0, f"expected no entries, got {len(entries)}"


async def test_semantic_archive_skips_whitespace_summary_string():
    """Whitespace-only summary is not semantic content and should not be written."""
    registry = InMemoryStoreRegistry()
    archive = MemoryLayerFactory.single_user(registry=registry).archive
    ctx = MemoryContext(session_id="semantic-empty-whitespace")
    strategy = SemanticArchiveStrategy()

    await strategy.archive(
        ctx,
        [],
        summary=" " * 80,
        history_manager=archive,
    )

    entries = await archive.get_recent(ctx, limit=10)
    assert len(entries) == 0, f"expected no entries, got {len(entries)}"


async def test_semantic_archive_writes_sanitized_fallback():
    """When sanitized messages exist, a fallback entry is written."""
    registry = InMemoryStoreRegistry()
    archive = MemoryLayerFactory.single_user(registry=registry).archive
    ctx = MemoryContext(session_id="semantic-fallback")
    strategy = SemanticArchiveStrategy()

    await strategy.archive(
        ctx,
        [{"role": "user", "content": "what is the weather?"}],
        summary="",  # no LLM summary → fallback path
        history_manager=archive,
    )

    entries = await archive.get_recent(ctx, limit=10)
    assert len(entries) == 1
    assert entries[0].metadata["source"] == "sanitized_fallback"
