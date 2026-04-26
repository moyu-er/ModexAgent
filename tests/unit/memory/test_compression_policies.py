"""Tests for compression policies (Phase 4)."""
from __future__ import annotations

import pytest

from framework.memory.compression.policies import (
    CommitPolicy,
    CompressionErrorPolicy,
    CompressionTriggerPolicy,
    DefaultCommitPolicy,
    DefaultCompressionErrorPolicy,
    DefaultCompressionTriggerPolicy,
    DefaultMemoryCompressionCoordinator,
    HeuristicSummaryStrategy,
    MemoryCompressionCoordinator,
    SummaryStrategy,
)
from framework.memory.core.models import (
    ArchiveEntry,
    CompressionPlan,
    CompressionReason,
    CompressionResult,
    CompressionTrigger,
    StorageRevision,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.registry.in_memory import InMemoryStoreRegistry


@pytest.fixture
def registry():
    return InMemoryStoreRegistry()


@pytest.fixture
def layer_set(registry):
    return MemoryLayerFactory.single_user(registry=registry)


async def test_trigger_no_compress_when_under_limit(registry):
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    ctx = MemoryContext(session_id="t1")
    await session.add_messages(ctx, [{"role": "user", "content": "hi"}])
    trigger = DefaultCompressionTriggerPolicy(max_messages=100)
    result = await trigger.should_compress(session=session, context=ctx)
    assert result is None


async def test_trigger_compress_when_over_limit(registry):
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    config = SessionMemoryConfig(max_messages=None)  # No auto-truncation
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    ctx = MemoryContext(session_id="t2")
    msgs = [{"role": "user", "content": f"msg{i}"} for i in range(110)]
    await session.add_messages(ctx, msgs)
    trigger = DefaultCompressionTriggerPolicy(max_messages=100, cooldown_messages=0)
    result = await trigger.should_compress(session=session, context=ctx)
    assert result is not None
    assert result.reason == CompressionReason.MESSAGE_COUNT


async def test_heuristic_summary():
    strategy = HeuristicSummaryStrategy()
    msgs = [
        {"role": "user", "content": "what is python"},
        {"role": "assistant", "content": "Python is a programming language"},
        {"role": "user", "content": "thanks"},
    ]
    summary = await strategy.summarize(msgs, MemoryContext(session_id="x"), CompressionReason.MANUAL)
    assert "what is python" in summary
    assert "thanks" in summary


async def test_error_policy_summary_fallback(registry):
    policy = DefaultCompressionErrorPolicy()
    msgs = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    fallback = await policy.on_summary_failure(
        RuntimeError("LLM down"), msgs, MemoryContext(session_id="x"),
    )
    assert fallback is not None
    assert "hello" in fallback


async def test_error_policy_archive_failure_preserves_session(registry):
    policy = DefaultCompressionErrorPolicy()
    plan = CompressionPlan(
        trigger=CompressionTrigger(reason=CompressionReason.MANUAL),
        expected_revision=StorageRevision(message_count=10, updated_at=None, version=0),
        expected_cursor=None,
        keep_messages=[],
        summarize_messages=[],
        archive_raw_messages=[],
        drop_messages=[],
    )
    proceed = await policy.on_archive_failure(RuntimeError("disk full"), plan, MemoryContext(session_id="x"))
    assert proceed is False  # Never mutate session on archive failure


async def test_compression_abc_registration():
    """Verify all policy ABCs can be subclassed."""
    class MyTrigger(CompressionTriggerPolicy):
        async def should_compress(self, *, session, context):
            return None

    class MySummary(SummaryStrategy):
        async def summarize(self, messages, context, reason):
            return "summary"

    class MyError(CompressionErrorPolicy):
        async def on_summary_failure(self, error, messages, context):
            return None
        async def on_archive_failure(self, error, plan, context):
            return False
        async def on_commit_conflict(self, plan, context):
            return False

    class MyCommit(CommitPolicy):
        async def commit(self, *, plan, session, archive, context, error_policy):
            return CompressionResult(committed=True)

    class MyCoordinator(MemoryCompressionCoordinator):
        async def maybe_compress(self, *, session, archive, context):
            return CompressionResult(committed=True, reason="test")

    assert isinstance(MyTrigger(), CompressionTriggerPolicy)
    assert isinstance(MySummary(), SummaryStrategy)
    assert isinstance(MyError(), CompressionErrorPolicy)
    assert isinstance(MyCommit(), CommitPolicy)
    assert isinstance(MyCoordinator(), MemoryCompressionCoordinator)


async def test_coordinator_no_compress_when_under_budget(registry):
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    archive = layer_set.archive
    ctx = MemoryContext(session_id="coord1")
    await session.add_messages(ctx, [{"role": "user", "content": "short"}])

    coordinator = DefaultMemoryCompressionCoordinator(max_messages=100)
    result = await coordinator.maybe_compress(
        session=session, archive=archive, context=ctx,
    )
    assert result.committed
    assert result.reason in ("not_needed", "within_budget")


async def test_coordinator_does_not_lose_data_on_archive_failure(registry):
    """Archive failure → session stays untouched."""
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    config = SessionMemoryConfig(max_messages=None)  # no auto-truncation
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)

    ctx = MemoryContext(session_id="coord2")
    msgs = [{"role": "user", "content": f"msg{i}"} for i in range(150)]
    await session.add_messages(ctx, msgs)

    initial_count = len(await session.get_visible_messages(ctx))
    assert initial_count == 150

    class FailingCommit(DefaultCommitPolicy):
        async def commit(self, *, plan, session, archive, context, error_policy):
            return CompressionResult(committed=False, retryable=True, reason="archive_failed")

    layer_set = MemoryLayerFactory.single_user(registry=registry)
    coordinator = DefaultMemoryCompressionCoordinator(max_messages=10, commit=FailingCommit())
    result = await coordinator.maybe_compress(
        session=session, archive=layer_set.archive, context=ctx,
    )
    assert not result.committed
    assert len(await session.get_visible_messages(ctx)) == 150  # untouched


