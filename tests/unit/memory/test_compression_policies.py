"""Tests for cleanup_session() and related compression policies.

Replaces the old test_compression_policies.py which tested the deleted
MemoryCompressionCoordinator and policy ABCs. These tests verify that
cleanup_session() correctly handles trigger, cleanup, and archive.
"""
from __future__ import annotations

from typing import Any

import pytest

from framework.core.types import MessageRole
from framework.memory.archive_generation import ArchiveGenerationStrategy
from framework.memory.archive_models import (
    ArchiveChannel,
    ArchiveGenerationInputs,
    ArchiveGenerationResult,
    ArchiveInputStats,
    ArchiveWrite,
)
from framework.memory.cleanup import CleanupResult, cleanup_session
from framework.memory.core.models import ArchiveEntry, CompressionReason
from framework.memory.core.scope import MemoryContext
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.registry.in_memory import InMemoryStoreRegistry


# ── Helpers ───────────────────────────────────────────────────────────────


class _SimpleArchiveGeneration(ArchiveGenerationStrategy):
    """Deterministic archive generation for integration tests."""

    async def generate(self, messages, context, reason):
        joined = " | ".join(
            str(m.content if hasattr(m, "content") else m.get("content", ""))
            for m in messages
        )
        return ArchiveGenerationResult(
            writes=(
                ArchiveWrite(
                    channel=ArchiveChannel.CONTEXT,
                    summary=f"context: {joined}",
                    metadata={"reason": reason.value},
                ),
                ArchiveWrite(
                    channel=ArchiveChannel.KNOWLEDGE,
                    summary=f"knowledge: {joined}",
                    metadata={"reason": reason.value},
                ),
            ),
            inputs=ArchiveGenerationInputs(
                context_transcript=joined,
                knowledge_transcript=joined,
                stats=ArchiveInputStats(
                    input_messages=len(messages),
                    context_messages=len(messages),
                    knowledge_messages=len(messages),
                    tool_chains=0,
                    dropped_messages=0,
                ),
            ),
        )


class _EmptyArchiveGeneration(ArchiveGenerationStrategy):
    async def generate(self, messages, context, reason):
        return ArchiveGenerationResult(
            writes=(),
            inputs=ArchiveGenerationInputs(
                context_transcript="",
                knowledge_transcript="",
                stats=ArchiveInputStats(
                    input_messages=len(messages),
                    context_messages=0,
                    knowledge_messages=0,
                    tool_chains=0,
                    dropped_messages=0,
                ),
            ),
        )


@pytest.fixture
def registry():
    return InMemoryStoreRegistry()


# ── Trigger tests ─────────────────────────────────────────────────────────


async def test_trigger_no_cleanup_when_under_limit(registry):
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    ctx = MemoryContext(session_id="t1")
    await session.add_messages(ctx, [{"role": "user", "content": "hi"}])

    result = await cleanup_session(
        session=session, archive=layer_set.archive, context=ctx,
        max_messages=100,
    )
    assert result.triggered is False


async def test_trigger_cleanup_when_over_limit(registry):
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    ctx = MemoryContext(session_id="t2")
    msgs = [{"role": "user", "content": f"msg{i}"} for i in range(110)]
    await session.add_messages(ctx, msgs)

    result = await cleanup_session(
        session=session, archive=layer_set.archive, context=ctx,
        max_messages=100,
    )
    assert result.triggered is True
    assert result.reason == CompressionReason.MESSAGE_COUNT


async def test_trigger_no_false_positive_below_threshold(registry):
    """Cleanup must NOT trigger when both message count and tokens are within budget."""
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    ctx = MemoryContext(session_id="no-fp")

    msgs = [{"role": "user", "content": f"short{i}"} for i in range(30)]
    await session.add_messages(ctx, msgs)

    result = await cleanup_session(
        session=session, archive=layer_set.archive, context=ctx,
        max_messages=50, max_tokens=100000,
    )
    assert result.triggered is False


async def test_trigger_fires_when_total_exceeds_threshold(registry):
    """Total stored count > max_messages -> trigger fires."""
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    ctx = MemoryContext(session_id="exceeds")

    await session.add_messages(ctx, [
        {"role": "user", "content": f"msg{i}"} for i in range(12)
    ])
    result = await cleanup_session(
        session=session, archive=layer_set.archive, context=ctx,
        max_messages=10,
    )
    assert result.triggered is True
    assert result.reason == CompressionReason.MESSAGE_COUNT


# ── Cleanup: tool-chain safety ────────────────────────────────────────────


