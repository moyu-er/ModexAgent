"""Tests for compression policies (Phase 4)."""
from __future__ import annotations

import inspect
from typing import Any

import pytest

from framework.core.types import MessageRole
from framework.memory.compaction.boundary import BoundaryPolicy
from framework.memory.compaction.policy import (
    MessageCompactionDecision,
    MessageCompactionPolicy,
)
from framework.memory.compression.policies import (
    CommitPolicy,
    CompressionErrorPolicy,
    CompressionTriggerPolicy,
    DefaultCommitPolicy,
    DefaultCompressionErrorPolicy,
    DefaultCompressionTriggerPolicy,
    DefaultMemoryCompressionCoordinator,
    MemoryCompressionCoordinator,
    SummaryStrategy,
)
from framework.memory.core.models import (
    ArchiveEntry,
    CompressionPlan,
    CompressionReason,
    CompressionResult,
    CompressionResultReason,
    CompressionTrigger,
    StorageRevision,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.layers.pending import PendingPrunedInputEntry
from framework.memory.registry.in_memory import InMemoryStoreRegistry


class _SimpleSummary(SummaryStrategy):
    """Deterministic summary for tests that need archive writes."""

    async def summarize(self, messages, context, reason):
        parts = [m.get("content", "") for m in messages if m.get("content")]
        return " | ".join(parts[:10]) if parts else ""


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
    trigger = DefaultCompressionTriggerPolicy(max_messages=100)
    result = await trigger.should_compress(session=session, context=ctx)
    assert result is not None
    assert result.reason == CompressionReason.MESSAGE_COUNT


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
        async def commit(self, *, plan, session, archive, pending=None, context, error_policy):
            _ = pending
            return CompressionResult(committed=True)

    class MyCoordinator(MemoryCompressionCoordinator):
        async def maybe_compress(self, *, session, archive, pending=None, context, idle_threshold_seconds=None):
            return CompressionResult(committed=True, reason=CompressionResultReason.NOT_NEEDED)

    assert isinstance(MyTrigger(), CompressionTriggerPolicy)
    assert isinstance(MySummary(), SummaryStrategy)
    assert isinstance(MyError(), CompressionErrorPolicy)
    assert isinstance(MyCommit(), CommitPolicy)
    assert isinstance(MyCoordinator(), MemoryCompressionCoordinator)


def test_compression_coordinator_contract_accepts_pending_layer():
    signature = inspect.signature(MemoryCompressionCoordinator.maybe_compress)

    assert "pending" in signature.parameters


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
    assert result.reason in (CompressionResultReason.NOT_NEEDED, None), \
        f"unexpected reason: {result.reason}"


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

    initial_count = len(await session.get_all_messages(ctx))
    assert initial_count == 150

    class FailingCommit(DefaultCommitPolicy):
        async def commit(self, *, plan, session, archive, pending=None, context, error_policy):
            _ = pending
            return CompressionResult(committed=False, retryable=True, reason=CompressionResultReason.ARCHIVE_FAILED)

    layer_set = MemoryLayerFactory.single_user(registry=registry)
    coordinator = DefaultMemoryCompressionCoordinator(max_messages=10, commit=FailingCommit())
    result = await coordinator.maybe_compress(
        session=session, archive=layer_set.archive, context=ctx,
    )
    assert not result.committed
    assert len(await session.get_all_messages(ctx)) == 150  # untouched


async def test_commit_does_not_persist_pending_when_archive_write_fails(registry):
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    pending = layer_set.pending
    assert pending is not None
    ctx = MemoryContext(session_id="pending-archive-failure")
    await session.add_messages(ctx, [
        {"role": "user", "content": "unfinished"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
    ])

    class FailingArchive:
        async def append(self, context, entry):
            _ = context, entry
            raise RuntimeError("archive down")

    revision = await session.get_revision(ctx)
    plan = CompressionPlan(
        trigger=CompressionTrigger(reason=CompressionReason.MESSAGE_COUNT),
        expected_revision=revision,
        expected_cursor=None,
        keep_messages=[{"role": "tool", "tool_call_id": "t1", "content": "result"}],
        summarize_messages=[{"role": "user", "content": "unfinished"}],
        archive_raw_messages=[],
        drop_messages=[],
        summary="unfinished",
        pending_pruned_input_entries=[
            PendingPrunedInputEntry.from_message(
                {"role": "user", "content": "unfinished"},
                pruned_at=100.0,
            )
        ],
    )

    result = await DefaultCommitPolicy().commit(
        plan=plan,
        session=session,
        archive=FailingArchive(),  # type: ignore[arg-type]
        pending=pending,
        context=ctx,
        error_policy=DefaultCompressionErrorPolicy(),
    )

    assert not result.committed
    assert result.reason == CompressionResultReason.ARCHIVE_FAILED
    assert [message.content for message in await session.get_all_messages(ctx)] == [
        "unfinished",
        "",
        "result",
    ]
    assert await pending.get_entries(ctx) == []


async def test_commit_restores_pending_snapshot_when_session_revision_conflicts(registry):
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    pending = layer_set.pending
    assert pending is not None
    ctx = MemoryContext(session_id="pending-revision-conflict")
    existing = PendingPrunedInputEntry.from_message(
        {"role": "user", "content": "already pending"},
        pruned_at=50.0,
    )
    await pending.append_entries(ctx, [existing])
    expected_revision = StorageRevision(message_count=3, updated_at=None, version=1)

    class ConflictingSession:
        async def get_revision(self, context):
            _ = context
            return expected_revision

        async def replace_messages_if_revision(self, context, messages, revision, state_updates=None, idle_threshold_seconds=None):
            _ = context, messages, revision, state_updates, idle_threshold_seconds
            return None

    plan = CompressionPlan(
        trigger=CompressionTrigger(reason=CompressionReason.MESSAGE_COUNT),
        expected_revision=expected_revision,
        expected_cursor=None,
        keep_messages=[{"role": "tool", "tool_call_id": "t1", "content": "result"}],
        summarize_messages=[],
        archive_raw_messages=[],
        drop_messages=[],
        summary="",
        pending_pruned_input_entries=[
            PendingPrunedInputEntry.from_message(
                {"role": "user", "content": "new pending"},
                pruned_at=100.0,
            )
        ],
    )

    result = await DefaultCommitPolicy().commit(
        plan=plan,
        session=ConflictingSession(),  # type: ignore[arg-type]
        archive=None,
        pending=pending,
        context=ctx,
        error_policy=DefaultCompressionErrorPolicy(),
    )

    assert result.reason == CompressionResultReason.REVISION_CHANGED
    assert [entry.content for entry in await pending.get_entries(ctx)] == ["already pending"]


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

    # keep_ratio=0.9 (clamp max): max_keep=4 from 6 msgs.
    # Planner uses budget_suffix because latest_user (index 5) >= min_start (2).
    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=5, keep_ratio_for_messages=0.9, summary=_SimpleSummary(),
    )
    result = await coordinator.maybe_compress(session=session, archive=archive, context=ctx)

    assert result.committed
    remaining = await session.get_all_messages(ctx)
    # Budget suffix keeps messages 2-5 (4 messages), which includes the user anchor.
    assert [message.content for message in remaining] == [
        "message 2", "message 3", "message 4", "message 5",
    ]

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


# ── Regression: empty summary commit ──────────────────────────────────────


async def test_commit_skips_empty_summary_and_reports_no_op(registry):
    """Empty summary → committed=False, reason=nothing_to_archive, no writes."""
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    archive = layer_set.archive
    ctx = MemoryContext(session_id="empty-commit")
    await session.add_messages(ctx, [{"role": "user", "content": "hi"}])

    plan = CompressionPlan(
        trigger=CompressionTrigger(reason=CompressionReason.MANUAL),
        expected_revision=await session.get_revision(ctx),
        expected_cursor=None,
        keep_messages=[{"role": "user", "content": "hi"}],
        summarize_messages=[],
        archive_raw_messages=[],
        drop_messages=[],
        summary="",  # empty
    )

    commit = DefaultCommitPolicy()
    error_policy = DefaultCompressionErrorPolicy()
    result = await commit.commit(
        plan=plan,
        session=session,
        archive=archive,
        context=ctx,
        error_policy=error_policy,
    )

    assert result.committed is True  # session truncated, just no archive
    assert result.reason == CompressionResultReason.NOTHING_TO_ARCHIVE


async def test_commit_skips_placeholder_summaries(registry):
    """Known placeholder strings like '(nothing)' also skip with no-op."""
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    archive = layer_set.archive
    ctx = MemoryContext(session_id="placeholder-commit")
    await session.add_messages(ctx, [{"role": "user", "content": "hi"}])

    for placeholder in ("(nothing)", "(no summary)", "(no semantic content)"):
        plan = CompressionPlan(
            trigger=CompressionTrigger(reason=CompressionReason.MANUAL),
            expected_revision=await session.get_revision(ctx),
            expected_cursor=None,
            keep_messages=[{"role": "user", "content": "hi"}],
            summarize_messages=[],
            archive_raw_messages=[],
            drop_messages=[],
            summary=placeholder,
        )

        result = await DefaultCommitPolicy().commit(
            plan=plan,
            session=session,
            archive=archive,
            context=ctx,
            error_policy=DefaultCompressionErrorPolicy(),
        )
        assert result.committed is True, f"placeholder '{placeholder}' — session truncated, no archive"
        assert result.reason == CompressionResultReason.NOTHING_TO_ARCHIVE, f"placeholder '{placeholder}'"


# ── Regression: trigger no-op on hidden history ───────────────────────────


async def test_commit_skips_long_whitespace_summary(registry):
    """Whitespace-only summaries should never create archive entries."""
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    archive = layer_set.archive
    ctx = MemoryContext(session_id="whitespace-commit")
    await session.add_messages(ctx, [{"role": "user", "content": "hi"}])

    plan = CompressionPlan(
        trigger=CompressionTrigger(reason=CompressionReason.MANUAL),
        expected_revision=await session.get_revision(ctx),
        expected_cursor=None,
        keep_messages=[{"role": "user", "content": "hi"}],
        summarize_messages=[],
        archive_raw_messages=[],
        drop_messages=[],
        summary=" " * 80,
    )

    result = await DefaultCommitPolicy().commit(
        plan=plan,
        session=session,
        archive=archive,
        context=ctx,
        error_policy=DefaultCompressionErrorPolicy(),
    )

    assert result.committed is True  # session still truncated
    assert result.reason == CompressionResultReason.NOTHING_TO_ARCHIVE
    assert await archive.get_recent(ctx, limit=10) == []  # no archive entries written


async def test_default_commit_replaces_session_when_archive_is_none(registry):
    """archive=None means session-only compression, not archive failure."""
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    ctx = MemoryContext(session_id="session-only-commit")
    await session.add_messages(
        ctx,
        [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new"},
        ],
    )

    plan = CompressionPlan(
        trigger=CompressionTrigger(reason=CompressionReason.MANUAL),
        expected_revision=await session.get_revision(ctx),
        expected_cursor=None,
        keep_messages=[{"role": "user", "content": "new"}],
        summarize_messages=[{"role": "user", "content": "old"}],
        archive_raw_messages=[],
        drop_messages=[],
        summary="old task summary",
    )

    result = await DefaultCommitPolicy().commit(
        plan=plan,
        session=session,
        archive=None,
        context=ctx,
        error_policy=DefaultCompressionErrorPolicy(),
    )

    assert result.committed is True
    assert result.reason == CompressionResultReason.NOTHING_TO_ARCHIVE
    kept = [msg.to_dict() for msg in await session.get_all_messages(ctx)]
    assert kept == [{"role": "user", "content": "new"}]


async def test_coordinator_excludes_dropped_messages_from_summary_input(registry):
    """Coordinator should only summarize messages classified as SUMMARIZE."""
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    class FixedBoundary(BoundaryPolicy):
        def find_prune_boundary(self, messages, decisions, target_prune_count):
            _ = messages, decisions, target_prune_count
            return 4

    class ToolDroppingPolicy(MessageCompactionPolicy):
        def decide(self, message, context, reason):
            _ = context, reason
            role = message.get("role") if isinstance(message, dict) else message.role
            has_tool_calls = bool(
                message.get("tool_calls") if isinstance(message, dict) else message.tool_calls
            )
            if role == "tool" or (role == "assistant" and has_tool_calls):
                return MessageCompactionDecision.DROP_FROM_SUMMARY
            return MessageCompactionDecision.SUMMARIZE

    class CapturingSummary(SummaryStrategy):
        def __init__(self):
            self.messages = []

        async def summarize(self, messages, context, reason):
            _ = context, reason
            self.messages = list(messages)
            return "summarized user content"

    config = SessionMemoryConfig(max_messages=None)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    archive = MemoryLayerFactory.single_user(registry=registry).archive
    ctx = MemoryContext(session_id="coord-tool-filter")
    await session.add_messages(ctx, [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "large raw result"},
        {"role": "assistant", "content": "answer based on tool"},
        {"role": "user", "content": "new question"},
    ])

    summary = CapturingSummary()
    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=1,
        boundary=FixedBoundary(),
        summary=summary,
        compaction=ToolDroppingPolicy(),
    )

    result = await coordinator.maybe_compress(session=session, archive=archive, context=ctx)

    assert result.committed
    assert [message["role"] for message in summary.messages] == ["user", "assistant"]
    assert all(not message.get("tool_calls") for message in summary.messages)


