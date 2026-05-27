"""Tests for DreamEngineConfig dual trigger fields and DreamEngine dual trigger logic."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.ioc.configs.memory import DreamEngineConfig
from framework.memory.consolidation.dream_engine import DreamEngine
from framework.memory.core.models import ArchiveEntry, UnprocessedResult


class TestDreamEngineConfigHasDualTriggerFields:
    """Verify the three new trigger/batch fields exist on DreamEngineConfig."""

    def test_dream_engine_config_has_dual_trigger_fields(self):
        cfg = DreamEngineConfig()
        assert hasattr(cfg, "min_archive_count"), "missing min_archive_count"
        assert hasattr(cfg, "max_archive_count"), "missing max_archive_count"
        assert hasattr(cfg, "max_batch_size"), "missing max_batch_size"

    def test_dream_engine_config_defaults(self):
        cfg = DreamEngineConfig()
        assert cfg.interval == 1200
        assert cfg.min_archive_count == 5
        assert cfg.max_archive_count == 30
        assert cfg.max_batch_size == 20

    def test_dream_engine_config_custom_values(self):
        cfg = DreamEngineConfig(
            enabled=True,
            interval=300,
            min_archive_count=10,
            max_archive_count=50,
            max_batch_size=15,
        )
        assert cfg.enabled is True
        assert cfg.interval == 300
        assert cfg.min_archive_count == 10
        assert cfg.max_archive_count == 50
        assert cfg.max_batch_size == 15


def _make_engine(
    min_archive_count: int = 5,
    max_archive_count: int = 30,
    max_batch_size: int = 20,
) -> DreamEngine:
    """Create a DreamEngine with mocked providers for trigger testing."""
    llm = MagicMock()
    history_mgr = AsyncMock()
    long_term_mgr = AsyncMock()
    summarizer = AsyncMock()
    return DreamEngine(
        llm_provider=llm,
        history_manager=history_mgr,
        long_term_manager=long_term_mgr,
        max_batch_size=max_batch_size,
        min_archive_count=min_archive_count,
        max_archive_count=max_archive_count,
        summarizer=summarizer,
    )


def _make_entries(count: int, start_id: int = 1) -> list[ArchiveEntry]:
    """Create a list of ArchiveEntry objects for testing."""
    return [
        ArchiveEntry(
            summary=f"test entry {i}",
            entry_id=start_id + i,
            created_at=datetime(2026, 1, 1, 0, 0, 0),
        )
        for i in range(count)
    ]


class TestDreamEngineDualTrigger:
    """Test dual trigger logic: skip below min, trigger above max, normal in between."""

    @pytest.mark.asyncio
    async def test_dream_engine_skips_below_min_threshold(self):
        """When archive_count < min_archive_count, should return False WITHOUT advancing cursor or pruning.

        The purpose of the min threshold is to accumulate enough data before consolidating.
        Advancing the cursor would discard entries that were never processed.
        """
        engine = _make_engine(min_archive_count=5, max_archive_count=30)
        entries = _make_entries(3)
        unprocessed = UnprocessedResult(cursor=3, entries=entries)
        engine.history_manager.get_unprocessed = AsyncMock(return_value=unprocessed)
        engine.history_manager.commit_cursor = AsyncMock()
        engine.history_manager.prune_consumed_pairs = AsyncMock()

        context = MagicMock()
        result = await engine.run(context)

        assert result is False
        # Cursor must NOT advance — entries should remain unprocessed for next run
        engine.history_manager.commit_cursor.assert_not_awaited()
        engine.history_manager.prune_consumed_pairs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dream_engine_triggers_above_max_threshold(self):
        """When archive_count > max_archive_count, should proceed to consolidation."""
        engine = _make_engine(min_archive_count=5, max_archive_count=30, max_batch_size=20)
        entries = _make_entries(35)
        unprocessed = UnprocessedResult(cursor=35, entries=entries)
        engine.history_manager.get_unprocessed = AsyncMock(return_value=unprocessed)
        engine.history_manager.commit_cursor = AsyncMock()
        engine.history_manager.prune_consumed_pairs = AsyncMock()

        # Mock consolidate to avoid LLM calls
        from framework.memory.core.consolidation import ConsolidationResult
        engine.consolidate = AsyncMock(
            return_value=ConsolidationResult(success=True, reasoning="test")
        )

        # Mock long_term_manager.get_all for the normal path
        engine.long_term_manager.get_all = AsyncMock()
        engine.long_term_manager.get_all.return_value = MagicMock(
            soul="", user="", memory="", custom={}
        )
        engine.long_term_manager.apply_update = AsyncMock()

        context = MagicMock()
        result = await engine.run(context)

        # Should have called consolidate (normal processing path was entered)
        engine.consolidate.assert_awaited_once()
        assert result is True

    @pytest.mark.asyncio
    async def test_dream_engine_respects_batch_size(self):
        """When entries exceed max_batch_size, only max_batch_size entries should be processed."""
        engine = _make_engine(min_archive_count=5, max_archive_count=100, max_batch_size=20)
        entries = _make_entries(50)
        unprocessed = UnprocessedResult(cursor=50, entries=entries)
        engine.history_manager.get_unprocessed = AsyncMock(return_value=unprocessed)
        engine.history_manager.commit_cursor = AsyncMock()
        engine.history_manager.prune_consumed_pairs = AsyncMock()

        # Mock consolidate to inspect what entries were passed
        from framework.memory.core.consolidation import ConsolidationResult
        engine.consolidate = AsyncMock(
            return_value=ConsolidationResult(success=True, reasoning="test")
        )

        # Mock long_term_manager.get_all for the normal path
        engine.long_term_manager.get_all = AsyncMock()
        engine.long_term_manager.get_all.return_value = MagicMock(
            soul="", user="", memory="", custom={}
        )
        engine.long_term_manager.apply_update = AsyncMock()

        context = MagicMock()
        result = await engine.run(context)

        # consolidate should be called with at most max_batch_size entries
        assert engine.consolidate.await_count == 1
        new_entries_arg = engine.consolidate.call_args[1]["new_entries"]
        assert len(new_entries_arg) <= 20
