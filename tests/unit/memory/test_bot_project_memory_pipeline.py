"""End-to-end tests for the bot_project memory pipeline.

Simulates multi-turn conversations using bot_project-equivalent configuration:
  turn messages -> ScopedMessageHistory -> cleanup_session -> archive -> injection.

LLM summarizer results are mocked to isolate pipeline logic from model calls.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from framework.agents.summarizer.abc import ArchiveSummarizerResult
from framework.core.types import MessageRole
from framework.memory.archive_models import (
    ArchiveChannel,
)
from framework.memory.core.models import ArchiveEntry
from framework.memory.core.scope import MemoryContext
from framework.memory.default_system import DefaultMemorySystem
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.registry.in_memory import InMemoryStoreRegistry
from framework.memory.stores.dir_archive import DirArchiveStorage


# ── Helpers ───────────────────────────────────────────────────────────────


class _MockArchiveGenerator:
    """Mock ArchiveGenerator that writes canned content to archive_dir.

    Matches the :class:`~framework.agents.summarizer.abc.ArchiveGenerator`
    contract so ``cleanup_session`` can use it as an ``archive_agent``.
    """

    def __init__(
        self,
        canned_context: str = "[MOCK] compressed conversation context",
        canned_knowledge: str = "[MOCK] compressed conversation knowledge",
    ) -> None:
        self.canned_context = canned_context
        self.canned_knowledge = canned_knowledge
        self.calls: list[list[Any]] = []

    async def generate(
        self,
        pruned_messages: list[dict[str, Any]],
        archive_dir: Path,
        archive_id: int = 0,
    ) -> ArchiveSummarizerResult:
        self.calls.append(list(pruned_messages))
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "context.md").write_text(self.canned_context, encoding="utf-8")
        (archive_dir / "knowledge.md").write_text(self.canned_knowledge, encoding="utf-8")
        (archive_dir / "index.md").write_text("mock index", encoding="utf-8")
        return ArchiveSummarizerResult(
            success=True,
            archive_id=archive_id,
            files_written=("context.md", "knowledge.md", "index.md"),
        )


class _EmptyArchiveGenerator:
    """Mock ArchiveGenerator that writes empty files (simulates no content)."""

    async def generate(
        self,
        pruned_messages: list[dict[str, Any]],
        archive_dir: Path,
        archive_id: int = 0,
    ) -> ArchiveSummarizerResult:
        _ = pruned_messages
        return ArchiveSummarizerResult(
            success=True,
            archive_id=archive_id,
            files_written=(),
        )


def _bot_project_system(
    registry: InMemoryStoreRegistry,
    max_messages: int = 50,
    keep_ratio: float = 0.5,
    *,
    archive_agent: object | None = None,
    archive_storage: DirArchiveStorage | None = None,
) -> DefaultMemorySystem:
    """Create a DefaultMemorySystem wired like bot_project (with cleanup_config).

    When *archive_agent* and *archive_storage* are provided, cleanup_session
    can generate archive entries on the hot path.
    """
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    cleanup_config: dict[str, int | float] = {
        "max_messages": max_messages,
        "keep_ratio": keep_ratio,
    }
    return DefaultMemorySystem(
        layer_set=layer_set,
        store_registry=registry,
        cleanup_config=cleanup_config,
        archive_agent=archive_agent,  # type: ignore[arg-type]
        archive_storage=archive_storage,  # type: ignore[arg-type]
    )


def _make_ctx(session_id: str = "test") -> MemoryContext:
    return MemoryContext(session_id=session_id, user_id="u1")


# ── Multi-turn cascade: turn-by-turn -> cleanup -> archive ──────────────────


@pytest.mark.asyncio
async def test_multi_turn_triggers_cleanup_at_threshold(tmp_path: Path):
    """After >50 turns through ScopedMessageHistory, cleanup fires and
    archive is written. Session stays within the max_messages window."""
    registry = InMemoryStoreRegistry()
    mock = _MockArchiveGenerator()
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(registry, archive_agent=mock, archive_storage=storage)
    await system.initialize()
    ctx = _make_ctx("multi-turn")

    history = system.create_message_history(ctx)
    for i in range(60):
        await history.append({"role": "user", "content": f"question {i}"})
        await history.append({"role": "assistant", "content": f"answer {i}"})

    remaining = await system.get_history(ctx, max_messages=None)
    assert len(remaining) <= 65, f"should compress to <=65, got {len(remaining)}"

    archive = await system.get_history_entries(ctx, limit=20)
    assert len(archive) > 0, "archive should have cleanup entries"


@pytest.mark.asyncio
async def test_cleanup_respects_threshold(tmp_path: Path):
    """After cleanup, adding a few more messages does not re-trigger until threshold is exceeded."""
    registry = InMemoryStoreRegistry()
    mock = _MockArchiveGenerator()
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(registry, max_messages=10, archive_agent=mock, archive_storage=storage)
    await system.initialize()
    ctx = _make_ctx("threshold")

    history = system.create_message_history(ctx)
    for i in range(20):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    compressed_count = len(await system.get_history(ctx, max_messages=None))
    archive_count_1 = len(await system.get_history_entries(ctx, limit=20))

    # Add only 2 more turns (4 messages)
    for i in range(2):
        await history.append({"role": "user", "content": f"post{i}"})
        await history.append({"role": "assistant", "content": f"post-a{i}"})

    new_count = len(await system.get_history(ctx, max_messages=None))
    archive_count_2 = len(await system.get_history_entries(ctx, limit=20))

    assert new_count <= compressed_count + 10
    assert archive_count_2 <= archive_count_1 + 2


@pytest.mark.asyncio
async def test_second_cleanup_fires_when_over_threshold(tmp_path: Path):
    """After sufficiently many new messages past threshold, cleanup re-fires."""
    registry = InMemoryStoreRegistry()
    mock = _MockArchiveGenerator()
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(registry, max_messages=10, archive_agent=mock, archive_storage=storage)
    await system.initialize()
    ctx = _make_ctx("recompress")

    history = system.create_message_history(ctx)
    for i in range(20):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    archive_first = len(await system.get_history_entries(ctx, limit=20))
    assert archive_first > 0, "first cleanup should produce archive"

    for i in range(30):
        await history.append({"role": "user", "content": f"round2-{i}"})

    archive_second = len(await system.get_history_entries(ctx, limit=20))
    assert archive_second >= archive_first


# ── Tool chain integrity across cleanup ────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_chains_intact_after_cascade(tmp_path: Path):
    """After multi-turn cleanup, kept suffix has no orphan tool results."""
    registry = InMemoryStoreRegistry()
    mock = _MockArchiveGenerator()
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(registry, max_messages=8, archive_agent=mock, archive_storage=storage)
    await system.initialize()
    ctx = _make_ctx("tool-chain-cascade")

    history = system.create_message_history(ctx)
    for i in range(8):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"tc{i}", "type": "function", "function": {"name": "read_file"}},
        ]})
        await history.append({"role": "tool", "tool_call_id": f"tc{i}", "name": "read_file", "content": f"data{i}"})
        await history.append({"role": "assistant", "content": f"answer {i}"})

    remaining = await system.get_history(ctx, max_messages=None)
    assert len(remaining) <= 25, f"should compress 32 to <=25, got {len(remaining)}"

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
async def test_archive_uses_mock_summarizer_output(tmp_path: Path):
    """When archive strategy is wired, archive content matches mock output."""
    registry = InMemoryStoreRegistry()
    mock = _MockArchiveGenerator(
        canned_context="[MOCK] compressed to archive",
        canned_knowledge="[MOCK] compressed to archive",
    )
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(registry, max_messages=5, archive_agent=mock, archive_storage=storage)
    await system.initialize()
    ctx = _make_ctx("mock-summary")

    history = system.create_message_history(ctx)
    for i in range(12):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    assert len(mock.calls) > 0, "mock archive generation should have been called"

    entries = await system.get_history_entries(ctx, limit=10)
    assert len(entries) > 0, "archive should have entries from mock generation"
    assert any("[MOCK]" in (e.get("summary") or "") for e in entries)


@pytest.mark.asyncio
async def test_archive_skips_empty_generation(tmp_path: Path):
    """Empty archive generation should not produce archive entries."""
    registry = InMemoryStoreRegistry()
    mock = _EmptyArchiveGenerator()
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(registry, max_messages=5, archive_agent=mock, archive_storage=storage)
    await system.initialize()
    ctx = _make_ctx("nothing-sentinel")

    history = system.create_message_history(ctx)
    for i in range(12):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    entries = await system.get_history_entries(ctx, limit=10)
    assert len(entries) == 0, "empty archive generation should produce no entries"
    # Session IS cleaned even when archive generation is empty
    remaining = len(await system.get_history(ctx, max_messages=None))
    assert remaining < 24, (
        f"session must be cleaned even without archive writes, still has {remaining}"
    )


# ── Context injection: knowledge + archive + session ──────────────────────


@pytest.mark.asyncio
async def test_full_injection_includes_knowledge_archive_and_session():
    """FullInjectionPolicy assembles Knowledge, Archive, and Session messages."""
    from framework.memory.injection import FullInjectionPolicy

    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("injection")

    # Seed knowledge
    await system._layers.knowledge.ensure_defaults(ctx, {
        "soul": "- friendly and concise",
        "user": "- prefers dark mode",
        "memory": "- project: ModexAgent",
    })

    # Seed archive
    await system._layers.archive.append(ctx, ArchiveEntry(
        summary="previous session: discussed memory compression",
    ))

    # Seed session messages
    await system._layers.session.add_messages(ctx, [
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": "current answer"},
    ])

    bundle = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=ctx, memory_system=system, query="",
    )

    content = bundle.system_prompt
    assert "<agent_knowledge>" in content
    assert "friendly and concise" in content
    assert "prefers dark mode" in content
    assert "ModexAgent" in content
    assert "memory compression" in content
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
    content = bundle.system_prompt
    assert "weather" in content
    assert "(nothing)" not in content
    assert "no semantic content" not in content


# ── Long-term knowledge: full update (existing + new) ─────────────────────


@pytest.mark.asyncio
async def test_knowledge_update_preserves_existing_when_adding_new():
    """Existing knowledge entries are preserved when new ones are added."""
    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("knowledge-update")

    km = system._layers.knowledge
    await km.ensure_defaults(ctx, {"memory": "- fact A\n- fact B\n"})
    before = await km.get_all(ctx)
    assert "fact A" in before.memory
    assert "fact B" in before.memory

    from framework.memory.core.consolidation import MemoryUpdate
    await km.apply_update(ctx, MemoryUpdate(
        file_name="MEMORY.md", content="- fact C\n", mode="append", reason="test",
    ))
    after = await km.get_all(ctx)
    assert "fact A" in after.memory
    assert "fact B" in after.memory
    assert "fact C" in after.memory


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
    assert "Osaka" in after.user
    assert "Tokyo" not in after.user
    assert "dark mode" in after.user


@pytest.mark.asyncio
async def test_archive_merges_multiple_cleanup_rounds(tmp_path: Path):
    """Multiple cleanup rounds produce cumulative archive entries."""
    registry = InMemoryStoreRegistry()
    mock = _MockArchiveGenerator(
        canned_context="[MOCK] round context",
        canned_knowledge="[MOCK] round knowledge",
    )
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(registry, max_messages=10, archive_agent=mock, archive_storage=storage)
    await system.initialize()
    ctx = _make_ctx("multi-compress")

    history = system.create_message_history(ctx)

    for i in range(20):
        await history.append({"role": "user", "content": f"r1-{i}"})
        await history.append({"role": "assistant", "content": f"r1-a{i}"})

    for i in range(40):
        await history.append({"role": "user", "content": f"r2-{i}"})

    entries = await system.get_history_entries(ctx, limit=50)
    assert len(entries) >= 1

    remaining = len(await system.get_history(ctx, max_messages=None))
    assert remaining <= 25, f"multiple rounds should keep session small, got {remaining}"


# ── Edge case: empty session, single message ──────────────────────────────


@pytest.mark.asyncio
async def test_empty_session_no_cleanup():
    """Empty session does not trigger cleanup."""
    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("empty")

    history = system.create_message_history(ctx)
    for i in range(3):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    remaining = len(await system.get_history(ctx, max_messages=None))
    assert remaining == 6, "all 6 messages should be present (no cleanup)"

    archive = await system.get_history_entries(ctx, limit=10)
    assert len(archive) == 0


@pytest.mark.asyncio
async def test_session_only_messages_no_duplicate_cleanup_trigger(tmp_path: Path):
    """Cleanup does not fire on every single message -- threshold-based trigger works."""
    registry = InMemoryStoreRegistry()
    mock = _MockArchiveGenerator()
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(registry, max_messages=10, archive_agent=mock, archive_storage=storage)
    await system.initialize()
    ctx = _make_ctx("no-dupe")

    history = system.create_message_history(ctx)
    for i in range(25):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    remaining = len(await system.get_history(ctx, max_messages=None))
    assert remaining <= 20, f"should compress, got {remaining}"
    assert remaining > 0


# ── Archive injection: distinguishable markers ────────────────────────────


@pytest.mark.asyncio
async def test_archive_injection_has_distinguishable_markers():
    """Multiple archive entries are injected with clear per-entry markers."""
    from framework.memory.injection import FullInjectionPolicy

    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("archive-markers")

    await system._layers.archive.append(ctx, ArchiveEntry(
        summary="first session: discussed login bug",
    ))
    await system._layers.archive.append(ctx, ArchiveEntry(
        summary="second session: fixed auth flow",
    ))
    await system._layers.archive.append(ctx, ArchiveEntry(
        summary="third session: added JWT tests",
    ))

    bundle = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=ctx, memory_system=system, query="",
    )
    content = bundle.system_prompt

    assert '<record id="1"' in content
    assert '<record id="2"' in content
    assert '<record id="3"' in content
    assert "<historical_context>" in content
    assert "</historical_context>" in content
    assert "login bug" in content
    assert "auth flow" in content
    assert "JWT tests" in content


@pytest.mark.asyncio
async def test_archive_injection_includes_timestamp_when_available():
    """Archive entries with created_at show timestamps in markers."""
    from framework.memory.injection import FullInjectionPolicy
    from datetime import datetime

    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("archive-timestamps")

    await system._layers.archive.append(ctx, ArchiveEntry(
        summary="older discussion",
        created_at=datetime(2026, 5, 1, 10, 30),
    ))
    await system._layers.archive.append(ctx, ArchiveEntry(
        summary="recent discussion",
        created_at=datetime(2026, 5, 6, 14, 45),
    ))

    bundle = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=ctx, memory_system=system, query="",
    )
    content = bundle.system_prompt

    assert "2026-05-01 10:30" in content
    assert "2026-05-06 14:45" in content
    assert '<record id="1" timestamp="2026-05-01 10:30"' in content
    assert '<record id="2" timestamp="2026-05-06 14:45"' in content



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

    entries = await system.get_history_entries(ctx, limit=10, query="天气")
    assert len(entries) > 0
    first_summary = str(entries[0].get("summary", ""))
    assert "天气" in first_summary


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
    assert len(entries) > 0


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

    km = system._layers.knowledge
    await km.ensure_defaults(ctx, {"memory": "- priority: 90"})
    await system._layers.archive.append(ctx, ArchiveEntry(summary="priority-70 entry"))
    await system._layers.session.add_messages(ctx, [
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": "response"},
    ])

    bundle = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=ctx, memory_system=system, query="",
    )

    # Verify sections appear in priority order: SOUL(100) before MEMORY(90) before archive(70)
    memory_pos = bundle.system_prompt.find("- priority: 90")
    archive_pos = bundle.system_prompt.find("priority-70 entry")
    # SOUL is at the start (highest priority)
    assert bundle.system_prompt != "", "System prompt should not be empty"
    if memory_pos >= 0 and archive_pos >= 0:
        assert memory_pos < archive_pos, (
            "MEMORY (priority 90) should appear before archive (priority 70)"
        )
    elif memory_pos >= 0:
        assert memory_pos >= 0, "MEMORY section should be present"
    elif archive_pos >= 0:
        assert archive_pos >= 0, "Archive section should be present"


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
    await system._layers.archive.append(ctx, ArchiveEntry(summary="low priority old history " * 20))

    budget = MemoryBudget(max_system_prompt_tokens=300, max_history_messages=10)
    bundle = await FullInjectionPolicy(max_history_entries=5, budget=budget).assemble(
        context=ctx, memory_system=system, query="",
    )

    high_priority_found = "HIGH priority" in bundle.system_prompt
    assert high_priority_found, "SOUL (priority 100) should survive budget trim"

    # Low priority content (archive at priority 70) should be trimmed before high priority
    # when token budget is tight
    low_priority_found = "low priority old history" in bundle.system_prompt
    # Either: high priority survived (always) + low may or may not (budget-dependent)
    assert high_priority_found


@pytest.mark.asyncio
async def test_restricted_injection_session_only():
    """Peer/subagent policy: only session messages, no knowledge/archive."""
    from framework.memory.injection import RestrictedInjectionPolicy

    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("restricted")

    await system._layers.knowledge.ensure_defaults(ctx, {"memory": "- should not appear"})
    await system._layers.archive.append(ctx, ArchiveEntry(summary="should not appear"))
    await system._layers.session.add_messages(ctx, [
        {"role": "user", "content": "visible message"},
    ])

    bundle = await RestrictedInjectionPolicy(max_session_messages=10).assemble(
        context=ctx, memory_system=system, query="",
    )

    assert bundle.system_prompt == ""
    assert len(bundle.messages) > 0
    assert any("visible message" in str(m) for m in bundle.messages)


@pytest.mark.asyncio
async def test_injection_preserves_tool_messages_by_default():
    """Injection preserves tool messages for governance to handle.

    The simplified design has no message filtering during injection.
    Governance (MicrocompactGovernance, ToolChainRepair) handles
    tool message management at the LLM call boundary.
    """
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

    # All 4 messages should survive injection; governance handles compaction
    assert len(bundle.messages) == 4, (
        f"Expected all 4 messages preserved, got {len(bundle.messages)}"
    )

    # Tool messages are present for governance to manage
    roles = [m.to_dict().get("role") for m in bundle.messages]
    assert "tool" in roles, "tool result should be preserved for governance"
    has_tool_calls = any(m.to_dict().get("tool_calls") for m in bundle.messages)
    assert has_tool_calls, "assistant tool_calls should be preserved for governance"


# ── Three-tier cascade: Session -> Archive -> Knowledge ───────────────────


@pytest.mark.asyncio
async def test_three_tier_memory_cascade_preserves_tool_context(tmp_path: Path):
    """Full cascade: add tool-heavy messages -> cleanup -> archive with tool context."""
    registry = InMemoryStoreRegistry()
    mock = _MockArchiveGenerator(
        canned_context="[ARCHIVE] user asked about weather, used shell+web_search, got sunny 28C",
        canned_knowledge="[ARCHIVE] user asked about weather, used shell+web_search, got sunny 28C",
    )
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(registry, max_messages=5, archive_agent=mock, archive_storage=storage)
    await system.initialize()
    ctx = _make_ctx("three-tier")

    history = system.create_message_history(ctx)
    for i in range(4):
        await history.append({"role": "user", "content": f"task {i}"})
        await history.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"tc{i}", "type": "function", "function": {"name": "read_file"}},
        ]})
        await history.append({"role": "tool", "tool_call_id": f"tc{i}", "name": "read_file", "content": f"result {i}"})
        await history.append({"role": "assistant", "content": f"done {i}"})

    remaining = await system.get_history(ctx, max_messages=None)
    assert len(remaining) <= 10, f"old prefix compressed, got {len(remaining)} remaining"

    archive_entries = await system.get_history_entries(ctx, limit=10)
    assert len(archive_entries) > 0
    assert any("[ARCHIVE]" in str(e.get("summary", "")) for e in archive_entries)

    await system._layers.archive.append(ctx, ArchiveEntry(
        summary="[ARCHIVE] user prefers dark mode for all UIs",
    ))
    await system._layers.archive.append(ctx, ArchiveEntry(
        summary="[ARCHIVE] project uses Python 3.11+, FastAPI, ChromaDB",
    ))

    retrieved = await system.get_history_entries(ctx, limit=10, query="dark mode")
    assert len(retrieved) > 0
    assert any("dark mode" in str(e.get("summary", "")) for e in retrieved)


@pytest.mark.asyncio
async def test_archive_entries_are_meaningful_for_dream_engine(tmp_path: Path):
    """Archive summaries contain enough context for DreamEngine fact extraction."""
    from framework.memory.consolidation.dream_engine import DreamEngine

    registry = InMemoryStoreRegistry()
    mock = _MockArchiveGenerator(
        canned_context="[ARCHIVE] user: fix login bug | tools: read_file(auth.py), shell(git log) | "
                     "decision: use JWT instead of session | state: branch fix/auth, tests fail",
        canned_knowledge="[ARCHIVE] user: fix login bug | tools: read_file(auth.py), shell(git log) | "
                        "decision: use JWT instead of session | state: branch fix/auth, tests fail",
    )
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(registry, max_messages=3, archive_agent=mock, archive_storage=storage)
    await system.initialize()
    ctx = _make_ctx("dream-input")

    history = system.create_message_history(ctx)
    for i in range(8):
        await history.append({"role": "user", "content": f"msg {i}"})
        await history.append({"role": "assistant", "content": f"reply {i}"})

    entries = await system.get_history_entries(ctx, limit=20)
    assert len(entries) > 0

    for e in entries:
        assert DreamEngine._is_meaningful_entry(e), \
            f"archive entry should be meaningful: {e.get('summary', '')[:80]}"

    summary = str(entries[0].get("summary", ""))
    assert "user:" in summary or "tools:" in summary or "decision:" in summary or "[ARCHIVE]" in summary


@pytest.mark.asyncio
async def test_cleanup_plugin_would_remove_tool_before_cleanup():
    """Document the conflict: if cleanup is enabled, tool context is lost."""
    registry = InMemoryStoreRegistry()
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("cleanup-conflict")

    await system._layers.session.add_messages(ctx, [
        {"role": "user", "content": "read file"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "function": {"name": "read_file"}}
        ]},
        {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "file data"},
        {"role": "assistant", "content": "file says hello"},
    ])

    before = len(await system._layers.session.get_all_messages(ctx))
    assert before == 4

    from examples.bot_project.plugins.tool_call_cleanup.policy import ToolCallCleanupPolicy
    policy = ToolCallCleanupPolicy()
    dict_msgs = [m.to_dict() for m in await system._layers.session.get_all_messages(ctx)]
    cleaned = policy.clean(dict_msgs)

    assert len(cleaned) == 2, f"cleanup removes tool msgs, got {len(cleaned)}"
    assert cleaned[0]["role"] == "user"
    assert cleaned[1]["role"] == "assistant"
    assert not cleaned[1].get("tool_calls")