async def test_conservative_policy_excludes_tool_from_summary(registry):
    """bot_project default: ConservativeCompactionPolicy + coordinator → tool excluded."""
    from framework.memory.compaction.policy import ConservativeCompactionPolicy
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    class FixedBoundary(BoundaryPolicy):
        def find_prune_boundary(self, messages, decisions, target_prune_count):
            _ = messages, decisions, target_prune_count
            return 4  # prune first 4 messages (user, tc, tool, answer)

    class CapturingSummary(SummaryStrategy):
        def __init__(self):
            self.messages: list[dict[str, Any]] = []

        async def summarize(self, messages, context, reason):
            _ = context, reason
            self.messages = list(messages)
            return "captured"

    config = SessionMemoryConfig(max_messages=None)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    archive = MemoryLayerFactory.single_user(registry=registry).archive
    ctx = MemoryContext(session_id="conservative-filter")

    await session.add_messages(ctx, [
        {"role": "user", "content": "read file"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "read_file"}}
        ]},
        {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "file content here"},
        {"role": "assistant", "content": "file says hello"},
        {"role": "user", "content": "new question"},
    ])

    summary = CapturingSummary()
    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=1,
        boundary=FixedBoundary(),
        summary=summary,
        compaction=ConservativeCompactionPolicy(),  # bot_project's actual policy
    )

    result = await coordinator.maybe_compress(session=session, archive=archive, context=ctx)
    assert result.committed

    # With SUMMARIZE for all messages (including tool): user + tc assistant + tool + answer
    roles = [m.get("role") for m in summary.messages]
    assert len(roles) >= 3, f"tool context should be in summary, got {len(roles)} roles"
    assert "user" in roles
    # assistant tool_calls message should be in summary input
    assert any(m.get("tool_calls") for m in summary.messages), \
        "assistant tool_calls should be in summary for tool context preservation"