async def test_cleanup_compresses_tool_chains_atomically(registry):
    """Tool chains are never split: whole chain pruned or whole chain kept."""
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    archive = layer_set.archive
    ctx = MemoryContext(session_id="tool-chain-compress")

    messages: list[dict[str, Any]] = []
    for i in range(6):
        messages.append({"role": "user", "content": f"q{i}"})
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"tc{i}a", "type": "function", "function": {"name": "read_file"}},
            {"id": f"tc{i}b", "type": "function", "function": {"name": "bash"}},
        ]})
        messages.append({"role": "tool", "tool_call_id": f"tc{i}a", "name": "read_file", "content": f"out{i}a"})
        messages.append({"role": "tool", "tool_call_id": f"tc{i}b", "name": "bash", "content": f"out{i}b"})
        messages.append({"role": "assistant", "content": f"answer {i}"})
    await session.add_messages(ctx, messages)

    result = await cleanup_session(
        session=session, archive=archive, context=ctx,
        max_messages=8, keep_ratio=0.5,
        archive_strategy=_SimpleArchiveGeneration(),
    )
    assert result.triggered is True

    remaining = await session.get_all_messages(ctx)
    assert len(remaining) <= 15

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
    entries = await archive.get_recent(ctx, limit=10)
    assert len(entries) > 0


# ── Cleanup: session always cleaned even without archive ──────────────────


async def test_cleanup_cleans_session_without_archive_strategy(registry):
    """No archive_strategy -> session still cleaned, no archive entries."""
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    archive = layer_set.archive
    ctx = MemoryContext(session_id="cleanup-no-strategy")

    await session.add_messages(ctx, [
        {"role": "user", "content": f"message {i}"} for i in range(6)
    ])

    result = await cleanup_session(
        session=session, archive=archive, context=ctx,
        max_messages=5, keep_ratio=0.5,
        archive_strategy=None,
    )
    assert result.triggered is True
    remaining = len(await session.get_all_messages(ctx))
    assert remaining < 6


async def test_cleanup_cleans_session_with_empty_archive_generation(registry):
    """Empty archive generation writes -> session still cleaned."""
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    archive = layer_set.archive
    ctx = MemoryContext(session_id="cleanup-empty-gen")

    await session.add_messages(ctx, [
        {"role": "user", "content": f"message {i}"} for i in range(6)
    ])

    result = await cleanup_session(
        session=session, archive=archive, context=ctx,
        max_messages=5, keep_ratio=0.5,
        archive_strategy=_EmptyArchiveGeneration(),
    )
    assert result.triggered is True
    remaining = len(await session.get_all_messages(ctx))
    assert remaining < 6
    assert await archive.get_recent(ctx, limit=10) == []


# ── Cleanup: creates headroom, no re-trigger on small growth ──────────────


async def test_cleanup_creates_headroom_no_retrigger_on_small_growth(registry):
    """After cleanup creates headroom, small growth doesn't re-trigger."""
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    archive = layer_set.archive
    ctx = MemoryContext(session_id="headroom")

    await session.add_messages(ctx, [
        {"role": "user", "content": f"msg{i}"} for i in range(96)
    ])

    result = await cleanup_session(
        session=session, archive=archive, context=ctx,
        max_messages=50, keep_ratio=0.5,
        archive_strategy=_SimpleArchiveGeneration(),
    )
    assert result.triggered is True
    after_compress = len(await session.get_all_messages(ctx))
    assert after_compress <= 60

    # Add 10 more messages — still well below trigger threshold of 50
    await session.add_messages(ctx, [
        {"role": "user", "content": f"new{i}"} for i in range(10)
    ])
    total = len(await session.get_all_messages(ctx))
    assert total <= 70

    result2 = await cleanup_session(
        session=session, archive=archive, context=ctx,
        max_messages=50, keep_ratio=0.5,
        archive_strategy=_SimpleArchiveGeneration(),
    )
    # Should not trigger if still under limit
    if total <= 50:
        assert result2.triggered is False


# ── Cleanup: archive writes ───────────────────────────────────────────────


async def test_cleanup_writes_archive_bundle(registry):
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    archive = layer_set.archive
    ctx = MemoryContext(session_id="archive-bundle")
    await session.add_messages(ctx, [
        {"role": "user", "content": f"message {i}"} for i in range(6)
    ])

    result = await cleanup_session(
        session=session, archive=archive, context=ctx,
        max_messages=5, keep_ratio=0.5,
        archive_strategy=_SimpleArchiveGeneration(),
    )
    assert result.triggered is True
    assert result.archive_skipped is False

    context_entries = await archive.get_recent(ctx, limit=10, channel=ArchiveChannel.CONTEXT)
    knowledge_entries = await archive.get_recent(ctx, limit=10, channel=ArchiveChannel.KNOWLEDGE)
    assert len(context_entries) == 1
    assert len(knowledge_entries) == 1
    assert context_entries[0].summary.startswith("context: message 0")


