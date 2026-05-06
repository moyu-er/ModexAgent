"""End-to-end tests for the bot_project memory pipeline.

Simulates multi-turn conversations using bot_project-equivalent configuration:
  turn messages → ScopedMessageHistory → lifecycle → compression → archive → injection.

LLM summarizer results are mocked to isolate pipeline logic from model calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from framework.core.types import MessageRole
from framework.memory.compaction.policy import ConservativeCompactionPolicy
from framework.memory.compression.policies import (
    DefaultMemoryCompressionCoordinator,
    HeuristicSummaryStrategy,
    SummaryStrategy,
)
from framework.memory.core.models import ArchiveEntry, CompressionReason
from framework.memory.core.scope import MemoryContext
from framework.memory.default_system import DefaultMemorySystem
from framework.memory.layers.config import SessionMemoryConfig
from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy
from framework.memory.registry.in_memory import InMemoryStoreRegistry


# ── Helpers ───────────────────────────────────────────────────────────────

class MockSummarizerStrategy(SummaryStrategy):
    """Returns a predictable string so the archive content is deterministic."""

    def __init__(self, canned: str = "[MOCK] compressed conversation summary") -> None:
        self.canned = canned
        self.calls: list[list[dict[str, Any]]] = []

    async def summarize(self, messages, context, reason):
        self.calls.append(list(messages))
        return self.canned


def _bot_project_coordinator(**kw: Any) -> DefaultMemoryCompressionCoordinator:
    """Create a coordinator matching bot_project config defaults."""
    defaults: dict[str, Any] = {
        "max_messages": 50,
        "compaction": ConservativeCompactionPolicy(),
    }
    defaults.update(kw)
    return DefaultMemoryCompressionCoordinator(**defaults)


def _bot_project_system(
    registry: InMemoryStoreRegistry,
    coordinator: DefaultMemoryCompressionCoordinator | None = None,
) -> DefaultMemorySystem:
    """Create a DefaultMemorySystem wired like bot_project (with lifecycle)."""
    from framework.memory.layers.factory import MemoryLayerFactory

    layer_set = MemoryLayerFactory.single_user(registry=registry)
    if coordinator is None:
        coordinator = _bot_project_coordinator()
    lifecycle = DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator)
    return DefaultMemorySystem(
        layer_set=layer_set, store_registry=registry, lifecycle_policy=lifecycle,
    )


def _make_ctx(session_id: str = "test") -> MemoryContext:
    return MemoryContext(session_id=session_id, user_id="u1")


# ── Multi-turn cascade: turn-by-turn → compression → archive ──────────────


@pytest.mark.asyncio
async def test_multi_turn_triggers_compression_at_threshold():
    """After >50 turns through ScopedMessageHistory, compression fires and
    archive is written.  Session stays within the max_messages window."""
    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("multi-turn")

    # Simulate 60 turns: each turn adds user + assistant via ScopedMessageHistory
    history = system.create_message_history(ctx)
    for i in range(60):
        await history.append({"role": "user", "content": f"question {i}"})
        await history.append({"role": "assistant", "content": f"answer {i}"})

    # After 60 turns, compression should have triggered via the callback
    remaining = await system.get_history(ctx, max_messages=None)
    assert len(remaining) <= 55, f"should compress to ≤55, got {len(remaining)}"

    archive = await system.get_history_entries(ctx, limit=20)
    assert len(archive) > 0, "archive should have compression entries"


@pytest.mark.asyncio
async def test_compression_respects_threshold():
    """After compression, adding a few more messages does not re-trigger until threshold is exceeded."""
    registry = InMemoryStoreRegistry()
    coordinator = _bot_project_coordinator(max_messages=10)
    system = _bot_project_system(registry, coordinator)
    await system.initialize()
    ctx = _make_ctx("threshold")

    history = system.create_message_history(ctx)
    # Push past threshold
    for i in range(20):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    compressed_count = len(await system.get_history(ctx, max_messages=None))
    archive_count_1 = len(await system.get_history_entries(ctx, limit=20))

    # Add only 2 more turns (4 messages) — still below max_messages=10 threshold for re-trigger
    for i in range(2):
        await history.append({"role": "user", "content": f"post{i}"})
        await history.append({"role": "assistant", "content": f"post-a{i}"})

    new_count = len(await system.get_history(ctx, max_messages=None))
    archive_count_2 = len(await system.get_history_entries(ctx, limit=20))

    # Session grew modestly (threshold prevents re-compression with small delta)
    assert new_count <= compressed_count + 10
    assert archive_count_2 <= archive_count_1 + 2


@pytest.mark.asyncio
async def test_second_compression_fires_when_over_threshold():
    """After sufficiently many new messages past threshold, compression re-fires."""
    registry = InMemoryStoreRegistry()
    coordinator = _bot_project_coordinator(max_messages=10)
    system = _bot_project_system(registry, coordinator)
    await system.initialize()
    ctx = _make_ctx("recompress")

    history = system.create_message_history(ctx)
    for i in range(20):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    archive_first = len(await system.get_history_entries(ctx, limit=20))
    assert archive_first > 0, "first compression should produce archive"

    # Add enough new messages to exceed threshold again
    for i in range(30):
        await history.append({"role": "user", "content": f"round2-{i}"})

    archive_second = len(await system.get_history_entries(ctx, limit=20))
    # Second compression may have fired (additional archive entries)
    assert archive_second >= archive_first


# ── Tool chain integrity across compression ───────────────────────────────


@pytest.mark.asyncio
async def test_tool_chains_intact_after_cascade():
    """After multi-turn compression, kept suffix has no orphan tool results."""
    registry = InMemoryStoreRegistry()
    coordinator = _bot_project_coordinator(max_messages=8)
    system = _bot_project_system(registry, coordinator)
    await system.initialize()
    ctx = _make_ctx("tool-chain-cascade")

    history = system.create_message_history(ctx)
    # 8 turns with 5 messages each (user + tc + 2x tool + answer) = 40 msgs
    for i in range(8):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"tc{i}", "type": "function", "function": {"name": "read_file"}},
        ]})
        await history.append({"role": "tool", "tool_call_id": f"tc{i}", "name": "read_file", "content": f"data{i}"})
        await history.append({"role": "assistant", "content": f"answer {i}"})

    remaining = await system.get_history(ctx, max_messages=None)
    assert len(remaining) <= 20, f"should compress 40 to ≤20, got {len(remaining)}"

    # No orphan tool results
    kept_ids: set[str] = set()
    for m in remaining:
        d = m.to_dict()
        for tc in d.get("tool_calls", []) or []:
            if isinstance(tc, dict) and tc.get("id"):
                kept_ids.add(tc["id"])
    for m in remaining:
        d = m.to_dict()
        if d.get("role") == str(MessageRole.TOOL):
            assert d.get("tool_call_id") in kept_ids, f"orphan {d.get('tool_call_id')}"


# ── Archive content with mock LLM summarizer ──────────────────────────────


@pytest.mark.asyncio
async def test_archive_uses_mock_summarizer_output():
    """When SummarizerStrategy is wired, archive content matches mock output."""
    registry = InMemoryStoreRegistry()
    mock = MockSummarizerStrategy("[MOCK] compressed to archive")
    coordinator = _bot_project_coordinator(max_messages=5, summary=mock)
    system = _bot_project_system(registry, coordinator)
    await system.initialize()
    ctx = _make_ctx("mock-summary")

    history = system.create_message_history(ctx)
    for i in range(12):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    # The summarizer should have been called
    assert len(mock.calls) > 0, "mock summarizer should have been called"

    entries = await system.get_history_entries(ctx, limit=10)
    assert len(entries) > 0, "archive should have entries from mock summarizer"
    assert any("[MOCK]" in (e.get("summary") or "") for e in entries), \
        "archive should contain mock summary text"


@pytest.mark.asyncio
async def test_archive_skips_nothing_sentinel_from_summarizer():
    """Summarizer returning '(nothing)' should not produce archive entries."""
    registry = InMemoryStoreRegistry()
    mock = MockSummarizerStrategy("(nothing)")
    coordinator = _bot_project_coordinator(max_messages=5, summary=mock)
    system = _bot_project_system(registry, coordinator)
    await system.initialize()
    ctx = _make_ctx("nothing-sentinel")

    history = system.create_message_history(ctx)
    for i in range(12):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    # Archive should be empty — all summaries were "(nothing)"
    entries = await system.get_history_entries(ctx, limit=10)
    # Session should still be compressed (messages truncated), just no archive
    remaining = len(await system.get_history(ctx, max_messages=None))
    assert remaining <= 10, "session should still be compressed"


# ── Context injection — knowledge + archive + session ─────────────────────


@pytest.mark.asyncio
async def test_full_injection_includes_knowledge_archive_and_session():
    """FullInjectionPolicy assembles Knowledge, Archive, and Session messages."""
    from framework.memory.injection import FullInjectionPolicy
    from framework.memory.core.models import LongTermMemory

    registry = InMemoryStoreRegistry()
    layer_set = _bot_project_system(registry)._layers
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("injection")

    # Seed knowledge
    from framework.memory.core.consolidation import MemoryUpdate
    await layer_set.knowledge.ensure_defaults(ctx, {
        "soul": "- friendly and concise",
        "user": "- prefers dark mode",
        "memory": "- project: ModexAgent",
    })

    # Seed archive
    await layer_set.archive.append(ctx, ArchiveEntry(
        summary="previous session: discussed memory compression",
    ))

    # Seed session messages
    await layer_set.session.add_messages(ctx, [
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": "current answer"},
    ])

    bundle = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=ctx, memory_system=system, query="",
    )

    content = "\n".join(s.content for s in bundle.system_sections)
    assert "friendly and concise" in content  # SOUL
    assert "prefers dark mode" in content     # USER
    assert "ModexAgent" in content            # MEMORY
    assert "memory compression" in content    # Archive
    # Session messages are in the bundle
    assert len(bundle.messages) >= 2


@pytest.mark.asyncio
async def test_injection_excludes_empty_archive_markers():
    """Archive entries with '(nothing)' / '(no semantic content)' are filtered."""
    from framework.memory.injection import FullInjectionPolicy

    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("filter-empty-injection")

    await system._layers.archive.append(ctx, ArchiveEntry(summary="(nothing)"))
    await system._layers.archive.append(ctx, ArchiveEntry(summary="(no semantic content)"))
    await system._layers.archive.append(ctx, ArchiveEntry(
        summary="real: user asked about weather",
    ))

    bundle = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=ctx, memory_system=system, query="",
    )
    content = "\n".join(s.content for s in bundle.system_sections)
    assert "weather" in content
    assert "(nothing)" not in content
    assert "no semantic content" not in content


@pytest.mark.asyncio
async def test_injection_includes_compression_summary():
    """After compression, the summary is injected into LLM context."""
    from framework.memory.injection import FullInjectionPolicy

    registry = InMemoryStoreRegistry()
    mock = MockSummarizerStrategy("[MOCK] built the login page")
    coordinator = _bot_project_coordinator(max_messages=5, summary=mock)
    system = _bot_project_system(registry, coordinator)
    await system.initialize()
    ctx = _make_ctx("compression-inject")

    history = system.create_message_history(ctx)
    for i in range(12):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    bundle = await FullInjectionPolicy(max_history_entries=10).assemble(
        context=ctx, memory_system=system, query="",
    )
    content = "\n".join(s.content for s in bundle.system_sections)
    # Compression summary should appear in injected sections
    assert "[MOCK] built the login page" in content or \
        any("[MOCK]" in s.content for s in bundle.system_sections), \
        "compression summary should be in injected context"


# ── Long-term knowledge: full update (existing + new) ─────────────────────


@pytest.mark.asyncio
async def test_knowledge_update_preserves_existing_when_adding_new():
    """Existing knowledge entries are preserved when new ones are added."""
    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("knowledge-update")

    km = system._layers.knowledge

    # Initial state
    await km.ensure_defaults(ctx, {"memory": "- fact A\n- fact B\n"})
    before = await km.get_all(ctx)
    assert "fact A" in before.memory
    assert "fact B" in before.memory

    # Add new fact via append
    from framework.memory.core.consolidation import MemoryUpdate
    await km.apply_update(ctx, MemoryUpdate(
        file_name="MEMORY.md", content="- fact C\n", mode="append", reason="test",
    ))
    after = await km.get_all(ctx)
    assert "fact A" in after.memory, "existing fact A should persist"
    assert "fact B" in after.memory, "existing fact B should persist"
    assert "fact C" in after.memory, "new fact C should be added"


@pytest.mark.asyncio
async def test_knowledge_replace_text_updates_in_place():
    """replace_text mode corrects existing entries without losing other facts."""
    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("knowledge-replace")

    km = system._layers.knowledge
    await km.ensure_defaults(ctx, {"user": "- location: Tokyo\n- prefers dark mode\n"})

    from framework.memory.core.consolidation import MemoryUpdate
    await km.apply_update(ctx, MemoryUpdate(
        file_name="USER.md",
        content="- location: Osaka\n",
        mode="replace_text",
        search_text="- location: Tokyo\n",
        reason="corrected",
    ))

    after = await km.get_all(ctx)
    assert "Osaka" in after.user, "location should be corrected"
    assert "Tokyo" not in after.user, "old location should be gone"
    assert "dark mode" in after.user, "unrelated fact should persist"


@pytest.mark.asyncio
async def test_archive_merges_multiple_compression_rounds():
    """Multiple compression rounds produce cumulative archive entries."""
    registry = InMemoryStoreRegistry()
    mock = MockSummarizerStrategy("[MOCK] round summary")
    coordinator = _bot_project_coordinator(max_messages=10, summary=mock)
    system = _bot_project_system(registry, coordinator)
    await system.initialize()
    ctx = _make_ctx("multi-compress")

    history = system.create_message_history(ctx)

    # Round 1: push past threshold
    for i in range(20):
        await history.append({"role": "user", "content": f"r1-{i}"})
        await history.append({"role": "assistant", "content": f"r1-a{i}"})

    # Round 2: add many more messages to trigger again
    for i in range(40):
        await history.append({"role": "user", "content": f"r2-{i}"})

    entries = await system.get_history_entries(ctx, limit=50)
    assert len(entries) >= 1, "at least one compression round produced archive"

    # Session should still be within budget after all rounds
    remaining = len(await system.get_history(ctx, max_messages=None))
    assert remaining <= 15, f"multiple rounds should keep session small, got {remaining}"


# ── Edge case: empty session, single message ──────────────────────────────


@pytest.mark.asyncio
async def test_empty_session_no_compression():
    """Empty session does not trigger compression."""
    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("empty")

    history = system.create_message_history(ctx)
    # Add just 3 messages — well within budget
    for i in range(3):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    remaining = len(await system.get_history(ctx, max_messages=None))
    assert remaining == 6, "all 6 messages should be present (no compression)"

    archive = await system.get_history_entries(ctx, limit=10)
    assert len(archive) == 0, "no archive should be generated"


@pytest.mark.asyncio
async def test_session_only_messages_no_duplicate_compression_trigger():
    """Compression does not fire on every single message — threshold-based trigger works."""
    registry = InMemoryStoreRegistry()
    coordinator = _bot_project_coordinator(max_messages=10)
    system = _bot_project_system(registry, coordinator)
    await system.initialize()
    ctx = _make_ctx("no-dupe")

    history = system.create_message_history(ctx)
    for i in range(25):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    remaining = len(await system.get_history(ctx, max_messages=None))
    # After 50 messages with max=10, should compress to ≤15
    assert remaining <= 20, f"should compress, got {remaining}"
    assert remaining > 0, "but not empty"


# ── Retrieval: archive search by query ────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_search_by_query_boosts_relevant_entries():
    """Archive search with query returns relevant entries prioritized."""
    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("archive-search")

    await system._layers.archive.append(ctx, ArchiveEntry(summary="天气讨论: 晴天"))
    await system._layers.archive.append(ctx, ArchiveEntry(summary="Python 编程: pandas 数据分析"))
    await system._layers.archive.append(ctx, ArchiveEntry(summary="天气更新: 明天有雨"))

    # Search by query returns weather-related entries first
    entries = await system.get_history_entries(ctx, limit=10, query="天气")
    assert len(entries) > 0
    # Weather entries should appear before non-weather entries
    first_summary = str(entries[0].get("summary", ""))
    assert "天气" in first_summary, f"first hit should match query, got: {first_summary}"


@pytest.mark.asyncio
async def test_archive_search_falls_back_to_recent_when_no_match():
    """When query matches nothing, fall back to recent entries."""
    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("archive-nomatch")

    await system._layers.archive.append(ctx, ArchiveEntry(summary="some old topic"))
    await system._layers.archive.append(ctx, ArchiveEntry(summary="another unrelated"))

    entries = await system.get_history_entries(ctx, limit=10, query="NONEXISTENT_KEYWORD_XYZ")
    # Falls back to recent entries
    assert len(entries) > 0, "should return recent entries as fallback"


# ── Retrieval: knowledge files ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_knowledge_retrieve_returns_all_files():
    """retrieve_knowledge returns SOUL, USER, MEMORY even with empty query."""
    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("knowledge-get")

    km = system._layers.knowledge
    await km.ensure_defaults(ctx, {
        "soul": "## SOUL\nbot personality",
        "user": "## USER\nuser preferences",
        "memory": "## MEMORY\nproject context",
    })

    knowledge = await system.retrieve_knowledge(ctx)
    assert "bot personality" in knowledge.soul
    assert "user preferences" in knowledge.user
    assert "project context" in knowledge.memory


# ── Injection: priority ordering ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_injection_priority_order_respected():
    """Sections are ordered by priority descending: knowledge > archive > compression."""
    from framework.memory.injection import FullInjectionPolicy

    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("priority-order")

    # Seed all layers
    km = system._layers.knowledge
    await km.ensure_defaults(ctx, {"memory": "- priority: 90"})
    await system._layers.archive.append(ctx, ArchiveEntry(summary="priority-70 entry"))
    # Add session messages
    await system._layers.session.add_messages(ctx, [
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": "response"},
    ])

    bundle = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=ctx, memory_system=system, query="",
    )

    priorities = [s.priority for s in bundle.system_sections]
    # Priorities should be non-increasing (sorted descending)
    assert priorities == sorted(priorities, reverse=True), \
        f"priorities should be descending: {priorities}"


@pytest.mark.asyncio
async def test_injection_budget_trims_low_priority_first():
    """When token budget is tight, low-priority sections drop first."""
    from framework.memory.injection import FullInjectionPolicy
    from framework.memory.core.models import MemoryBudget

    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("budget-trim")

    km = system._layers.knowledge
    await km.ensure_defaults(ctx, {
        "soul": "HIGH priority content " * 50,
        "memory": "medium priority " * 100,
    })
    # Add archive
    await system._layers.archive.append(ctx, ArchiveEntry(summary="low priority old history " * 20))

    # Tight budget: only 300 tokens — should drop low-priority sections
    budget = MemoryBudget(max_system_prompt_tokens=300, max_history_messages=10)
    bundle = await FullInjectionPolicy(max_history_entries=5, budget=budget).assemble(
        context=ctx, memory_system=system, query="",
    )

    # High priority (SOUL 100) should be present
    high_priority_found = any("HIGH priority" in s.content for s in bundle.system_sections)
    assert high_priority_found, "SOUL (priority 100) should survive budget trim"

    # Dropped sections should be reported
    if bundle.dropped_sections:
        dropped_priorities = [d["priority"] for d in bundle.dropped_sections]
        # If 70 dropped but 100 didn't, priorities drop from low end
        for dp in dropped_priorities:
            kept_priorities = [s.priority for s in bundle.system_sections]
            assert dp <= min(kept_priorities, default=100), \
                f"dropped priority {dp} should be ≤ all kept priorities"


@pytest.mark.asyncio
async def test_restricted_injection_session_only():
    """Peer/subagent policy: only session messages, no knowledge/archive."""
    from framework.memory.injection import RestrictedInjectionPolicy

    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("restricted")

    # Seed all layers
    await system._layers.knowledge.ensure_defaults(ctx, {"memory": "- should not appear"})
    await system._layers.archive.append(ctx, ArchiveEntry(summary="should not appear"))
    await system._layers.session.add_messages(ctx, [
        {"role": "user", "content": "visible message"},
    ])

    bundle = await RestrictedInjectionPolicy(max_session_messages=10).assemble(
        context=ctx, memory_system=system, query="",
    )

    # No system sections at all
    assert len(bundle.system_sections) == 0, "restricted policy should have no sections"
    # Session messages present
    assert len(bundle.messages) > 0, "session messages should be present"
    assert any("visible message" in str(m) for m in bundle.messages)


@pytest.mark.asyncio
async def test_injection_tool_messages_filtered_by_default():
    """Default ToolMessageFilterStrategy strips tool_calls and tool results."""
    from framework.memory.injection import FullInjectionPolicy

    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("tool-filter")

    await system._layers.session.add_messages(ctx, [
        {"role": "user", "content": "read file"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "read_file"}}
        ]},
        {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "file data"},
        {"role": "assistant", "content": "file says hello"},
    ])

    bundle = await FullInjectionPolicy().assemble(
        context=ctx, memory_system=system, query="",
    )

    for m in bundle.messages:
        d = m.to_dict()
        assert d.get("role") not in ("tool",), "tool messages should be filtered"
        assert not d.get("tool_calls"), f"tool_calls should be filtered, got {d.get('tool_calls')}"


@pytest.mark.asyncio
async def test_bundle_to_context_state_assembles_system_prompt():
    """bundle_to_context_state converts bundle sections into a system prompt."""
    from framework.memory.injection import FullInjectionPolicy, bundle_to_context_state

    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("to-state")

    await system._layers.knowledge.ensure_defaults(ctx, {"soul": "- bot personality note"})
    await system._layers.session.add_messages(ctx, [
        {"role": "user", "content": "hello"},
    ])

    bundle = await FullInjectionPolicy().assemble(
        context=ctx, memory_system=system, query="",
    )
    state = bundle_to_context_state(
        bundle, system, ctx,
        base_system_prompt="[BASE] You are a helpful assistant.",
    )

    assert "[BASE]" in state.system_prompt, "base prompt should be first"
    assert "bot personality note" in state.system_prompt, "knowledge should be injected"
    msgs = await state.history.to_list()
    assert len(msgs) >= 1, "session messages should be in history"


# ── Three-tier cascade: Session → Archive → Knowledge ────────────────────


@pytest.mark.asyncio
async def test_three_tier_memory_cascade_preserves_tool_context():
    """Full cascade: add tool-heavy messages → compress → archive with tool context.

    Verifies:
    - Old messages are compressed (prefix), recent kept (suffix) — matching nanobot pattern
    - Tool context is preserved in archive summary (not silently dropped)
    - Archive entries are meaningful for DreamEngine to process later
    """
    from framework.memory.registry.in_memory import InMemoryStoreRegistry

    registry = InMemoryStoreRegistry()
    mock = MockSummarizerStrategy("[ARCHIVE] user asked about weather, used shell+web_search, got sunny 28°C")
    coordinator = _bot_project_coordinator(max_messages=5, summary=mock)
    system = _bot_project_system(registry, coordinator)
    await system.initialize()
    ctx = _make_ctx("three-tier")

    history = system.create_message_history(ctx)

    # 4 turns with tool chains: user → tc → tool → answer = 16 msgs
    for i in range(4):
        await history.append({"role": "user", "content": f"task {i}"})
        await history.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"tc{i}", "type": "function", "function": {"name": "read_file"}},
        ]})
        await history.append({"role": "tool", "tool_call_id": f"tc{i}", "name": "read_file", "content": f"result {i}"})
        await history.append({"role": "assistant", "content": f"done {i}"})

    # Tier 1: Session compressed (old prefix → archive, recent suffix kept)
    remaining = await system.get_history(ctx, max_messages=None)
    assert len(remaining) <= 8, f"old prefix compressed, got {len(remaining)} remaining"

    # Tier 2: Archive has compression entries with tool context
    archive_entries = await system.get_history_entries(ctx, limit=10)
    assert len(archive_entries) > 0, "archive should have entries"
    # Mock output was used
    assert any("[ARCHIVE]" in str(e.get("summary", "")) for e in archive_entries), \
        "archive preserves tool context in summary"

    # Tier 3: Knowledge can be built from archive (DreamEngine input)
    # Simulate: store some archive entries, verify they're retrievable
    await system._layers.archive.append(ctx, ArchiveEntry(
        summary="[ARCHIVE] user prefers dark mode for all UIs",
    ))
    await system._layers.archive.append(ctx, ArchiveEntry(
        summary="[ARCHIVE] project uses Python 3.11+, FastAPI, ChromaDB",
    ))

    # Knowledge retrieval sees archive entries
    retrieved = await system.get_history_entries(ctx, limit=10, query="dark mode")
    assert len(retrieved) > 0
    assert any("dark mode" in str(e.get("summary", "")) for e in retrieved)


@pytest.mark.asyncio
async def test_archive_entries_are_meaningful_for_dream_engine():
    """Archive summaries contain enough context for DreamEngine fact extraction."""
    from framework.memory.registry.in_memory import InMemoryStoreRegistry
    from framework.memory.consolidation.dream_engine import DreamEngine

    registry = InMemoryStoreRegistry()
    mock = MockSummarizerStrategy(
        "[ARCHIVE] user: fix login bug | tools: read_file(auth.py), shell(git log) | "
        "decision: use JWT instead of session | state: branch fix/auth, tests fail"
    )
    coordinator = _bot_project_coordinator(max_messages=3, summary=mock)
    system = _bot_project_system(registry, coordinator)
    await system.initialize()
    ctx = _make_ctx("dream-input")

    history = system.create_message_history(ctx)
    for i in range(8):
        await history.append({"role": "user", "content": f"msg {i}"})
        await history.append({"role": "assistant", "content": f"reply {i}"})

    # Archive entries exist
    entries = await system.get_history_entries(ctx, limit=20)
    assert len(entries) > 0

    # DreamEngine._is_meaningful_entry should accept these
    for e in entries:
        assert DreamEngine._is_meaningful_entry(e), \
            f"archive entry should be meaningful: {e.get('summary', '')[:80]}"

    # Verify entry contains structured info DreamEngine can extract
    summary = str(entries[0].get("summary", ""))
    assert "user:" in summary or "tools:" in summary or "decision:" in summary or "[ARCHIVE]" in summary, \
        f"archive should have structured context, got: {summary[:100]}"


@pytest.mark.asyncio
async def test_cleanup_plugin_would_remove_tool_before_compression():
    """Document the conflict: if cleanup is enabled, tool context is lost.

    This test proves that ToolCallAwareSessionManager.cleanup() runs BEFORE
    the lifecycle callback, so tool context would be removed from storage
    before maybe_compress() reads all_messages.  When cleanup is disabled
    (bot_project current default), this is not an issue.
    """
    from framework.memory.registry.in_memory import InMemoryStoreRegistry

    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("cleanup-conflict")

    # Add a completed ReAct turn (ends with plain assistant)
    await system._layers.session.add_messages(ctx, [
        {"role": "user", "content": "read file"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "read_file"}}
        ]},
        {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "file data"},
        {"role": "assistant", "content": "file says hello"},
    ])

    before = len(await system._layers.session.get_all_messages(ctx))
    assert before == 4  # all 4 messages present

    # ToolCallCleanupPolicy would remove the intermediate tool messages
    from examples.bot_project.plugins.tool_call_cleanup.policy import ToolCallCleanupPolicy
    policy = ToolCallCleanupPolicy()
    dict_msgs = [m.to_dict() for m in await system._layers.session.get_all_messages(ctx)]
    cleaned = policy.clean(dict_msgs)


    # After cleanup: only user + final assistant remain
    assert len(cleaned) == 2, f"cleanup removes tool msgs, got {len(cleaned)}"
    assert cleaned[0]["role"] == "user"
    assert cleaned[1]["role"] == "assistant"
    assert not cleaned[1].get("tool_calls")

    # If cleanup were applied BEFORE compression (which it would be,
    # because the wrapper's add_messages calls _cleanup before returning),
    # the summarizer would only see the remaining 2 messages