async def test_high_value_tool_results_included_in_summary(registry):
    """When tool is in high_value_tools, its result IS included in summary."""
    from framework.memory.compaction.policy import ConservativeCompactionPolicy
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    class FixedBoundary(BoundaryPolicy):
        def find_prune_boundary(self, messages, decisions, target_prune_count):
            _ = messages, decisions, target_prune_count
            return 3

    class CapturingSummary(SummaryStrategy):
        def __init__(self):
            self.messages: list[dict[str, Any]] = []

        async def summarize(self, messages, context, reason):
            _ = context, reason
            self.messages = list(messages)
            return "captured"

    config = SessionMemoryConfig(max_messages=None)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    archive = MemoryLayerFactory.single_user(registry=registry).archive
    ctx = MemoryContext(session_id="high-value")

    await session.add_messages(ctx, [
        {"role": "user", "content": "search web"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "web_search"}}
        ]},
        {"role": "tool", "tool_call_id": "t1", "name": "web_search", "content": "search result"},
        {"role": "assistant", "content": "here is the answer"},
    ])

    summary = CapturingSummary()
    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=1,
        boundary=FixedBoundary(),
        summary=summary,
        # web_search is whitelisted as high-value
        compaction=ConservativeCompactionPolicy(high_value_tools={"web_search"}),
    )

    result = await coordinator.maybe_compress(session=session, archive=archive, context=ctx)
    assert result.committed

    roles = [m.get("role") for m in summary.messages]
    # High-value process evidence is summarized, while the recent user input is
    # kept raw by priority retention under the hard keep budget.
    assert "tool" in roles, "high-value tool result should be in summary"
    assert "user" not in roles

    kept_roles = [m.to_dict().get("role") for m in await session.get_all_messages(ctx)]
    assert kept_roles == ["user"]


