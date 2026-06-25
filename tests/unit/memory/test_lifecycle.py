"""Tests for lifecycle and maintenance policies (Phase 6).

Covers: DefaultMemoryMaintenancePolicy, retention policies,
and DreamEngine cursor semantics.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.memory.core.layers import MemoryLayerSet
from modex_agent.memory.core.models import ArchiveEntry
from modex_agent.core.scope import (
    MemoryContext,
    MemoryLayerName,
    ScopeRecord,
    SessionScope,
    UserScope,
)
from modex_agent.memory.layers.archive import ScopedArchiveMemoryManager
from modex_agent.memory.layers.config import (
    ArchiveMemoryConfig,
    KnowledgeMemoryConfig,
    SessionMemoryConfig,
)
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.layers.knowledge import ScopedKnowledgeMemoryManager
from modex_agent.memory.layers.session import ScopedSessionMemoryManager
from modex_agent.memory.lifecycle import (
    DefaultArchiveRetentionPolicy,
    DefaultKnowledgeRetentionPolicy,
    DefaultMemoryMaintenancePolicy,
    MaintenanceResult,
)
from modex_agent.memory.registry.in_memory import InMemoryStoreRegistry


def _make_layer_set() -> MemoryLayerSet:
    registry = InMemoryStoreRegistry()
    session = ScopedSessionMemoryManager(
        lambda ctx: asyncio.coroutine(lambda: None)() or registry.resolve(
            layer=MemoryLayerName.SESSION,
            scope=MagicMock(get_scope_key=MagicMock(return_value="default")),
            context=ctx,
        ),
        SessionMemoryConfig(),
    )
    archive = ScopedArchiveMemoryManager(
        lambda ctx: asyncio.coroutine(lambda: None)() or registry.resolve(
            layer=MemoryLayerName.ARCHIVE,
            scope=MagicMock(get_scope_key=MagicMock(return_value="default")),
            context=ctx,
        ),
        ArchiveMemoryConfig(),
    )
    knowledge = ScopedKnowledgeMemoryManager(
        lambda ctx: asyncio.coroutine(lambda: None)() or registry.resolve(
            layer=MemoryLayerName.KNOWLEDGE,
            scope=MagicMock(get_scope_key=MagicMock(return_value="default")),
            context=ctx,
        ),
        KnowledgeMemoryConfig(),
    )
    return MemoryLayerSet(session=session, archive=archive, knowledge=knowledge)


# ── Maintenance ───────────────────────────────────────────────────────────────


class TestDefaultMemoryMaintenancePolicy:
    @pytest.mark.asyncio
    async def test_scan_once_returns_empty_when_no_policies(self):
        policy = DefaultMemoryMaintenancePolicy()
        registry = AsyncMock(spec=InMemoryStoreRegistry)
        layers = MagicMock(spec=MemoryLayerSet)
        results = await policy.scan_once(registry=registry, layers=layers)
        assert results == []

    @pytest.mark.asyncio
    async def test_scan_once_returns_empty_when_no_archive(self):
        policy = DefaultMemoryMaintenancePolicy()
        registry = AsyncMock(spec=InMemoryStoreRegistry)
        layers = MemoryLayerSet(session=AsyncMock(), archive=None)
        results = await policy.scan_once(registry=registry, layers=layers)
        assert results == []

    @pytest.mark.asyncio
    async def test_scan_once_handles_list_records_failure(self):
        policy = DefaultMemoryMaintenancePolicy()
        registry = AsyncMock(spec=InMemoryStoreRegistry)
        registry.list_records = AsyncMock(side_effect=RuntimeError("db down"))
        layers = MemoryLayerSet(session=AsyncMock(), archive=AsyncMock())
        results = await policy.scan_once(registry=registry, layers=layers)
        assert results == []

    @pytest.mark.asyncio
    async def test_scan_once_runs_archive_retention(self):
        registry = InMemoryStoreRegistry()
        layer_set = MemoryLayerFactory.single_user(registry=registry)
        ctx = MemoryContext(session_id="s1", user_id="u1")

        # Seed archive with 3 entries
        for i in range(3):
            await layer_set.archive.append(
                ctx,
                ArchiveEntry(summary=f"entry {i}", entry_id=i, created_at=datetime.now()),
            )

        retention = DefaultArchiveRetentionPolicy(max_entries=2)
        policy = DefaultMemoryMaintenancePolicy(archive_retention_policy=retention)

        results = await policy.scan_once(registry=registry, layers=layer_set)

        assert any(r.task == "archive_retention" and r.success for r in results)
        recent = await layer_set.archive.get_recent(ctx, limit=10)
        assert len(recent) == 2

    @pytest.mark.asyncio
    async def test_scan_once_archive_retention_prunes_by_max_age_days(self):
        from datetime import datetime, timedelta

        registry = InMemoryStoreRegistry()
        layer_set = MemoryLayerFactory.single_user(registry=registry)
        ctx = MemoryContext(session_id="s1", user_id="u1")

        old_time = datetime.now() - timedelta(days=10)
        await layer_set.archive.append(
            ctx,
            ArchiveEntry(summary="old entry", entry_id=1, created_at=old_time),
        )
        await layer_set.archive.append(
            ctx,
            ArchiveEntry(summary="new entry", entry_id=2, created_at=datetime.now()),
        )

        retention = DefaultArchiveRetentionPolicy(max_age_days=5)
        policy = DefaultMemoryMaintenancePolicy(archive_retention_policy=retention)

        results = await policy.scan_once(registry=registry, layers=layer_set)

        assert any(r.task == "archive_retention" and r.success for r in results)
        recent = await layer_set.archive.get_recent(ctx, limit=10)
        assert len(recent) == 1
        assert recent[0].summary == "new entry"

    @pytest.mark.asyncio
    async def test_scan_once_archive_retention_handles_failure(self):
        registry = AsyncMock(spec=InMemoryStoreRegistry)
        registry.list_records = AsyncMock(return_value=[
            ScopeRecord(
                scope_key="u1",
                layer=MemoryLayerName.ARCHIVE,
                context=MemoryContext(session_id="s1", user_id="u1"),
                storage_path="memory://archive/u1",
            )
        ])
        registry.resolve = AsyncMock(side_effect=RuntimeError("storage broken"))

        retention = DefaultArchiveRetentionPolicy(max_entries=5)
        policy = DefaultMemoryMaintenancePolicy(archive_retention_policy=retention)
        layers = MemoryLayerSet(session=AsyncMock(), archive=AsyncMock())

        results = await policy.scan_once(registry=registry, layers=layers)

        assert any(
            r.task == "archive_retention" and not r.success and "storage broken" in (r.detail or "")
            for r in results
        )

    @pytest.mark.asyncio
    async def test_scan_once_knowledge_eviction_prunes_stale_files(self):
        from datetime import UTC, datetime, timedelta

        registry = InMemoryStoreRegistry()
        layer_set = MemoryLayerFactory.single_user(registry=registry)
        ctx = MemoryContext(session_id="s1", user_id="u1")

        # Seed knowledge with permanent files + stale MEMORY.md
        knowledge_storage = await registry.resolve(
            layer=MemoryLayerName.KNOWLEDGE,
            scope=UserScope(),
            context=ctx,
        )
        await knowledge_storage.set("SOUL.md", "soul content")
        await knowledge_storage.set("USER.md", "user content")
        await knowledge_storage.set("MEMORY.md", "memory content")

        # Changelog with old timestamp for MEMORY.md (stale)
        old_time = (datetime.now(UTC) - timedelta(days=20)).isoformat()
        await knowledge_storage.append_log({
            "file": "MEMORY.md",
            "mode": "append",
            "reason": "test",
            "created_at": old_time,
        })

        retention = DefaultKnowledgeRetentionPolicy(stale_days=14)
        policy = DefaultMemoryMaintenancePolicy(knowledge_retention_policy=retention)

        results = await policy.scan_once(registry=registry, layers=layer_set)

        assert any(r.task == "knowledge_eviction" and r.success for r in results)
        keys = await knowledge_storage.list_keys()
        assert "SOUL.md" in keys
        assert "USER.md" in keys
        assert "MEMORY.md" not in keys

    @pytest.mark.asyncio
    async def test_scan_once_knowledge_eviction_handles_failure(self):
        registry = AsyncMock(spec=InMemoryStoreRegistry)
        registry.list_records = AsyncMock(return_value=[
            ScopeRecord(
                scope_key="u1",
                layer=MemoryLayerName.KNOWLEDGE,
                context=MemoryContext(session_id="s1", user_id="u1"),
                storage_path="memory://knowledge/u1",
            )
        ])
        registry.resolve = AsyncMock(side_effect=RuntimeError("storage broken"))

        retention = DefaultKnowledgeRetentionPolicy(stale_days=14)
        policy = DefaultMemoryMaintenancePolicy(knowledge_retention_policy=retention)
        layers = MemoryLayerSet(session=AsyncMock(), archive=AsyncMock(), knowledge=AsyncMock())

        results = await policy.scan_once(registry=registry, layers=layers)

        assert any(
            r.task == "knowledge_eviction" and not r.success and "storage broken" in (r.detail or "")
            for r in results
        )


# ── Retention ───────────────────────────────────────────────────────────────


class TestArchiveRetentionPolicy:
    @pytest.mark.asyncio
    async def test_default_max_entries(self):
        policy = DefaultArchiveRetentionPolicy(max_entries=500)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        assert await policy.get_max_entries(ctx) == 500

    @pytest.mark.asyncio
    async def test_default_max_age_days_none(self):
        policy = DefaultArchiveRetentionPolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        assert await policy.get_max_age_days(ctx) is None

    @pytest.mark.asyncio
    async def test_default_max_age_days_set(self):
        policy = DefaultArchiveRetentionPolicy(max_age_days=90)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        assert await policy.get_max_age_days(ctx) == 90


class TestKnowledgeRetentionPolicy:
    def test_soul_and_user_are_permanent(self):
        policy = DefaultKnowledgeRetentionPolicy()
        assert policy.is_permanent_file("SOUL.md") is True
        assert policy.is_permanent_file("USER.md") is True
        assert policy.is_permanent_file("MEMORY.md") is False
        assert policy.is_permanent_file("custom.md") is False

    def test_memory_stale_threshold(self):
        policy = DefaultKnowledgeRetentionPolicy(stale_days=14)
        assert policy.get_stale_threshold_days("MEMORY.md") == 14
        assert policy.get_stale_threshold_days("SOUL.md") is None
        assert policy.get_stale_threshold_days("USER.md") is None

    def test_custom_stale_days(self):
        policy = DefaultKnowledgeRetentionPolicy(stale_days=30)
        assert policy.get_stale_threshold_days("MEMORY.md") == 30


# ── MaintenanceResult ───────────────────────────────────────────────────────


class TestMaintenanceResult:
    def test_success_result(self):
        result = MaintenanceResult(scope_key="s1", task="idle_compact", success=True)
        assert result.success is True
        assert result.detail is None

    def test_failure_result(self):
        result = MaintenanceResult(
            scope_key="s1", task="idle_compact", success=False, detail="error msg"
        )
        assert result.success is False
        assert result.detail == "error msg"


# ── Archive FIFO eviction ───────────────────────────────────────────────────


class TestArchiveRetentionFifoEviction:
    """FIFO eviction via DirArchiveStorage (max_archive_total)."""

    @pytest.mark.asyncio
    async def test_scan_once_fifo_eviction_deletes_oldest_consumed(self, tmp_path):
        """Oldest consumed archives are deleted when total exceeds max_archive_total."""
        from modex_agent.memory.stores.dir_archive import DirArchiveStorage

        archive_dir = tmp_path / "archives"
        storage = DirArchiveStorage(archive_dir)

        # Create 5 archives with content
        for aid in [1, 2, 3, 4, 5]:
            d = archive_dir / str(aid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "context.md").write_text(f"entry {aid}", encoding="utf-8")
            (d / "knowledge.md").write_text(f"knowledge {aid}", encoding="utf-8")
            (d / "index.md").write_text(f"index {aid}", encoding="utf-8")

        # consumed=3 means archives 1,2,3 are safe to delete
        await storage.write_archive_state({
            "next_archive_id": 6,
            "knowledge_consumed_archive_id": 3,
        })

        # Mock registry returns a non-DirArchiveStorage (simulating DefaultScopedStorage)
        registry = AsyncMock(spec=InMemoryStoreRegistry)
        registry.list_records = AsyncMock(return_value=[
            ScopeRecord(
                scope_key="u1",
                layer=MemoryLayerName.ARCHIVE,
                context=MemoryContext(session_id="s1", user_id="u1"),
                storage_path=str(archive_dir),
            )
        ])
        # Mock a MemoryStorage-like object with channel + maintenance methods
        mock_storage = MagicMock()
        mock_storage.read_channel_logs = AsyncMock(return_value=[{"cursor": 1}])
        mock_storage.save_channel_logs = AsyncMock()
        mock_storage.read_archive_state = AsyncMock(return_value={"knowledge_consumed_archive_id": 3})
        mock_storage.prune_to_max = AsyncMock(return_value=0)
        mock_storage.cleanup_empty_dirs = AsyncMock(return_value=0)
        registry.resolve = AsyncMock(return_value=mock_storage)

        # Mock archive layer that returns the directory path
        mock_archive = AsyncMock()
        mock_archive.get_storage_path = AsyncMock(return_value=archive_dir)

        layers = MagicMock(spec=MemoryLayerSet)
        layers.archive = mock_archive

        retention = DefaultArchiveRetentionPolicy(max_archive_total=2)
        policy = DefaultMemoryMaintenancePolicy(archive_retention_policy=retention)

        results = await policy.scan_once(registry=registry, layers=layers)

        assert any(r.task == "archive_retention" and r.success for r in results)

        # Deletable IDs <= consumed (3): [1, 2, 3]
        # max_total=2 means keep newest 2, delete oldest: 1
        assert not (archive_dir / "1").exists()
        assert (archive_dir / "2").exists()
        assert (archive_dir / "3").exists()
        assert (archive_dir / "4").exists()
        assert (archive_dir / "5").exists()

    @pytest.mark.asyncio
    async def test_scan_once_fifo_preserves_unconsumed_archives(self, tmp_path):
        """Archives above the consumed cursor are never deleted."""
        from modex_agent.memory.stores.dir_archive import DirArchiveStorage

        archive_dir = tmp_path / "archives"
        storage = DirArchiveStorage(archive_dir)

        for aid in [1, 2, 3, 4, 5]:
            d = archive_dir / str(aid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "context.md").write_text(f"entry {aid}", encoding="utf-8")
            (d / "knowledge.md").write_text(f"knowledge {aid}", encoding="utf-8")
            (d / "index.md").write_text(f"index {aid}", encoding="utf-8")

        # consumed=0 means NONE are safe to delete
        await storage.write_archive_state({
            "next_archive_id": 6,
            "knowledge_consumed_archive_id": 0,
        })

        registry = AsyncMock(spec=InMemoryStoreRegistry)
        registry.list_records = AsyncMock(return_value=[
            ScopeRecord(
                scope_key="u1",
                layer=MemoryLayerName.ARCHIVE,
                context=MemoryContext(session_id="s1", user_id="u1"),
                storage_path=str(archive_dir),
            )
        ])
        # Mock a MemoryStorage-like object with channel + maintenance methods
        mock_storage = MagicMock()
        mock_storage.read_channel_logs = AsyncMock(return_value=[{"cursor": 1}])
        mock_storage.save_channel_logs = AsyncMock()
        mock_storage.read_archive_state = AsyncMock(return_value={"knowledge_consumed_archive_id": 0})
        mock_storage.prune_to_max = AsyncMock(return_value=0)
        mock_storage.cleanup_empty_dirs = AsyncMock(return_value=0)
        registry.resolve = AsyncMock(return_value=mock_storage)

        mock_archive = AsyncMock()
        mock_archive.get_storage_path = AsyncMock(return_value=archive_dir)

        layers = MagicMock(spec=MemoryLayerSet)
        layers.archive = mock_archive

        retention = DefaultArchiveRetentionPolicy(max_archive_total=2)
        policy = DefaultMemoryMaintenancePolicy(archive_retention_policy=retention)

        results = await policy.scan_once(registry=registry, layers=layers)

        # No archives deleted because none are consumed
        for aid in [1, 2, 3, 4, 5]:
            assert (archive_dir / str(aid)).exists(), f"archive {aid} should be preserved"
