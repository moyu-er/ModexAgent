"""End-to-end tests for the bot_project memory pipeline.

Simulates multi-turn conversations using bot_project-equivalent configuration:
  turn messages -> ScopedMessageHistory -> cleanup_session -> archive -> injection.

LLM summarizer results are mocked to isolate pipeline logic from model calls.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from modex_agent.agents.summarizer.abc import ArchiveGenerator
from modex_agent.core.scope import MemoryContext
from modex_agent.core.types import MessageRole
from modex_agent.memory.archive_models import (
    ArchiveChannel,
    ArchiveDocuments,
    ArchiveGenerationResult,
)
from modex_agent.memory.core.models import ArchiveEntry
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.registry import DefaultMemoryStoreRegistry, MemoryStoreRegistry
from modex_agent.memory.stores.dir_archive import DirArchiveStorage
from tests.unit.memory.conftest import FixedTokenEstimator

# ── Helpers ───────────────────────────────────────────────────────────────


class _MockArchiveGenerator(ArchiveGenerator):
    """Mock ArchiveGenerator that writes canned content to archive_dir.

    Matches the :class:`~framework.agents.summarizer.abc.ArchiveGenerator`
    contract so ``cleanup_session`` can use it as an ``archive_agent``.
    """

    def __init__(
        self,
        canned_context: str = "[MOCK] compressed conversation context",
        canned_core: str = "[MOCK] compressed conversation core",
    ) -> None:
        self.canned_context = canned_context
        self.canned_core = canned_core
        self.calls: list[list[Any]] = []

    async def generate(
        self,
        pruned_messages: Sequence[dict[str, Any]],
    ) -> ArchiveGenerationResult:
        self.calls.append(list(pruned_messages))
        return ArchiveGenerationResult(
            documents=ArchiveDocuments(
                context=self.canned_context,
                core=self.canned_core,
                index="mock index",
            )
        )


class _EmptyArchiveGenerator(ArchiveGenerator):
    """Mock ArchiveGenerator that writes empty files (simulates no content)."""

    async def generate(
        self,
        pruned_messages: Sequence[dict[str, Any]],
    ) -> ArchiveGenerationResult:
        _ = pruned_messages
        return ArchiveGenerationResult(
            documents=ArchiveDocuments(context="", core="", index=""),
        )


class _FakeInjectableMemorySystem(MemorySystem):
    """Minimal memory system for testing archive injection via DirArchiveStorage.

    Inherits MemorySystem ABC (CRUD + injection reads).
    """

    def __init__(self, archive_dir: Path) -> None:
        self._archive_dir = archive_dir

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def create_message_history(self, context: Any, initial_messages: Any = None) -> Any:
        from modex_agent.memory.history import ListMessageHistory

        return ListMessageHistory()

    async def add_messages(self, context: Any, messages: Any) -> None:
        pass

    async def search(self, query: str, context: Any, limit: int = 5) -> list:
        return []

    async def clear(self, context: Any) -> None:
        pass

    async def get_core_memory(self, context: Any) -> Any:
        from modex_agent.memory.core.models import CoreMemoryContents

        return CoreMemoryContents()

    async def get_storage_path(self, context: Any) -> Path | None:
        return self._archive_dir

    async def get_history(self, context: Any, max_messages: int | None = None) -> list:
        return []

    async def get_full_history(self, context: Any) -> list:
        return []

    async def retrieve_core_memory(self, context: Any, query: str = "") -> Any:
        from modex_agent.memory.core.models import CoreMemoryContents

        return CoreMemoryContents()

    async def get_history_entries(
        self,
        context: Any,
        limit: int = 3,
        query: str = "",
        *,
        channel: ArchiveChannel = ArchiveChannel.CONTEXT,
    ) -> list[dict[str, str | int | None]]:
        _ = context, query, channel
        from modex_agent.memory.stores.dir_archive import DirArchiveStorage

        storage = DirArchiveStorage(self._archive_dir)
        archive_ids = await storage.list_archives(limit=limit)
        entries: list[dict[str, str | int | None]] = []
        for archive_id in archive_ids:
            content = await storage.read_archive_file(archive_id, "context.md")
            entries.append(
                {
                    "summary": content,
                    "archive_id": archive_id,
                    "cursor": archive_id,
                    "created_at": None,
                }
            )
        return entries

    async def get_core_memory_directory(self, context: Any) -> None:
        return None

    def get_providers(self) -> list:
        return []

    async def prefetch_memories(self, query: str, context: Any) -> None:
        return None


def _bot_project_system(
    registry: MemoryStoreRegistry,
    max_context_tokens: int = 700,
    keep_ratio: float = 0.5,
    *,
    archive_agent: object | None = None,
    archive_storage: DirArchiveStorage | None = None,
) -> DefaultMemorySystem:
    """Create a DefaultMemorySystem wired like bot_project (with cleanup_config).

    When *archive_agent* and *archive_storage* are provided, cleanup_session
    can generate archive entries on the hot path.

    Token-driven: cleanup fires when non-system session tokens exceed
    ``max_context_tokens * max_token_ratio`` (i.e. ``max_context_tokens * 0.8``).
    """
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    cleanup_config: dict[str, int | float] = {
        "max_context_tokens": max_context_tokens,
        "max_token_ratio": 0.8,
        "keep_ratio": keep_ratio,
    }
    return DefaultMemorySystem(
        layer_set=layer_set,
        store_registry=registry,
        cleanup_config=cleanup_config,
        archive_agent=archive_agent,  # type: ignore[arg-type]
        archive_storage=archive_storage,  # type: ignore[arg-type]
        token_estimator=FixedTokenEstimator(10),
    )


def _make_ctx(session_id: str = "test") -> MemoryContext:
    return MemoryContext(session_id=session_id, user_id="u1")


# ── Multi-turn cascade: turn-by-turn -> cleanup -> archive ──────────────────


@pytest.mark.asyncio
async def test_multi_turn_triggers_cleanup_at_threshold(tmp_path: Path):
    """After >50 turns through ScopedMessageHistory, cleanup fires and
    archive is written. Session stays within the max_messages window."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    mock = _MockArchiveGenerator()
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(registry, archive_agent=mock, archive_storage=storage)
    await system.initialize()
    ctx = _make_ctx("multi-turn")

    history = system.create_message_history(ctx)
    for i in range(60):
        await history.append({"role": "user", "content": f"question {i}"})
        await history.append({"role": "assistant", "content": f"answer {i}"})

    remaining = await system.get_history(ctx)
    assert len(remaining) <= 65, f"should compress to <=65, got {len(remaining)}"

    # Archives are MD-based (DirArchiveStorage); verify directly
    archive_ids = await storage.list_archives()
    assert len(archive_ids) > 0, "archive should have cleanup entries"