async def test_trigger_does_not_fire_when_total_within_budget(registry):
    """Total stored count within budget → no trigger, regardless of visible cap."""
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    config = SessionMemoryConfig(max_messages=3)  # caps visible to 3
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    ctx = MemoryContext(session_id="within-budget")

    # 6 messages stored total, visible capped at 3
    await session.add_messages(ctx, [
        {"role": "user", "content": f"msg{i}"} for i in range(6)
    ])

    stored = len(await session.get_all_messages(ctx))
    assert stored == 6

    trigger = DefaultCompressionTriggerPolicy(max_messages=10, max_tokens=8000)
    result = await trigger.should_compress(session=session, context=ctx)
    # Total stored (6) < trigger threshold (10) → no trigger
    assert result is None, f"total 6 < max 10 should not trigger, got {result}"


async def test_trigger_fires_when_total_exceeds_threshold(registry):
    """Total stored count > max_messages → trigger, even if visible is capped."""
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    config = SessionMemoryConfig(max_messages=5)  # caps visible to 5
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    ctx = MemoryContext(session_id="exceeds")

    await session.add_messages(ctx, [
        {"role": "user", "content": f"msg{i}"} for i in range(12)
    ])

    stored = len(await session.get_all_messages(ctx))
    assert stored == 12
    visible = len(await session.get_recent_messages(ctx))
    assert visible == 5  # capped

    trigger = DefaultCompressionTriggerPolicy(max_messages=10)
    result = await trigger.should_compress(session=session, context=ctx)
    # Total stored (12) > trigger threshold (10)
    assert result is not None, "total 12 > max 10 should trigger"
    assert result.reason == CompressionReason.MESSAGE_COUNT