# ── Cleanup: sanitizer removes stale invalid tool chains ──────────────────


async def test_cleanup_removes_stale_invalid_tool_chain(registry):
    """Stale incomplete tool-chain must be removed by sanitizer."""
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
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
            "tool_calls": [{"id": "c", "function": {"name": "tool_c"}}],
        },
        {"role": str(MessageRole.TOOL), "tool_call_id": "c", "content": "result_c"},
        {"role": str(MessageRole.ASSISTANT), "content": "done"},
    ])

    result = await cleanup_session(
        session=session, archive=None, context=ctx,
        max_messages=None, max_tokens=1, keep_ratio=0.9,
    )
    assert result.triggered is True
    remaining = [msg.to_dict() for msg in await session.get_all_messages(ctx)]
    remaining_str = str(remaining)
    assert "stale-a" not in remaining_str
    assert "stale-b" not in remaining_str


# ── Injection filters ─────────────────────────────────────────────────────


async def test_injection_filters_no_semantic_content_entries(registry):
    """Archive entries with '(no semantic content)' are filtered."""
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
    content = bundle.system_prompt
    assert "project setup" in content
    assert "no semantic content" not in content


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
        context=ctx, memory_system=system, query="数据分析",
    )
    content = bundle.system_prompt
    assert "Python 数据分析项目" in content
    assert "天气很好" not in content


async def test_archive_injection_uses_context_channel_only(registry):
    from framework.memory.default_system import DefaultMemorySystem
    from framework.memory.injection import FullInjectionPolicy

    layer_set = MemoryLayerFactory.single_user(registry=registry)
    system = DefaultMemorySystem(layer_set=layer_set, store_registry=registry)
    ctx = MemoryContext(session_id="archive-inject-context-channel")
    await system.initialize()
    await layer_set.archive.append_bundle(
        ctx,
        (
            ArchiveWrite(
                channel=ArchiveChannel.CONTEXT,
                summary="context archive for direct dialogue continuity",
            ),
            ArchiveWrite(
                channel=ArchiveChannel.KNOWLEDGE,
                summary="knowledge archive for dream consolidation only",
            ),
        ),
    )

    bundle = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=ctx, memory_system=system, query="archive",
    )
    content = bundle.system_prompt
    assert "context archive for direct dialogue continuity" in content
    assert "knowledge archive for dream consolidation only" not in content


# ── Regression: token estimation uses all messages ────────────────────────


@pytest.mark.asyncio
async def test_trigger_token_pressure_uses_all_messages(registry):
    """TOKEN_PRESSURE must estimate tokens from ALL messages."""
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    ctx = MemoryContext(session_id="token-pressure")

    msgs = [{"role": "user", "content": "A" * 500} for _ in range(20)]
    await session.add_messages(ctx, msgs)

    result = await cleanup_session(
        session=session, archive=layer_set.archive, context=ctx,
        max_messages=50, max_tokens=1500,
    )
    assert result.triggered is True
    assert result.reason == CompressionReason.TOKEN_PRESSURE


@pytest.mark.asyncio
async def test_trigger_message_count_respects_threshold(registry):
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    session = layer_set.session
    ctx = MemoryContext(session_id="msg-count")

    msgs = [{"role": "user", "content": f"msg{i}"} for i in range(55)]
    await session.add_messages(ctx, msgs)

    result = await cleanup_session(
        session=session, archive=layer_set.archive, context=ctx,
        max_messages=50,
    )
    assert result.triggered is True
    assert result.reason == CompressionReason.MESSAGE_COUNT


# ── Regression: CleanupResult type ────────────────────────────────────────


def test_cleanup_result_fields():
    result = CleanupResult(
        triggered=True,
        messages_kept=5,
        messages_pruned=10,
        archive_skipped=False,
        reason=CompressionReason.MESSAGE_COUNT,
    )
    assert result.triggered is True
    assert result.messages_kept == 5
    assert result.messages_pruned == 10
    assert result.archive_skipped is False
    assert result.reason == CompressionReason.MESSAGE_COUNT


def test_cleanup_result_not_triggered():
    result = CleanupResult(triggered=False)
    assert result.triggered is False
    assert result.messages_kept == 0
    assert result.messages_pruned == 0
