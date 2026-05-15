"""Tests for lifecycle and maintenance policies (Phase 6).

Covers: MemoryLifecyclePolicy, MemoryMaintenancePolicy, retention policies,
DreamEngine cursor semantics, and ConsolidationEngine ABC.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.core.types import MessageRole
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
from framework.memory.layers.pending import PendingPrunedInputEntry
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
        session = AsyncMock()
        session.get_all_messages = AsyncMock(return_value=[])
        layers = MemoryLayerSet(session=session, archive=archive)
        await policy.on_messages_added(ctx, layers)
        coordinator.maybe_compress.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_messages_added_triggers_compression_even_without_archive(self):
        """archive=None still runs session-only compression through the default coordinator."""
        coordinator = AsyncMock()
        policy = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        session = AsyncMock()
        session.get_all_messages = AsyncMock(return_value=[])
        layers = MemoryLayerSet(session=session, archive=None)
        await policy.on_messages_added(ctx, layers)
        coordinator.maybe_compress.assert_called_once_with(
            session=layers.session, archive=None, pending=None, context=ctx,
        )

    @pytest.mark.asyncio
    async def test_on_messages_added_delegates_open_assistant_tool_call_to_coordinator(self):
        coordinator = AsyncMock()
        coordinator.maybe_compress = AsyncMock()
        policy = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        session = AsyncMock()
        session.get_all_messages = AsyncMock(
            return_value=[
                {
                    "role": str(MessageRole.ASSISTANT),
                    "content": "",
                    "tool_calls": [{"id": "call-1", "function": {"name": "search_files"}}],
                }
            ]
        )
        layers = MemoryLayerSet(session=session, archive=None)

        await policy.on_messages_added(ctx, layers)

        coordinator.maybe_compress.assert_called_once_with(
            session=layers.session, archive=None, pending=None, context=ctx,
        )

    @pytest.mark.asyncio
    async def test_on_messages_added_delegates_tool_result_to_coordinator(self):
        coordinator = AsyncMock()
        coordinator.maybe_compress = AsyncMock()
        policy = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        session = AsyncMock()
        session.get_all_messages = AsyncMock(
            return_value=[
                {
                    "role": str(MessageRole.ASSISTANT),
                    "content": "",
                    "tool_calls": [{"id": "call-1", "function": {"name": "search_files"}}],
                },
                {"role": str(MessageRole.TOOL), "tool_call_id": "call-1", "content": "result"},
            ]
        )
        layers = MemoryLayerSet(session=session, archive=None)

        await policy.on_messages_added(ctx, layers)

        coordinator.maybe_compress.assert_called_once_with(
            session=layers.session, archive=None, pending=None, context=ctx,
        )

    @pytest.mark.asyncio
    async def test_on_messages_added_runs_after_matched_tool_result(self):
        """A matched tool result is a legal boundary for compression checks.

        The coordinator still owns the final safety decision. Lifecycle must
        not defer every complete assistant/tool pair until a final plain
        assistant, otherwise long ReAct loops can exceed max_messages by a
        large margin.
        """
        coordinator = AsyncMock()
        coordinator.maybe_compress = AsyncMock()
        policy = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        session = AsyncMock()
        session.get_all_messages = AsyncMock(
            return_value=[
                {
                    "role": str(MessageRole.ASSISTANT),
                    "content": "",
                    "tool_calls": [{"id": "call-1", "function": {"name": "search_files"}}],
                },
                {"role": str(MessageRole.TOOL), "tool_call_id": "call-1", "content": "result"},
            ]
        )
        layers = MemoryLayerSet(session=session, archive=None)

        await policy.on_messages_added(ctx, layers)

        coordinator.maybe_compress.assert_called_once_with(
            session=layers.session, archive=None, pending=None, context=ctx,
        )

    @pytest.mark.asyncio
    async def test_on_messages_added_runs_after_final_assistant_consumes_tool_result(self):
        coordinator = AsyncMock()
        coordinator.maybe_compress = AsyncMock()
        policy = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        session = AsyncMock()
        session.get_all_messages = AsyncMock(
            return_value=[
                {
                    "role": str(MessageRole.ASSISTANT),
                    "content": "",
                    "tool_calls": [{"id": "call-1", "function": {"name": "search_files"}}],
                },
                {"role": str(MessageRole.TOOL), "tool_call_id": "call-1", "content": "result"},
                {"role": str(MessageRole.ASSISTANT), "content": "done"},
            ]
        )
        layers = MemoryLayerSet(session=session, archive=None)

        await policy.on_messages_added(ctx, layers)

        coordinator.maybe_compress.assert_called_once_with(
            session=layers.session, archive=None, pending=None, context=ctx,
        )

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

    @pytest.mark.asyncio
    async def test_completed_assistant_append_clears_pending_memory(self):
        registry = InMemoryStoreRegistry()
        layers = MemoryLayerFactory.single_user(registry=registry)
        ctx = MemoryContext(session_id="pending-clear")
        assert layers.pending is not None
        await layers.pending.append_entries(ctx, [
            PendingPrunedInputEntry.from_message(
                {"role": "user", "content": "unfinished"},
                pruned_at=100.0,
            )
        ])
        await layers.session.add_messages(ctx, [
            {"role": str(MessageRole.ASSISTANT), "content": "done"},
        ])

        await DefaultMemoryLifecyclePolicy().on_messages_added(ctx, layers)

        assert await layers.pending.get_entries(ctx) == []

    @pytest.mark.asyncio
    async def test_completed_assistant_in_appended_batch_clears_pending_memory(self):
        registry = InMemoryStoreRegistry()
        layers = MemoryLayerFactory.single_user(registry=registry)
        ctx = MemoryContext(session_id="pending-clear-batch")
        assert layers.pending is not None
        await layers.pending.append_entries(ctx, [
            PendingPrunedInputEntry.from_message(
                {"role": "user", "content": "unfinished"},
                pruned_at=100.0,
            )
        ])
        await layers.session.add_messages(ctx, [
            {"role": str(MessageRole.ASSISTANT), "content": "done"},
            {"role": str(MessageRole.USER), "content": "next"},
        ])

        await DefaultMemoryLifecyclePolicy().on_messages_added(ctx, layers)

        assert await layers.pending.get_entries(ctx) == []

    @pytest.mark.asyncio
    async def test_subagent_session_end_clears_pending_memory(self):
        registry = InMemoryStoreRegistry()
        layers = MemoryLayerFactory.single_user(registry=registry)
        ctx = MemoryContext(session_id="subagent", agent_role="subagent")
        assert layers.pending is not None
        await layers.session.add_messages(ctx, [{"role": "user", "content": "tmp"}])
        await layers.pending.append_entries(ctx, [
            PendingPrunedInputEntry.from_message(
                {"role": "user", "content": "unfinished"},
                pruned_at=100.0,
            )
        ])

        await DefaultMemoryLifecyclePolicy().on_session_end(ctx, layers)

        assert await layers.session.get_all_messages(ctx) == []
        assert await layers.pending.get_entries(ctx) == []


    @pytest.mark.asyncio
    async def test_on_messages_added_does_not_skip_for_old_stale_incomplete_tool_call(self):
        coordinator = AsyncMock()
        coordinator.maybe_compress = AsyncMock()
        policy = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
        ctx = MemoryContext(session_id="s1", user_id="u1")
        session = AsyncMock()
        session.get_all_messages = AsyncMock(
            return_value=[
                {
                    "role": str(MessageRole.ASSISTANT),
                    "content": "",
                    "tool_calls": [
                        {"id": "old-a", "function": {"name": "search_files"}},
                        {"id": "old-b", "function": {"name": "search_files"}},
                    ],
                },
                {"role": str(MessageRole.TOOL), "tool_call_id": "old-a", "content": "partial"},
                {"role": str(MessageRole.ASSISTANT), "content": "done"},
            ]
        )
        layers = MemoryLayerSet(session=session, archive=None)

        await policy.on_messages_added(ctx, layers)

        coordinator.maybe_compress.assert_called_once_with(
            session=layers.session,
            archive=None,
            pending=None,
            context=ctx,
        )


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


# ── End-to-end: bot_project cascading memory flow ──────────────────────────


async def test_full_add_messages_compression_archive_cascade():
    """Simulate bot_project flow: add_messages → lifecycle → compression → archive.

    Config: max_messages=50 (same as bot_profile short_term.max_messages).
    Adding 96 messages should trigger compression, truncate session, and
    write archive entries — all through the normal add_messages path.
    """
    from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator, SummaryStrategy
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy
    from framework.memory.registry.in_memory import InMemoryStoreRegistry

    class _Summary(SummaryStrategy):
        async def summarize(self, messages, context, reason):
            return "compressed summary"

    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="e2e-cascade")

    coordinator = DefaultMemoryCompressionCoordinator(max_messages=50, summary=_Summary())
    lifecycle = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
    system = DefaultMemorySystem(layer_set=layer_set, store_registry=registry, lifecycle_policy=lifecycle)
    await system.initialize()

    # Add 96 messages in batches (simulates real turn-by-turn flow)
    messages = [{"role": "user", "content": f"msg{i}"} for i in range(96)]
    await system.add_messages(ctx, messages)

    # Session should be truncated to ≤50 by compression
    remaining = await system.get_history(ctx, max_messages=None)
    assert len(remaining) <= 55, \
        f"session should be compressed, got {len(remaining)} messages"

    # Archive should have entries from the compressed prefix
    entries = await layer_set.archive.get_recent(ctx, limit=20)
    assert len(entries) > 0, \
        f"archive should have compression entries, got {len(entries)}"

    # Compression summary should be stored in session state
    storage = await registry.resolve(
        layer=MemoryLayerName.SESSION, scope=SessionScope(), context=ctx,
    )
    summary = await storage.get(".compression_summary")
    assert summary is not None, "compression summary should be persisted"


async def test_cascaded_compression_retains_tool_chain_integrity():
    """After compression via add_messages, tool chains in the suffix are intact."""
    from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator, SummaryStrategy
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy
    from framework.memory.registry.in_memory import InMemoryStoreRegistry

    class _Summary(SummaryStrategy):
        async def summarize(self, messages, context, reason):
            return "compressed summary"

    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="e2e-toolchain")

    coordinator = DefaultMemoryCompressionCoordinator(max_messages=8, summary=_Summary())
    lifecycle = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
    system = DefaultMemorySystem(layer_set=layer_set, store_registry=registry, lifecycle_policy=lifecycle)
    await system.initialize()

    # 6 turns with tool chains = 30 messages total
    messages: list[dict[str, object]] = []
    for i in range(6):
        messages.append({"role": "user", "content": f"q{i}"})
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"tc{i}a", "type": "function", "function": {"name": "read_file"}},
            {"id": f"tc{i}b", "type": "function", "function": {"name": "shell"}},
        ]})
        messages.append({"role": "tool", "tool_call_id": f"tc{i}a", "name": "read_file", "content": f"out{i}a"})
        messages.append({"role": "tool", "tool_call_id": f"tc{i}b", "name": "shell", "content": f"out{i}b"})
        messages.append({"role": "assistant", "content": f"answer {i}"})
    await system.add_messages(ctx, messages)

    remaining = await system.get_history(ctx, max_messages=None)
    assert len(remaining) <= 12  # compressed, with chain safety margin

    # No orphan tool results in the kept suffix
    kept_call_ids: set[str] = set()
    for m in remaining:
        d = m.to_dict()
        for tc in d.get("tool_calls", []) or []:
            if isinstance(tc, dict) and tc.get("id"):
                kept_call_ids.add(tc["id"])
    for m in remaining:
        d = m.to_dict()
        if d.get("role") == str(MessageRole.TOOL):
            assert d.get("tool_call_id") in kept_call_ids, \
                f"orphan tool result {d.get('tool_call_id')} in suffix"

    # Archive entries exist
    entries = await layer_set.archive.get_recent(ctx, limit=20)
    assert len(entries) > 0


async def test_archive_retrieval_after_compression():
    """Compressed archive entries are retrievable via get_history_entries."""
    from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator, SummaryStrategy
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy
    from framework.memory.registry.in_memory import InMemoryStoreRegistry

    class _Summary(SummaryStrategy):
        async def summarize(self, messages, context, reason):
            return "compressed summary"

    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="e2e-retrieval")

    coordinator = DefaultMemoryCompressionCoordinator(max_messages=5, summary=_Summary())
    lifecycle = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
    system = DefaultMemorySystem(layer_set=layer_set, store_registry=registry, lifecycle_policy=lifecycle)
    await system.initialize()

    # Add distinctive messages then compress
    messages = [{"role": "user", "content": f"distinctive topic {i}"} for i in range(20)]
    await system.add_messages(ctx, messages)

    # Archive should be searchable
    entries = await system.get_history_entries(ctx, limit=10, query="topic")
    assert len(entries) > 0, "archive should be searchable after compression"


# ── Regression: ScopedMessageHistory path (bot_project hot path) ─────────


async def test_scoped_message_history_triggers_compression():
    """Messages added through ScopedMessageHistory (ReAct turn path) must compress.

    This is the actually-used path in bot_project: during a ReAct turn,
    assistant/tool messages are appended via ScopedMessageHistory.append().
    Since it calls session.add_messages() directly, not
    DefaultMemorySystem.add_messages(), the lifecycle hook is skipped.
    """
    from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator, SummaryStrategy
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy
    from framework.memory.registry.in_memory import InMemoryStoreRegistry

    class _Summary(SummaryStrategy):
        async def summarize(self, messages, context, reason):
            return "compressed summary"

    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="e2e-history")

    coordinator = DefaultMemoryCompressionCoordinator(max_messages=10, summary=_Summary())
    lifecycle = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
    system = DefaultMemorySystem(
        layer_set=layer_set, store_registry=registry, lifecycle_policy=lifecycle,
    )
    await system.initialize()

    # Simulate ReAct turn: messages go through ScopedMessageHistory (bot_project hot path)
    history = system.create_message_history(ctx)
    for i in range(30):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    # Verify: compression WAS triggered by the on_messages_added callback
    stored = len(await system.get_history(ctx, max_messages=None))
    assert stored <= 20, \
        f"should compress from 60 to ≤20, got {stored}"

    # Archive must have entries from compression
    archive_entries = await layer_set.archive.get_recent(ctx, limit=20)
    assert len(archive_entries) > 0, \
        f"archive should have compression entries, got {len(archive_entries)}"


async def test_scoped_message_history_compresses_during_long_tool_loop():
    """Matched tool results must not delay compression until a final answer.

    This reproduces long bot_project ReAct loops where the model repeatedly
    emits assistant(tool_calls) followed by tool results. The hard message
    threshold must still be enforced after matched tool results, while the
    coordinator keeps assistant/tool pairs structurally legal.
    """
    from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator, SummaryStrategy
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy
    from framework.memory.registry.in_memory import InMemoryStoreRegistry

    class _Summary(SummaryStrategy):
        async def summarize(self, messages, context, reason):
            return "compressed summary"

    registry = InMemoryStoreRegistry()
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="e2e-long-tool-loop")

    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=10,
        keep_ratio_for_messages=0.4,
        summary=_Summary(),
    )
    lifecycle = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
    system = DefaultMemorySystem(
        layer_set=layer_set, store_registry=registry, lifecycle_policy=lifecycle,
    )
    await system.initialize()

    history = system.create_message_history(ctx)
    await history.append({"role": "user", "content": "inspect a large project"})
    for i in range(18):
        call_id = f"call-{i}"
        await history.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "read_file"}}
            ],
        })
        await history.append({
            "role": "tool",
            "tool_call_id": call_id,
            "name": "read_file",
            "content": f"result {i}",
        })

    remaining = [message.to_dict() for message in await system.get_history(ctx, max_messages=None)]
    assert len(remaining) <= 10, \
        f"matched tool loop should compress at threshold, got {len(remaining)} messages"

    declared_call_ids = {
        tool_call["id"]
        for message in remaining
        for tool_call in message.get("tool_calls", []) or []
        if isinstance(tool_call, dict) and tool_call.get("id")
    }
    for message in remaining:
        if message.get("role") == str(MessageRole.TOOL):
            assert message.get("tool_call_id") in declared_call_ids