# ── Regression: injection filters legacy empty markers ────────────────────


async def test_injection_filters_no_semantic_content_entries(registry):
    """Archive entries with '(no semantic content)' or source=empty are filtered."""
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.injection import FullInjectionPolicy

    layer_set = MemoryLayerFactory.single_user(registry=registry)
    system = DefaultMemorySystem(layer_set=layer_set, store_registry=registry)
    ctx = MemoryContext(session_id="filter-empty")
    await system.initialize()

    await layer_set.archive.append(ctx, ArchiveEntry(
        summary="(no semantic content)",
        metadata={"source": "empty", "semantic_count": 0},
    ))
    await layer_set.archive.append(ctx, ArchiveEntry(
        summary="real conversation about project setup",
    ))

    bundle = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=ctx, memory_system=system, query="",
    )

    content = "\n".join(section.content for section in bundle.system_sections)
    assert "project setup" in content
    assert "no semantic content" not in content


async def test_injection_filters_empty_session_summaries(registry):
    """Legacy empty compression and auto-compact summaries should not be injected."""
    from framework.memory.core.scope import MemoryLayerName, SessionScope
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.injection import FullInjectionPolicy

    layer_set = MemoryLayerFactory.single_user(registry=registry)
    system = DefaultMemorySystem(layer_set=layer_set, store_registry=registry)
    ctx = MemoryContext(session_id="filter-empty-session-summary")
    await system.initialize()

    storage = await registry.resolve(
        layer=MemoryLayerName.SESSION,
        scope=SessionScope(),
        context=ctx,
    )
    await storage.set(".compression_summary", "(no semantic content)")
    await storage.set(".auto_compact_summary", " " * 80)

    bundle = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=ctx, memory_system=system, query="",
    )

    content = "\n".join(section.content for section in bundle.system_sections)
    assert "Earlier conversation compressed" not in content
    assert "Auto-compact summary" not in content


# ── Bot_project scenario: tool-chain safety + visible cap ────────────────


async def test_coordinator_compresses_tool_chains_atomically(registry):
    """Tool chains are never split: whole chain pruned or whole chain kept."""
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    config = SessionMemoryConfig(max_messages=None)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    archive = MemoryLayerFactory.single_user(registry=registry).archive
    ctx = MemoryContext(session_id="tool-chain-compress")

    # 6 turns, each: user → assistant tool_calls → 2x tool result → assistant answer = 30 msgs
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
    await session.add_messages(ctx, messages)

    assert len(await session.get_all_messages(ctx)) == 30

    coordinator = DefaultMemoryCompressionCoordinator(max_messages=8, summary=_SimpleSummary())
    result = await coordinator.maybe_compress(session=session, archive=archive, context=ctx)
    assert result.committed, f"expected committed, got {result}"

    remaining = await session.get_all_messages(ctx)
    # Boundary may keep slightly more than max_messages to avoid splitting tool chains
    assert len(remaining) <= 12, \
        f"should compress substantially (from 30), got {len(remaining)}"

    # No orphan tool results in kept suffix
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
                f"orphan tool result {d.get('tool_call_id')} in kept suffix"

    # Archive entries written
    assert len(await archive.get_recent(ctx, limit=10)) > 0


async def test_coordinator_compresses_when_total_exceeds_visible_cap(registry):
    """Simulate bot_project: 96 stored, visible cap 50, trigger at 50 → compresses."""
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    config = SessionMemoryConfig(max_messages=50)  # caps visible
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    archive = MemoryLayerFactory.single_user(registry=registry).archive
    ctx = MemoryContext(session_id="visible-cap")

    await session.add_messages(ctx, [
        {"role": "user", "content": f"msg{i}"} for i in range(96)
    ])

    stored = len(await session.get_all_messages(ctx))
    visible = len(await session.get_recent_messages(ctx))
    assert stored == 96
    assert visible == 50

    coordinator = DefaultMemoryCompressionCoordinator(max_messages=50, summary=_SimpleSummary())
    result = await coordinator.maybe_compress(session=session, archive=archive, context=ctx)
    assert result.committed, f"stored=96 > max=50 should trigger, got {result}"

    remaining = len(await session.get_all_messages(ctx))
    # keep_ratio=0.5 → keep_target=25, compress 96→~25
    assert remaining <= 30, f"should compress with headroom to ~25, got {remaining}"

    entries = await archive.get_recent(ctx, limit=10)
    assert len(entries) > 0, "archive should have compressed entries"