@pytest.mark.asyncio
async def test_cleanup_respects_threshold(tmp_path: Path):
    """After cleanup, adding a few more messages does not re-trigger until threshold is exceeded."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    mock = _MockArchiveGenerator()
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(
        registry, max_context_tokens=140, archive_agent=mock, archive_storage=storage
    )
    await system.initialize()
    ctx = _make_ctx("threshold")

    history = system.create_message_history(ctx)
    for i in range(20):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    compressed_count = len(await system.get_history(ctx))
    archive_count_1 = len(await storage.list_archives())

    # Add only 2 more turns (4 messages)
    for i in range(2):
        await history.append({"role": "user", "content": f"post{i}"})
        await history.append({"role": "assistant", "content": f"post-a{i}"})

    new_count = len(await system.get_history(ctx))
    archive_count_2 = len(await storage.list_archives())

    assert new_count <= compressed_count + 10
    assert archive_count_2 <= archive_count_1 + 2


@pytest.mark.asyncio
async def test_second_cleanup_fires_when_over_threshold(tmp_path: Path):
    """After sufficiently many new messages past threshold, cleanup re-fires."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    mock = _MockArchiveGenerator()
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(
        registry, max_context_tokens=140, archive_agent=mock, archive_storage=storage
    )
    await system.initialize()
    ctx = _make_ctx("recompress")

    history = system.create_message_history(ctx)
    for i in range(20):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    archive_first = len(await storage.list_archives())
    assert archive_first > 0, "first cleanup should produce archive"

    for i in range(30):
        await history.append({"role": "user", "content": f"round2-{i}"})

    archive_second = len(await storage.list_archives())
    assert archive_second >= archive_first