async def test_coordinator_commit_replaces_session_and_persists_summary(registry):
    from framework.memory.core.scope import MemoryLayerName, SessionScope
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    config = SessionMemoryConfig(max_messages=None)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    archive = MemoryLayerFactory.single_user(registry=registry).archive
    ctx = MemoryContext(session_id="coord-summary")
    messages = [{"role": "user", "content": f"message {index}"} for index in range(6)]
    await session.add_messages(ctx, messages)

    coordinator = DefaultMemoryCompressionCoordinator(max_messages=3)
    result = await coordinator.maybe_compress(session=session, archive=archive, context=ctx)

    assert result.committed
    remaining = await session.get_all_messages(ctx)
    assert [message.content for message in remaining] == ["message 3", "message 4", "message 5"]

    storage = await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=ctx,
    )
    summary = await storage.get(".compression_summary")
    assert summary is not None
    assert "message 0" in summary


async def test_archive_injection_prefers_query_search(registry):
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.injection import FullInjectionPolicy

    layer_set = MemoryLayerFactory.single_user(registry=registry)
    system = DefaultMemorySystem(layer_set=layer_set, store_registry=registry)
    ctx = MemoryContext(session_id="archive-inject")
    await system.initialize()
    await layer_set.archive.append(ctx, ArchiveEntry(summary="最近闲聊: 天气很好"))
    await layer_set.archive.append(ctx, ArchiveEntry(summary="关键历史: Python 数据分析项目"))

    bundle = await FullInjectionPolicy(max_history_entries=1).assemble(
        context=ctx,
        memory_system=system,
        query="数据分析",
    )

    content = "\n".join(section.content for section in bundle.system_sections)
    assert "Python 数据分析项目" in content
    assert "天气很好" not in content