async def test_coordinator_creates_headroom_no_recompress_on_small_growth(registry):
    """After compression creates headroom, small growth doesn't re-trigger."""
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    config = SessionMemoryConfig(max_messages=None)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    archive = MemoryLayerFactory.single_user(registry=registry).archive
    ctx = MemoryContext(session_id="headroom")

    # 96 messages, trigger at 50, keep_ratio=0.5 → compress to ~25
    await session.add_messages(ctx, [
        {"role": "user", "content": f"msg{i}"} for i in range(96)
    ])

    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=50, keep_ratio_for_messages=0.5, summary=_SimpleSummary(),
    )
    result = await coordinator.maybe_compress(session=session, archive=archive, context=ctx)
    assert result.committed

    after_compress = len(await session.get_all_messages(ctx))
    assert after_compress <= 30, f"headroom created: {after_compress}"

    # Add 10 more messages — still well below trigger threshold of 50
    await session.add_messages(ctx, [
        {"role": "user", "content": f"new{i}"} for i in range(10)
    ])
    total = len(await session.get_all_messages(ctx))
    assert total <= 45, f"still below trigger after small growth: {total}"

    # Trigger should NOT fire
    result2 = await coordinator.maybe_compress(session=session, archive=archive, context=ctx)
    assert result2.reason in (CompressionResultReason.NOT_NEEDED, None), \
        f"should NOT compress, total={total} < max=50, got {result2.reason}"


# ── Regression: token estimation must use all messages ─────────────────────


@pytest.mark.asyncio
async def test_trigger_token_pressure_uses_all_messages_not_windowed_view(registry):
    """TOKEN_PRESSURE must estimate tokens from ALL messages, not a windowed subset.

    Regression: Previously get_visible_messages() was used, which applies the
    session's max_messages window.  If the session window is smaller than the
    trigger threshold, token estimation is incomplete and may miss pressure.

    Scenario:
      - Session window (max_messages) = 10  ← small window
      - Trigger threshold (max_messages) = 50
      - Trigger threshold (max_tokens) = 1500
      - 20 messages, each 500 chars → ~2500 tokens total

    Old behavior (get_visible_messages, window=10):
      - Only 10 messages considered → ~1250 tokens
      - 1250 < 1500 → NO trigger ❌ (missed pressure)

    New behavior (get_all_messages):
      - All 20 messages considered → ~2500 tokens
      - 2500 > 1500 → TOKEN_PRESSURE trigger ✅
    """
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    config = SessionMemoryConfig(max_messages=10)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    ctx = MemoryContext(session_id="token-pressure-regression")

    # 20 messages × 500 chars = 10000 chars → ~2500 tokens (estimate = chars/4)
    msgs = [{"role": "user", "content": "A" * 500} for _ in range(20)]
    await session.add_messages(ctx, msgs)

    stored = len(await session.get_all_messages(ctx))
    recent = len(await session.get_recent_messages(ctx))
    assert stored == 20, "all 20 messages stored"
    assert recent == 10, "windowed view only shows 10"

    trigger = DefaultCompressionTriggerPolicy(max_messages=50, max_tokens=1500)
    result = await trigger.should_compress(session=session, context=ctx)

    assert result is not None, (
        "TOKEN_PRESSURE should trigger: total tokens ~2500 > max_tokens=1500. "
        "If this fails, token estimation may be using a windowed view."
    )
    assert result.reason == CompressionReason.TOKEN_PRESSURE


@pytest.mark.asyncio
async def test_trigger_message_count_respects_threshold(registry):
    """MESSAGE_COUNT trigger must fire only when total messages exceed threshold.

    Regression guard: ensures the fix to use get_all_messages for token
    estimation does not break the primary MESSAGE_COUNT trigger path.
    """
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    config = SessionMemoryConfig(max_messages=None)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    ctx = MemoryContext(session_id="msg-count")

    # 55 messages > threshold 50 → should trigger
    msgs = [{"role": "user", "content": f"msg{i}"} for i in range(55)]
    await session.add_messages(ctx, msgs)

    trigger = DefaultCompressionTriggerPolicy(max_messages=50)
    result = await trigger.should_compress(session=session, context=ctx)

    assert result is not None
    assert result.reason == CompressionReason.MESSAGE_COUNT
    # Log should show: "Compression triggered by MESSAGE_COUNT: msgs=55 > max_messages=50"


