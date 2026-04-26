"""Tests for lifecycle and maintenance policies (Phase 6).

Covers: MemoryLifecyclePolicy, MemoryMaintenancePolicy, retention policies,
DreamEngine cursor semantics, and ConsolidationEngine ABC.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from datetime import datetime

from framework.memory.core.layers import MemoryLayerSet
from framework.memory.core.models import ArchiveEntry
from framework.memory.core.scope import (
    MemoryContext,
    MemoryLayerName,
    ScopeRecord,
    SessionScope,
    UserScope,
)
from framework.memory.layers.archive import ScopedArchiveMemoryManager
from framework.memory.layers.config import (
    ArchiveMemoryConfig,
    KnowledgeMemoryConfig,
    SessionMemoryConfig,
)
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.layers.knowledge import ScopedKnowledgeMemoryManager
from framework.memory.layers.session import ScopedSessionMemoryManager
from framework.memory.lifecycle import (
    DefaultArchiveRetentionPolicy,
    DefaultKnowledgeRetentionPolicy,
    DefaultMemoryLifecyclePolicy,
    DefaultMemoryMaintenancePolicy,
    DefaultSessionRetentionPolicy,
    MaintenanceResult,
)
from framework.memory.registry.in_memory import InMemoryStoreRegistry


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


# ── Lifecycle ───────────────────────────────────────────────────────────────


class TestDefaultMemoryLifecyclePolicy:
    @pytest.mark.asyncio
    async def test_on_turn_start_is_noop(self):
        policy = DefaultMemoryLifecyclePolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        layers = MagicMock(spec=MemoryLayerSet)
        await policy.on_turn_start(ctx, layers)

    @pytest.mark.asyncio
    async def test_on_turn_end_is_noop(self):
        policy = DefaultMemoryLifecyclePolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        layers = MagicMock(spec=MemoryLayerSet)
        await policy.on_turn_end(ctx, layers)

    @pytest.mark.asyncio
    async def test_on_session_end_is_noop(self):
        policy = DefaultMemoryLifecyclePolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        layers = MagicMock(spec=MemoryLayerSet)
        await policy.on_session_end(ctx, layers)

    @pytest.mark.asyncio
    async def test_on_messages_added_triggers_coordinator(self):
        coordinator = AsyncMock()
        coordinator.maybe_compress = AsyncMock()
        policy = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        archive = AsyncMock()
        layers = MemoryLayerSet(session=AsyncMock(), archive=archive)
        await policy.on_messages_added(ctx, layers)
        coordinator.maybe_compress.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_messages_added_skips_when_no_archive(self):
        coordinator = AsyncMock()
        policy = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        layers = MemoryLayerSet(session=AsyncMock(), archive=None)
        await policy.on_messages_added(ctx, layers)
        coordinator.maybe_compress.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_messages_added_handles_coordinator_failure(self):
        coordinator = AsyncMock()
        coordinator.maybe_compress = AsyncMock(side_effect=RuntimeError("boom"))
        policy = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        layers = MemoryLayerSet(session=AsyncMock(), archive=AsyncMock())
        await policy.on_messages_added(ctx, layers)

    @pytest.mark.asyncio
    async def test_on_messages_added_skips_when_no_coordinator(self):
        policy = DefaultMemoryLifecyclePolicy()
        ctx = MemoryContext(session_id="s1", user_id="u1")
        layers = MemoryLayerSet(session=AsyncMock(), archive=AsyncMock())
        await policy.on_messages_added(ctx, layers)


# ── Maintenance ─────────────────────────────────────────────────────────────


class TestDefaultMemoryMaintenancePolicy:
    @pytest.mark.asyncio
    async def test_scan_once_returns_empty_when_no_coordinator(self):
        policy = DefaultMemoryMaintenancePolicy()
        registry = AsyncMock(spec=InMemoryStoreRegistry)
        layers = MagicMock(spec=MemoryLayerSet)
        results = await policy.scan_once(registry=registry, layers=layers)
        assert results == []

    @pytest.mark.asyncio
    async def test_scan_once_returns_empty_when_no_archive(self):
        coordinator = AsyncMock()
        policy = DefaultMemoryMaintenancePolicy(compression_coordinator=coordinator)
        registry = AsyncMock(spec=InMemoryStoreRegistry)
        layers = MemoryLayerSet(session=AsyncMock(), archive=None)
        results = await policy.scan_once(registry=registry, layers=layers)
        assert results == []

    @pytest.mark.asyncio
    async def test_scan_once_handles_list_records_failure(self):
        coordinator = AsyncMock()
        policy = DefaultMemoryMaintenancePolicy(compression_coordinator=coordinator)
        registry = AsyncMock(spec=InMemoryStoreRegistry)
        registry.list_records = AsyncMock(side_effect=RuntimeError("db down"))
        layers = MemoryLayerSet(session=AsyncMock(), archive=AsyncMock())
        results = await policy.scan_once(registry=registry, layers=layers)
        assert results == []

    @pytest.mark.asyncio
    async def test_scan_once_uses_last_activity_in_session_storage(self):
        import time

        registry = InMemoryStoreRegistry()
        layer_set = MemoryLayerFactory.single_user(registry=registry)
        ctx = MemoryContext(session_id="idle-session")
        await layer_set.session.add_messages(ctx, [{"role": "user", "content": "old"}])
        storage = await registry.resolve(
            layer=MemoryLayerName.SESSION,
            scope=SessionScope(),
            context=ctx,
        )
        await storage.set(".last_activity", time.time() - 3600)

        coordinator = AsyncMock()
        coordinator.maybe_compress = AsyncMock()
        policy = DefaultMemoryMaintenancePolicy(
            idle_threshold_seconds=10,
            compression_coordinator=coordinator,
        )

        results = await policy.scan_once(registry=registry, layers=layer_set)

        assert [result.task for result in results] == ["idle_compact"]
        coordinator.maybe_compress.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_add_messages_updates_last_activity(self):
        registry = InMemoryStoreRegistry()
        layer_set = MemoryLayerFactory.single_user(registry=registry)
        ctx = MemoryContext(session_id="active-session")

        await layer_set.session.add_messages(ctx, [{"role": "user", "content": "hello"}])

        storage = await registry.resolve(
            layer=MemoryLayerName.SESSION,
            scope=SessionScope(),
            context=ctx,
        )
        assert isinstance(await storage.get(".last_activity"), float)

    @pytest.mark.asyncio
    async def test_scan_once_runs_archive_retention_without_coordinator(self):
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
        import time
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
        import time
        from datetime import datetime, timedelta, UTC

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


class TestSessionRetentionPolicy:
    @pytest.mark.asyncio
    async def test_default_never_compacts(self):
        policy = DefaultSessionRetentionPolicy()
        assert await policy.should_compact(storage=AsyncMock(), context=MagicMock()) is False

    @pytest.mark.asyncio
    async def test_default_never_evicts_checkpoint(self):
        policy = DefaultSessionRetentionPolicy()
        assert await policy.should_evict_checkpoint(storage=AsyncMock(), context=MagicMock()) is False


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


# ── ConsolidationEngine ABC ─────────────────────────────────────────────────


class TestConsolidationEngineABC:
    def test_cannot_instantiate_directly(self):
        from framework.memory.core.consolidation import ConsolidationEngine

        with pytest.raises(TypeError):
            ConsolidationEngine()

    def test_concrete_impl_must_implement_run(self):
        from framework.memory.core.consolidation import ConsolidationEngine

        class Incomplete(ConsolidationEngine):
            pass

        with pytest.raises(TypeError):
            Incomplete()

    def test_concrete_impl_works(self):
        from framework.memory.core.consolidation import ConsolidationEngine

        class Complete(ConsolidationEngine):
            async def run(self, context):
                return True

            async def consolidate(self, scope_key, new_entries, existing_memories):
                from framework.memory.core.consolidation import ConsolidationResult
                return ConsolidationResult.empty()

        engine = Complete()
        assert engine is not None