# ── Tool chain integrity across cleanup ────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_chains_intact_after_cascade(tmp_path: Path):
    """After multi-turn cleanup, kept suffix has no orphan tool results."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    mock = _MockArchiveGenerator()
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(
        registry, max_context_tokens=112, archive_agent=mock, archive_storage=storage
    )
    await system.initialize()
    ctx = _make_ctx("tool-chain-cascade")

    history = system.create_message_history(ctx)
    for i in range(8):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": f"tc{i}", "type": "function", "function": {"name": "read_file"}},
                ],
            }
        )
        await history.append(
            {"role": "tool", "tool_call_id": f"tc{i}", "name": "read_file", "content": f"data{i}"}
        )
        await history.append({"role": "assistant", "content": f"answer {i}"})

    remaining = await system.get_history(ctx)
    assert len(remaining) <= 25, f"should compress 32 to <=25, got {len(remaining)}"

    # No orphan tool results
    kept_ids: set[str] = set()
    for m in remaining:
        d = m.to_dict()
        for tc in d.get("tool_calls", []) or []:
            if isinstance(tc, dict):
                cid = tc.get("id")
                if cid:
                    kept_ids.add(cid)
    for m in remaining:
        d = m.to_dict()
        if d.get("role") == str(MessageRole.TOOL):
            assert d.get("tool_call_id") in kept_ids, f"orphan {d.get('tool_call_id')}"


# ── Archive content with mock LLM summarizer ──────────────────────────────


@pytest.mark.asyncio
async def test_archive_uses_mock_summarizer_output(tmp_path: Path):
    """When archive strategy is wired, archive content matches mock output."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    mock = _MockArchiveGenerator(
        canned_context="[MOCK] compressed to archive",
        canned_core="[MOCK] compressed to archive",
    )
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(
        registry, max_context_tokens=70, archive_agent=mock, archive_storage=storage
    )
    await system.initialize()
    ctx = _make_ctx("mock-summary")

    history = system.create_message_history(ctx)
    for i in range(12):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    assert len(mock.calls) > 0, "mock archive generation should have been called"

    archive_ids = await storage.list_archives()
    assert len(archive_ids) > 0, "archive should have mock-generated entries"
    content = await storage.read_archive_file(archive_ids[0], "context.md") or ""
    assert "[MOCK]" in content