@pytest.mark.asyncio
async def test_compression_persists_pruned_unfinished_input_to_pending(registry):
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    config = SessionMemoryConfig(max_messages=None)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    layers = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="pending-compress")
    assert layers.pending is not None
    await session.add_messages(ctx, [
        {"role": "user", "content": "unfinished"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
        {"role": "user", "content": "follow up"},
    ])

    coordinator = DefaultMemoryCompressionCoordinator(max_messages=2)
    result = await coordinator.maybe_compress(
        session=session,
        archive=None,
        pending=layers.pending,
        context=ctx,
    )

    assert result.committed
    assert [entry.content for entry in await layers.pending.get_entries(ctx)] == ["unfinished"]


@pytest.mark.asyncio
async def test_compression_does_not_pending_completed_pruned_input(registry):
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    config = SessionMemoryConfig(max_messages=None)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    layers = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="pending-completed")
    assert layers.pending is not None
    await session.add_messages(ctx, [
        {"role": "user", "content": "finished"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "current"},
    ])

    coordinator = DefaultMemoryCompressionCoordinator(max_messages=1)
    result = await coordinator.maybe_compress(
        session=session,
        archive=None,
        pending=layers.pending,
        context=ctx,
    )

    assert result.committed
    assert await layers.pending.get_entries(ctx) == []


@pytest.mark.asyncio
async def test_compression_input_does_not_include_pending_entries(registry):
    layers = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="pending-not-compressed")
    assert layers.pending is not None
    await layers.pending.append_entries(ctx, [
        PendingPrunedInputEntry.from_message(
            {"role": "user", "content": "pending only"},
            pruned_at=100.0,
        )
    ])
    await layers.session.add_messages(ctx, [
        {"role": "user", "content": "s0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "s1"},
        {"role": "assistant", "content": "a1"},
    ])
    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=3,
        keep_ratio_for_messages=0.5,
    )

    await coordinator.maybe_compress(
        session=layers.session,
        archive=layers.archive,
        pending=layers.pending,
        context=ctx,
    )

    session_contents = [msg.content for msg in await layers.session.get_all_messages(ctx)]
    assert "pending only" not in session_contents


@pytest.mark.asyncio
async def test_trigger_no_false_positive_below_threshold(registry):
    """Compression must NOT trigger when both message count and tokens are within budget.

    Regression guard: ensures we don't over-trigger after removing cooldown.
    """
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    config = SessionMemoryConfig(max_messages=None)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(storage_factory=factory, config=config)
    ctx = MemoryContext(session_id="no-fp")

    # 30 messages < threshold 50, short content → tokens well below budget
    msgs = [{"role": "user", "content": f"short{i}"} for i in range(30)]
    await session.add_messages(ctx, msgs)

    trigger = DefaultCompressionTriggerPolicy(max_messages=50, max_tokens=100000)
    result = await trigger.should_compress(session=session, context=ctx)

    assert result is None, "should not trigger: 30 msgs < 50, tokens well below 100k"


async def test_coordinator_removes_stale_invalid_tool_chain_without_archive(registry):
    """Stale incomplete tool-chain that is NOT the last assistant with tool_calls
    must be removed. The last tool-call assistant has a complete chain."""
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(factory, SessionMemoryConfig(max_messages=None))
    ctx = MemoryContext(session_id="sanitize-cleanup")
    await session.add_messages(ctx, [
        {"role": str(MessageRole.USER), "content": "start"},
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [
                {"id": "stale-a", "function": {"name": "tool_stale_a"}},
                {"id": "stale-b", "function": {"name": "tool_stale_b"}},
            ],
        },
        {"role": str(MessageRole.TOOL), "tool_call_id": "stale-a", "content": "partial"},
        {"role": str(MessageRole.USER), "content": "continued"},
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [
                {"id": "c", "function": {"name": "tool_c"}},
            ],
        },
        {"role": str(MessageRole.TOOL), "tool_call_id": "c", "content": "result_c"},
        {"role": str(MessageRole.ASSISTANT), "content": "done"},
    ])

    # Use token-pressure trigger with high keep-ratio to avoid over-trimming
    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=None,
        max_tokens=1,
        keep_ratio_for_token=0.9,
    )
    result = await coordinator.maybe_compress(
        session=session,
        archive=None,
        context=ctx,
    )

    assert result.committed
    remaining = [msg.to_dict() for msg in await session.get_all_messages(ctx)]
    remaining_str = str(remaining)
    # Stale incomplete tool-chain records must be removed
    assert "stale-a" not in remaining_str
    assert "stale-b" not in remaining_str
    # Since commit succeeded and stale records are gone, sanitizer worked.
    # (Keep planner may also trim older messages — that is expected behavior.)


