"""Tests for DreamEngineConfig dual trigger fields and DreamEngine dual trigger logic."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.agents.summarizer.outcomes import ConsolidationOutcome
from modex_agent.ioc.configs.memory import DreamEngineConfig
from modex_agent.memory.consolidation.dream_engine import DreamEngine
from modex_agent.memory.core.models import ArchiveEntry, UnprocessedResult


class TestDreamEngineConfigHasConsumePerRunField:
    """Verify max_consume_per_run field exists on DreamEngineConfig."""

    def test_dream_engine_config_has_consume_per_run_field(self):
        cfg = DreamEngineConfig()
        assert hasattr(cfg, "max_consume_per_run"), "missing max_consume_per_run"

    def test_dream_engine_config_defaults(self):
        cfg = DreamEngineConfig()
        assert cfg.interval == 1200
        assert cfg.max_consume_per_run == 3


def test_dream_engine_default_max_consume_per_run() -> None:
    """DreamEngine defaults to max_consume_per_run=3."""
    engine = DreamEngine(
        history_manager=MagicMock(),
        long_term_manager=MagicMock(),
    )
    assert engine.max_consume_per_run == 3


def test_dream_engine_config_custom_values() -> None:
    cfg = DreamEngineConfig(
        enabled=True,
        interval=300,
        max_consume_per_run=15,
    )
    assert cfg.enabled is True
    assert cfg.interval == 300
    assert cfg.max_consume_per_run == 15


def _make_engine(
    max_consume_per_run: int = 20,
) -> DreamEngine:
    """Create a DreamEngine with mocked providers for trigger testing."""
    history_mgr = AsyncMock()
    long_term_mgr = AsyncMock()
    return DreamEngine(
        history_manager=history_mgr,
        long_term_manager=long_term_mgr,
        max_consume_per_run=max_consume_per_run,
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


class TestDreamEngineRun:
    """Test DreamEngine.run() — no-op when no consolidator, processes with consolidator, batch limits."""

    @pytest.mark.asyncio
    async def test_run_returns_false_when_no_consolidator(self):
        """Without a consolidator, run() returns False immediately."""
        engine = _make_engine()
        entries = _make_entries(3)
        unprocessed = UnprocessedResult(cursor=3, entries=entries)
        engine.history_manager.get_unprocessed = AsyncMock(return_value=unprocessed)
        engine.history_manager.commit_cursor = AsyncMock()
        engine.history_manager.prune_consumed_pairs = AsyncMock()

        context = MagicMock()
        result = await engine.run(context)

        assert result is False
        engine.history_manager.commit_cursor.assert_not_awaited()
        engine.history_manager.prune_consumed_pairs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_processes_with_consolidator(self):
        """With a consolidator, run() processes entries and advances cursor."""
        mock_consolidator = AsyncMock()
        mock_consolidator.consolidate.return_value = ConsolidationOutcome(changed=True)
        engine = _make_engine()
        engine._consolidator = mock_consolidator

        entries = _make_entries(5)
        unprocessed = UnprocessedResult(cursor=5, entries=entries)
        engine.history_manager.get_unprocessed = AsyncMock(return_value=unprocessed)
        engine.history_manager.commit_cursor = AsyncMock()
        engine.history_manager.prune_consumed_pairs = AsyncMock()

        engine.long_term_manager.get_storage_path = AsyncMock()
        engine.long_term_manager.get_storage_path.return_value = MagicMock()
        engine.history_manager.get_storage_path = AsyncMock()
        engine.history_manager.get_storage_path.return_value = MagicMock()

        context = MagicMock()
        result = await engine.run(context)

        assert result is True
        mock_consolidator.consolidate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_respects_max_consume_per_run(self):
        """Only max_consume_per_run entries are processed per invocation."""
        mock_consolidator = AsyncMock()
        mock_consolidator.consolidate.return_value = ConsolidationOutcome(changed=True)
        engine = _make_engine(max_consume_per_run=2)
        engine._consolidator = mock_consolidator

        entries = _make_entries(5)
        unprocessed = UnprocessedResult(cursor=5, entries=entries)
        engine.history_manager.get_unprocessed = AsyncMock(return_value=unprocessed)
        engine.history_manager.commit_cursor = AsyncMock()
        engine.history_manager.prune_consumed_pairs = AsyncMock()

        engine.long_term_manager.get_storage_path = AsyncMock()
        engine.long_term_manager.get_storage_path.return_value = MagicMock()
        engine.history_manager.get_storage_path = AsyncMock()
        engine.history_manager.get_storage_path.return_value = MagicMock()

        context = MagicMock()
        await engine.run(context)

        mock_consolidator.consolidate.assert_awaited_once()
        archive_ids_arg = mock_consolidator.consolidate.call_args[1]["archive_ids"]
        assert len(archive_ids_arg) == 2
        assert archive_ids_arg == [1, 2]