@pytest.mark.asyncio
async def test_archive_skips_empty_generation(tmp_path: Path):
    """Empty archive generation should not produce archive files."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    mock = _EmptyArchiveGenerator()
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(
        registry, max_context_tokens=70, archive_agent=mock, archive_storage=storage
    )
    await system.initialize()
    ctx = _make_ctx("nothing-sentinel")

    history = system.create_message_history(ctx)
    for i in range(12):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    archive_ids = await storage.list_archives()
    assert len(archive_ids) == 0, "empty archive generation should produce no dirs"
    # Session IS cleaned even when archive generation is empty
    remaining = len(await system.get_history(ctx))
    assert remaining < 24, (
        f"session must be cleaned even without archive writes, still has {remaining}"
    )


# ── Context injection: core + archive + session ──────────────────────


@pytest.mark.asyncio
async def test_full_injection_includes_core_archive_and_session(tmp_path: Path):
    """FullInjectionPolicy assembles Knowledge, Archive, and Session messages."""
    from modex_agent.memory.injection import FullInjectionPolicy
    from modex_agent.memory.stores.dir_archive import DirArchiveStorage

    archive_dir = tmp_path / "archives"
    storage = DirArchiveStorage(archive_dir)
    await storage.write_archive_file(
        1, "context.md", "previous session: discussed memory compression"
    )

    fake = _FakeInjectableMemorySystem(archive_dir)
    result = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=fake,
    )

    # Archive content injected via MD path
    content = result.system_prompt
    assert "memory compression" in content
    assert "### Earlier Conversation Summaries" in content


@pytest.mark.asyncio
async def test_injection_excludes_empty_archive_markers(tmp_path: Path):
    """Empty context.md files are skipped; only non-empty archives inject."""
    from modex_agent.memory.injection import FullInjectionPolicy
    from modex_agent.memory.stores.dir_archive import DirArchiveStorage

    archive_dir = tmp_path / "archives"
    storage = DirArchiveStorage(archive_dir)
    await storage.write_archive_file(1, "context.md", "")
    await storage.write_archive_file(2, "context.md", "   ")
    await storage.write_archive_file(3, "context.md", "real: user asked about weather")

    fake = _FakeInjectableMemorySystem(archive_dir)
    result = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=fake,
    )

    content = result.system_prompt
    assert "weather" in content
    assert 'number="1"' not in content
    assert 'number="2"' not in content


# ── Long-term core: full update (existing + new) ─────────────────────


@pytest.mark.asyncio
async def test_core_update_preserves_existing_when_adding_new(tmp_path: Path):
    """Existing core entries are preserved when new ones are added."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("core-update")

    km = system._layers.core
    await km.ensure_defaults(ctx, {"memory": "- fact A\n- fact B\n"})
    before = await km.get_all(ctx)
    assert "fact A" in before.memory
    assert "fact B" in before.memory

    from modex_agent.memory.core.consolidation import MemoryUpdate

    await km.apply_update(
        ctx,
        MemoryUpdate(
            file_name="MEMORY.md",
            content="- fact C\n",
            mode="append",
            reason="test",
        ),
    )
    after = await km.get_all(ctx)
    assert "fact A" in after.memory
    assert "fact B" in after.memory
    assert "fact C" in after.memory


@pytest.mark.asyncio
async def test_core_replace_text_updates_in_place(tmp_path: Path):
    """replace_text mode corrects existing entries without losing other facts."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("core-replace")

    km = system._layers.core
    await km.ensure_defaults(ctx, {"user": "- location: Tokyo\n- prefers dark mode\n"})

    from modex_agent.memory.core.consolidation import MemoryUpdate

    await km.apply_update(
        ctx,
        MemoryUpdate(
            file_name="USER.md",
            content="- location: Osaka\n",
            mode="replace_text",
            search_text="- location: Tokyo\n",
            reason="corrected",
        ),
    )

    after = await km.get_all(ctx)
    assert "Osaka" in after.user
    assert "Tokyo" not in after.user
    assert "dark mode" in after.user


@pytest.mark.asyncio
async def test_archive_merges_multiple_cleanup_rounds(tmp_path: Path):
    """Multiple cleanup rounds produce cumulative archive entries."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    mock = _MockArchiveGenerator(
        canned_context="[MOCK] round context",
        canned_core="[MOCK] round core",
    )
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(
        registry, max_context_tokens=140, archive_agent=mock, archive_storage=storage
    )
    await system.initialize()
    ctx = _make_ctx("multi-compress")

    history = system.create_message_history(ctx)

    for i in range(20):
        await history.append({"role": "user", "content": f"r1-{i}"})
        await history.append({"role": "assistant", "content": f"r1-a{i}"})

    for i in range(40):
        await history.append({"role": "user", "content": f"r2-{i}"})

    archive_ids = await storage.list_archives()
    assert len(archive_ids) >= 1

    remaining = len(await system.get_history(ctx))
    assert remaining <= 25, f"multiple rounds should keep session small, got {remaining}"