async def test_coordinator_preserves_active_open_tail_but_removes_older_invalid_chain(registry):
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(factory, SessionMemoryConfig(max_messages=None))
    ctx = MemoryContext(session_id="sanitize-open-tail")
    open_tail = {
        "role": str(MessageRole.ASSISTANT),
        "content": "",
        "tool_calls": [
            {"id": "tail-a", "function": {"name": "tool_tail_a"}},
            {"id": "tail-b", "function": {"name": "tool_tail_b"}},
        ],
    }
    await session.add_messages(ctx, [
        {"role": str(MessageRole.USER), "content": "start"},
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [{"id": "old-a", "function": {"name": "tool_old_a"}}],
        },
        {"role": str(MessageRole.USER), "content": "continued"},
        open_tail,
        {"role": str(MessageRole.TOOL), "tool_call_id": "tail-a", "content": "partial"},
    ])

    coordinator = DefaultMemoryCompressionCoordinator(max_messages=3)
    result = await coordinator.maybe_compress(
        session=session,
        archive=None,
        context=ctx,
    )

    assert result.committed
    remaining = [msg.to_dict() for msg in await session.get_all_messages(ctx)]
    assert remaining == [
        open_tail,
        {"role": str(MessageRole.TOOL), "tool_call_id": "tail-a", "content": "partial"},
    ]
    assert "old-a" not in str(remaining)


async def test_coordinator_compresses_complete_history_before_active_open_tail(registry):
    """An active open tail protects only itself, not all earlier history."""
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(factory, SessionMemoryConfig(max_messages=None))
    ctx = MemoryContext(session_id="compress-before-open-tail")
    await session.add_messages(ctx, [{"role": str(MessageRole.USER), "content": "start"}])
    for idx in range(6):
        call_id = f"done-{idx}"
        await session.add_messages(ctx, [
            {
                "role": str(MessageRole.ASSISTANT),
                "content": "",
                "tool_calls": [{"id": call_id, "function": {"name": "tool_done"}}],
            },
            {"role": str(MessageRole.TOOL), "tool_call_id": call_id, "content": f"result {idx}"},
        ])
    open_tail = {
        "role": str(MessageRole.ASSISTANT),
        "content": "",
        "tool_calls": [
            {"id": "tail-a", "function": {"name": "tool_tail_a"}},
            {"id": "tail-b", "function": {"name": "tool_tail_b"}},
        ],
    }
    await session.add_messages(ctx, [
        open_tail,
        {"role": str(MessageRole.TOOL), "tool_call_id": "tail-a", "content": "partial"},
    ])

    coordinator = DefaultMemoryCompressionCoordinator(
        max_messages=6,
        keep_ratio_for_messages=0.5,
    )
    result = await coordinator.maybe_compress(
        session=session,
        archive=None,
        context=ctx,
    )

    assert result.committed
    remaining = [msg.to_dict() for msg in await session.get_all_messages(ctx)]
    assert len(remaining) <= 3
    assert remaining[-2:] == [
        open_tail,
        {"role": str(MessageRole.TOOL), "tool_call_id": "tail-a", "content": "partial"},
    ]
    assert "done-0" not in str(remaining)


async def test_sanitizer_removed_messages_do_not_create_pending_entries(registry):
    """Sanitizer-removed invalid records must not produce pending entries."""
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    layer_set = MemoryLayerFactory.single_user(registry=registry)
    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(factory, SessionMemoryConfig(max_messages=None))
    ctx = MemoryContext(session_id="pending-boundary")
    await session.add_messages(ctx, [
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [{"id": "old-a", "function": {"name": "tool_old_a"}}],
        },
        {"role": str(MessageRole.USER), "content": "unfinished"},
    ])

    assert layer_set.pending is not None
    coordinator = DefaultMemoryCompressionCoordinator(max_messages=1)
    await coordinator.maybe_compress(
        session=session,
        archive=None,
        pending=layer_set.pending,
        context=ctx,
    )

    entries = await layer_set.pending.get_entries(ctx)
    assert [entry.content for entry in entries] == []


async def test_sanitizer_removed_messages_are_not_archived(registry):
    """Sanitizer-removed invalid records must not be written to archive."""
    from framework.memory.core.scope import MemoryLayerName
    from framework.memory.layers.config import SessionMemoryConfig
    from framework.memory.layers.session import ScopedSessionMemoryManager

    factory = MemoryLayerFactory._storage_factory(registry, MemoryLayerName.SESSION)
    session = ScopedSessionMemoryManager(factory, SessionMemoryConfig(max_messages=None))
    ctx = MemoryContext(session_id="archive-boundary")
    await session.add_messages(ctx, [
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [{"id": "old-a", "function": {"name": "tool_old_a"}}],
        },
        {"role": str(MessageRole.ASSISTANT), "content": "plain"},
    ])

    class RecordingArchive:
        def __init__(self):
            self.entries = []

        async def append(self, context, entry):
            _ = context
            self.entries.append(entry)

    archive = RecordingArchive()
    coordinator = DefaultMemoryCompressionCoordinator(max_messages=1)
    await coordinator.maybe_compress(
        session=session,
        archive=archive,  # type: ignore[arg-type]
        context=ctx,
    )

    assert archive.entries == []