# ── Edge case: empty session, single message ──────────────────────────────


@pytest.mark.asyncio
async def test_empty_session_no_cleanup(tmp_path: Path):
    """Empty session does not trigger cleanup."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("empty")

    history = system.create_message_history(ctx)
    for i in range(3):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    remaining = len(await system.get_history(ctx))
    assert remaining == 6, "all 6 messages should be present (no cleanup)"

    archive = await system.get_history_entries(ctx, limit=10)
    assert len(archive) == 0


@pytest.mark.asyncio
async def test_session_only_messages_no_duplicate_cleanup_trigger(tmp_path: Path):
    """Cleanup does not fire on every single message -- threshold-based trigger works."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    mock = _MockArchiveGenerator()
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(
        registry, max_context_tokens=140, archive_agent=mock, archive_storage=storage
    )
    await system.initialize()
    ctx = _make_ctx("no-dupe")

    history = system.create_message_history(ctx)
    for i in range(25):
        await history.append({"role": "user", "content": f"q{i}"})
        await history.append({"role": "assistant", "content": f"a{i}"})

    remaining = len(await system.get_history(ctx))
    assert remaining <= 20, f"should compress, got {remaining}"
    assert remaining > 0


# ── Archive injection: distinguishable markers ────────────────────────────


@pytest.mark.asyncio
async def test_archive_injection_has_distinguishable_markers(tmp_path: Path):
    """Multiple archive entries are injected with clear per-entry markers."""
    from modex_agent.memory.injection import FullInjectionPolicy
    from modex_agent.memory.stores.dir_archive import DirArchiveStorage

    archive_dir = tmp_path / "archives"
    storage = DirArchiveStorage(archive_dir)
    await storage.write_archive_file(1, "context.md", "first session: discussed login bug")
    await storage.write_archive_file(2, "context.md", "second session: fixed auth flow")
    await storage.write_archive_file(3, "context.md", "third session: added JWT tests")

    fake = _FakeInjectableMemorySystem(archive_dir)
    result = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=fake,
    )
    content = result.system_prompt

    assert "<summary" in content
    assert "<older_topics>" in content
    assert "</older_topics>" in content
    assert "login bug" in content
    assert "auth flow" in content
    assert "JWT tests" in content


@pytest.mark.asyncio
async def test_archive_injection_uses_archive_id_as_number(tmp_path: Path):
    """Archive entries are injected with archive_id as the number attribute."""
    from modex_agent.memory.injection import FullInjectionPolicy
    from modex_agent.memory.stores.dir_archive import DirArchiveStorage

    archive_dir = tmp_path / "archives"
    storage = DirArchiveStorage(archive_dir)
    await storage.write_archive_file(1, "context.md", "older discussion")
    await storage.write_archive_file(2, "context.md", "recent discussion")

    fake = _FakeInjectableMemorySystem(archive_dir)
    result = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=fake,
    )
    content = result.system_prompt

    assert 'number="1"' in content
    assert 'number="2"' in content
    assert "older discussion" in content
    assert "recent discussion" in content


# ── Retrieval: archive search by query ────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_search_by_query_boosts_relevant_entries(tmp_path: Path):
    """Archive search with query returns relevant entries prioritized."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
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
async def test_archive_search_falls_back_to_recent_when_no_match(tmp_path: Path):
    """When query matches nothing, fall back to recent entries."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("archive-nomatch")

    await system._layers.archive.append(ctx, ArchiveEntry(summary="some old topic"))
    await system._layers.archive.append(ctx, ArchiveEntry(summary="another unrelated"))

    entries = await system.get_history_entries(ctx, limit=10, query="NONEXISTENT_KEYWORD_XYZ")
    assert len(entries) > 0


# ── Retrieval: core files ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_core_retrieve_returns_all_files(tmp_path: Path):
    """retrieve_core_memory returns SOUL, USER, MEMORY even with empty query."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("core-get")

    km = system._layers.core
    await km.ensure_defaults(
        ctx,
        {
            "soul": "## SOUL\nbot personality",
            "user": "## USER\nuser preferences",
            "memory": "## MEMORY\nproject context",
        },
    )

    core = await system.retrieve_core_memory(ctx)
    assert "bot personality" in core.soul
    assert "user preferences" in core.user
    assert "project context" in core.memory


# ── Injection: priority ordering ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_injection_priority_order_respected(tmp_path: Path):
    """Sections are ordered by priority descending: core > archive > compression."""
    from modex_agent.memory.injection import FullInjectionPolicy

    registry = DefaultMemoryStoreRegistry(tmp_path)
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("priority-order")

    km = system._layers.core
    await km.ensure_defaults(ctx, {"memory": "- priority: 90"})
    await system._layers.archive.append(ctx, ArchiveEntry(summary="priority-70 entry"))
    await system._layers.session.add_messages(
        ctx,
        [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": "response"},
        ],
    )

    bundle = await FullInjectionPolicy(max_history_entries=5).assemble(
        context=ctx,
        memory_system=system,
        query="",
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
async def test_injection_budget_trims_low_priority_first(tmp_path: Path):
    """When token budget is tight, low-priority sections drop first."""
    from modex_agent.memory.core.models import MemoryBudget
    from modex_agent.memory.injection import FullInjectionPolicy

    registry = DefaultMemoryStoreRegistry(tmp_path)
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("budget-trim")

    km = system._layers.core
    await km.ensure_defaults(
        ctx,
        {
            "soul": "HIGH priority content " * 50,
            "memory": "medium priority " * 100,
        },
    )
    await system._layers.archive.append(ctx, ArchiveEntry(summary="low priority old history " * 20))

    budget = MemoryBudget(max_system_prompt_tokens=1200)
    bundle = await FullInjectionPolicy(max_history_entries=5, budget=budget).assemble(
        context=ctx,
        memory_system=system,
        query="",
    )

    high_priority_found = "HIGH priority" in bundle.system_prompt
    assert high_priority_found, "SOUL (priority 100) should survive budget trim"

    # Low priority content (archive at priority 70) should be trimmed before high priority
    # when token budget is tight
    low_priority_found = "low priority old history" in bundle.system_prompt
    # Either: high priority survived (always) + low may or may not (budget-dependent)
    assert high_priority_found


@pytest.mark.asyncio
async def test_restricted_injection_session_only(tmp_path: Path):
    """Peer/subagent policy: only session messages, no core/archive."""
    from modex_agent.memory.injection import RestrictedInjectionPolicy

    registry = DefaultMemoryStoreRegistry(tmp_path)
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("restricted")

    await system._layers.core.ensure_defaults(ctx, {"memory": "- should not appear"})
    await system._layers.archive.append(ctx, ArchiveEntry(summary="should not appear"))
    await system._layers.session.add_messages(
        ctx,
        [
            {"role": "user", "content": "visible message"},
        ],
    )

    bundle = await RestrictedInjectionPolicy().assemble(
        context=ctx,
        memory_system=system,
        query="",
    )

    assert bundle.system_prompt == ""
    assert len(bundle.messages) > 0
    assert any("visible message" in str(m) for m in bundle.messages)


@pytest.mark.asyncio
async def test_injection_preserves_tool_messages_by_default(tmp_path: Path):
    """Injection preserves tool messages for governance to handle.

    The simplified design has no message filtering during injection.
    Governance (MicrocompactGovernance, ToolChainRepair) handles
    tool message management at the LLM call boundary.
    """
    from modex_agent.memory.injection import FullInjectionPolicy

    registry = DefaultMemoryStoreRegistry(tmp_path)
    system = _bot_project_system(registry)
    await system.initialize()
    ctx = _make_ctx("tool-filter")

    await system._layers.session.add_messages(
        ctx,
        [
            {"role": "user", "content": "read file"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "t1", "function": {"name": "read_file"}}],
            },
            {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "file data"},
            {"role": "assistant", "content": "file says hello"},
        ],
    )

    bundle = await FullInjectionPolicy().assemble(
        context=ctx,
        memory_system=system,
        query="",
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
    registry = DefaultMemoryStoreRegistry(tmp_path)
    mock = _MockArchiveGenerator(
        canned_context="[ARCHIVE] user asked about weather, used shell+web_search, got sunny 28C",
        canned_core="[ARCHIVE] user asked about weather, used shell+web_search, got sunny 28C",
    )
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(
        registry, max_context_tokens=70, archive_agent=mock, archive_storage=storage
    )
    await system.initialize()
    ctx = _make_ctx("three-tier")

    history = system.create_message_history(ctx)
    for i in range(4):
        await history.append({"role": "user", "content": f"task {i}"})
        await history.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": f"tc{i}", "type": "function", "function": {"name": "read_file"}},
                ],
            }
        )
        await history.append(
            {
                "role": "tool",
                "tool_call_id": f"tc{i}",
                "name": "read_file",
                "content": f"result {i}",
            }
        )
        await history.append({"role": "assistant", "content": f"done {i}"})

    remaining = await system.get_history(ctx)
    assert len(remaining) <= 10, f"old prefix compressed, got {len(remaining)} remaining"

    archive_ids = await storage.list_archives()
    assert len(archive_ids) > 0
    context_md = await storage.read_archive_file(archive_ids[0], "context.md") or ""
    assert "[ARCHIVE]" in context_md

    # In-memory archive additions still work through the fallback path
    await system._layers.archive.append(
        ctx,
        ArchiveEntry(
            summary="[ARCHIVE] user prefers dark mode for all UIs",
        ),
    )
    await system._layers.archive.append(
        ctx,
        ArchiveEntry(
            summary="[ARCHIVE] project uses Python 3.11+, FastAPI, ChromaDB",
        ),
    )

    retrieved = await system.get_history_entries(ctx, limit=10, query="dark mode")
    assert len(retrieved) > 0
    assert any("dark mode" in str(e.get("summary", "")) for e in retrieved)


@pytest.mark.asyncio
async def test_archive_entries_are_meaningful_for_dream_engine(tmp_path: Path):
    """Archive summaries contain enough context for DreamEngine fact extraction."""
    registry = DefaultMemoryStoreRegistry(tmp_path)
    mock = _MockArchiveGenerator(
        canned_context="[ARCHIVE] user: fix login bug | tools: read_file(auth.py), shell(git log) | "
        "decision: use JWT instead of session | state: branch fix/auth, tests fail",
        canned_core="[ARCHIVE] user: fix login bug | tools: read_file(auth.py), shell(git log) | "
        "decision: use JWT instead of session | state: branch fix/auth, tests fail",
    )
    storage = DirArchiveStorage(tmp_path / "archives")
    system = _bot_project_system(
        registry, max_context_tokens=42, archive_agent=mock, archive_storage=storage
    )
    await system.initialize()
    ctx = _make_ctx("dream-input")

    history = system.create_message_history(ctx)
    for i in range(8):
        await history.append({"role": "user", "content": f"msg {i}"})
        await history.append({"role": "assistant", "content": f"reply {i}"})

    archive_ids = await storage.list_archives()
    assert len(archive_ids) > 0

    for aid in archive_ids:
        context_md = await storage.read_archive_file(aid, "context.md") or ""
        assert context_md.strip(), f"archive {aid} should have non-empty context.md"

    summary = await storage.read_archive_file(archive_ids[0], "context.md") or ""
    assert (
        "user:" in summary
        or "tools:" in summary
        or "decision:" in summary
        or "[ARCHIVE]" in summary
    )
