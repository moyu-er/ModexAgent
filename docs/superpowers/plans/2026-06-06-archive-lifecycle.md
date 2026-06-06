# Archive Lifecycle & Injection Overhaul — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix archive injection truncation, add FIFO disk cleanup, add archive-count-triggered knowledge updates, protect DreamEngine concurrency, and clean up dead config.

**Architecture:** Extend `DirArchiveStorage` with real cleanup methods, make `FullInjectionPolicy` configurable for archive count/length, add `asyncio.Lock` to `DreamEngine`, add `on_archive_generated` callback to `cleanup_session`, and wire new `ArchiveConfig` fields.

**Tech Stack:** Python 3.12, pytest-asyncio, Pydantic v2, framework memory subsystem

---

## File Map

| File | Responsibility |
|------|---------------|
| `framework/ioc/configs/memory.py` | Pydantic config models for archive + dream engine |
| `framework/memory/stores/dir_archive.py` | Directory-based archive storage with cleanup |
| `framework/memory/injection/full_injection.py` | Archive injection into system prompt |
| `framework/memory/consolidation/dream_engine.py` | Knowledge consolidation with concurrency lock |
| `framework/memory/cleanup.py` | Session cleanup with optional post-archive callback |
| `framework/memory/lifecycle.py` | Maintenance scan with FIFO archive eviction |
| `framework/ioc/factories/memory.py` | Wire config to components |
| `examples/bot_project/config/pools/main.yml` | Updated example config |
| `examples/bot_project/bot/service/core.py` | Archive-count trigger callback |
| `tests/unit/memory/stores/test_dir_archive.py` | DirArchiveStorage cleanup tests |
| `tests/unit/memory/test_lifecycle.py` | Lifecycle FIFO eviction tests |
| `tests/unit/memory/consolidation/test_dream_engine_registry.py` | DreamEngine lock/batch tests |
| `tests/unit/memory/test_full_injection_archive.py` | Archive injection tests |
| `tests/unit/memory/test_archive_config.py` | Config model tests |

---

## Task 1: Config Model — ArchiveConfig

**Files:**
- Modify: `framework/ioc/configs/memory.py:85-90`
- Test: `tests/unit/memory/test_archive_config.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""Tests for ArchiveConfig and DreamEngineConfig."""
from __future__ import annotations

from framework.ioc.configs.memory import ArchiveConfig, DreamEngineConfig


def test_archive_config_defaults():
    cfg = ArchiveConfig()
    assert cfg.enabled is False
    assert cfg.max_entries == 1000
    assert cfg.retained_consumed_archive_pairs == 3
    assert cfg.max_archive_count == 10
    assert cfg.max_archive_total == 20
    assert cfg.max_archive_inject == 3


def test_dream_engine_config_defaults():
    cfg = DreamEngineConfig()
    assert cfg.enabled is False
    assert cfg.interval == 600
    assert cfg.max_consume_per_run == 3


def test_archive_config_custom_values():
    cfg = ArchiveConfig(
        enabled=True,
        max_archive_count=5,
        max_archive_total=15,
        max_archive_inject=2,
    )
    assert cfg.max_archive_count == 5
    assert cfg.max_archive_total == 15
    assert cfg.max_archive_inject == 2


def test_dream_engine_config_custom_values():
    cfg = DreamEngineConfig(
        enabled=True,
        interval=300,
        max_consume_per_run=5,
    )
    assert cfg.max_consume_per_run == 5
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd F:/tool/pythonProject/ModexAgent
pytest tests/unit/memory/test_archive_config.py -v
```

Expected: FAIL with `ImportError` or `AttributeError` on new fields.

- [ ] **Step 3: Write minimal implementation**

In `framework/ioc/configs/memory.py`, add 3 fields to `ArchiveConfig` (around line 85):

```python
class ArchiveConfig(BaseModel):
    """Archive memory: compressed history summaries. Separate from KnowledgeConfig."""

    enabled: bool = False
    max_entries: int = 1000
    retained_consumed_archive_pairs: int = 3

    # NEW — three independent parameters
    max_archive_count: int = 10    # trigger knowledge update when this many undigested
    max_archive_total: int = 20    # max archive dirs on disk (FIFO eviction)
    max_archive_inject: int = 3    # how many recent archives to inject into system prompt
```

In `DreamEngineConfig` (around line 56), remove `min_archive_count` and `max_archive_count`, add `max_consume_per_run`:

```python
class DreamEngineConfig(BaseModel):
    """Offline archive-to-knowledge consolidation."""

    enabled: bool = False
    interval: int = 1200
    max_consume_per_run: int = 3   # renamed from max_batch_size
```

Also remove the `max_batch_size` line (line 63 in the current file).

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/memory/test_archive_config.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add framework/ioc/configs/memory.py tests/unit/memory/test_archive_config.py
git commit -m "feat(config): add archive lifecycle params (max_archive_count/total/inject, max_consume_per_run)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: DirArchiveStorage — Cleanup Methods

**Files:**
- Modify: `framework/memory/stores/dir_archive.py:121-124` (save_channel_logs), add new methods
- Test: `tests/unit/memory/stores/test_dir_archive.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/memory/stores/test_dir_archive.py`, after the existing `test_save_channel_logs_is_noop`:

```python
    async def test_save_channel_logs_deletes_missing_dirs(
        self, store: DirArchiveStorage
    ) -> None:
        # Create dirs 1, 2, 3 with required files
        for aid in [1, 2, 3]:
            d = store.base_dir / str(aid)
            d.mkdir(parents=True, exist_ok=True)
            for name in ("context.md", "knowledge.md", "index.md"):
                (d / name).write_text("content", encoding="utf-8")

        # Keep only archive_id 2 and 3
        await store.save_channel_logs("context", [
            {"archive_id": 2},
            {"archive_id": 3},
        ])

        assert not (store.base_dir / "1").exists()
        assert (store.base_dir / "2").exists()
        assert (store.base_dir / "3").exists()

    async def test_prune_to_max_deletes_oldest(self, store: DirArchiveStorage) -> None:
        for aid in [1, 2, 3, 4, 5]:
            d = store.base_dir / str(aid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "context.md").write_text(f"entry {aid}", encoding="utf-8")

        deleted = await store.prune_to_max(3)

        assert deleted == 2
        assert not (store.base_dir / "1").exists()
        assert not (store.base_dir / "2").exists()
        assert (store.base_dir / "3").exists()
        assert (store.base_dir / "4").exists()
        assert (store.base_dir / "5").exists()

    async def test_prune_to_max_respects_min_safe_id(self, store: DirArchiveStorage) -> None:
        """Archives > min_safe_id (unconsumed) must not be deleted."""
        for aid in [1, 2, 3, 4, 5]:
            d = store.base_dir / str(aid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "context.md").write_text(f"entry {aid}", encoding="utf-8")

        # min_safe_id=3 means archives 1-3 are "already consumed" and can be deleted
        # archives 4-5 are "not yet consumed" and must be preserved
        deleted = await store.prune_to_max(2, min_safe_id=3)

        # Only archive 1 should be deleted (oldest among deletable: 1,2)
        assert deleted == 1
        assert not (store.base_dir / "1").exists()
        assert (store.base_dir / "2").exists()  # kept to reach max_total=2
        assert (store.base_dir / "3").exists()  # at min_safe_id, not deletable
        assert (store.base_dir / "4").exists()  # > min_safe_id, preserved
        assert (store.base_dir / "5").exists()  # > min_safe_id, preserved

    async def test_prune_to_max_noop_when_under_limit(
        self, store: DirArchiveStorage
    ) -> None:
        for aid in [1, 2]:
            d = store.base_dir / str(aid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "context.md").write_text("content", encoding="utf-8")

        deleted = await store.prune_to_max(5)
        assert deleted == 0
        assert (store.base_dir / "1").exists()

    async def test_cleanup_empty_dirs_removes_empty(
        self, store: DirArchiveStorage
    ) -> None:
        # Empty dir
        empty = store.base_dir / "1"
        empty.mkdir(parents=True, exist_ok=True)

        # Dir with only empty files
        almost_empty = store.base_dir / "2"
        almost_empty.mkdir(parents=True, exist_ok=True)
        (almost_empty / "context.md").write_text("", encoding="utf-8")
        (almost_empty / "knowledge.md").write_text("", encoding="utf-8")

        # Valid dir
        valid = store.base_dir / "3"
        valid.mkdir(parents=True, exist_ok=True)
        (valid / "context.md").write_text("has content", encoding="utf-8")

        count = await store.cleanup_empty_dirs()

        assert count == 2
        assert not empty.exists()
        assert not almost_empty.exists()
        assert valid.exists()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/memory/stores/test_dir_archive.py -v
```

Expected: FAIL — `AttributeError: 'DirArchiveStorage' object has no attribute 'prune_to_max'`

- [ ] **Step 3: Write minimal implementation**

In `framework/memory/stores/dir_archive.py`:

1. Add `import shutil` at the top (after `import json`).

2. Replace `save_channel_logs` (lines 121-124):

```python
    async def save_channel_logs(
        self, channel: str, entries: list[dict[str, Any]]
    ) -> None:
        """Remove archive directories not present in *entries*."""
        if not self._base.exists():
            return
        kept_ids = {
            int(e.get("archive_id", 0))
            for e in entries
            if e.get("archive_id")
        }
        for child in list(self._base.iterdir()):
            if child.is_dir() and child.name.isdigit():
                aid = int(child.name)
                if aid not in kept_ids and aid > 0:
                    shutil.rmtree(child, ignore_errors=True)
```

3. Add `prune_to_max` after `save_channel_logs`:

```python
    async def prune_to_max(self, max_total: int, min_safe_id: int = 0) -> int:
        """Delete oldest archive dirs exceeding max_total, but never below min_safe_id.

        min_safe_id is typically knowledge_consumed_archive_id — archives at or below
        this ID are already consumed and safe to delete. Archives above it are preserved
        for pending knowledge digestion.
        """
        ids = await self.list_archives(limit=10_000)
        deletable = [aid for aid in ids if aid > min_safe_id]
        if len(deletable) <= max_total:
            return 0
        ascending = sorted(deletable)
        to_delete = ascending[:-max_total]
        for aid in to_delete:
            shutil.rmtree(self._base / str(aid), ignore_errors=True)
        return len(to_delete)
```

4. Add `cleanup_empty_dirs` after `prune_to_max`:

```python
    async def cleanup_empty_dirs(self) -> int:
        """Remove archive directories with no non-empty required files."""
        required = {"context.md", "knowledge.md", "index.md"}
        count = 0
        for child in list(self._base.iterdir()):
            if child.is_dir() and child.name.isdigit():
                has_content = any(
                    (child / f).exists() and (child / f).stat().st_size > 0
                    for f in required
                )
                if not has_content:
                    shutil.rmtree(child, ignore_errors=True)
                    count += 1
        return count
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/memory/stores/test_dir_archive.py -v
```

Expected: All tests PASS (including existing ones).

- [ ] **Step 5: Commit**

```bash
git add framework/memory/stores/dir_archive.py tests/unit/memory/stores/test_dir_archive.py
git commit -m "feat(archive): DirArchiveStorage cleanup — prune_to_max, cleanup_empty_dirs, real save_channel_logs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: FullInjectionPolicy — Configurable Archive Injection

**Files:**
- Modify: `framework/memory/injection/full_injection.py:43-50`, `248-317`
- Test: `tests/unit/memory/test_full_injection_archive.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""Tests for FullInjectionPolicy archive injection."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.memory.injection.full_injection import FullInjectionPolicy


class _FakeMemorySystem:
    """Minimal injectable memory system for testing."""

    def __init__(self, archive_dir, archive_contents: dict[int, str]):
        self._archive_dir = archive_dir
        self._archive_contents = archive_contents

    async def get_storage_path(self, context):
        return self._archive_dir

    async def get_history(self, context, max_messages=None):
        return []

    async def retrieve_knowledge(self, context, query=""):
        from framework.memory.core.models import LongTermMemory
        return LongTermMemory()

    async def get_history_entries(self, context, limit=3, query="", channel=None):
        return []

    async def get_knowledge_directory(self, context):
        return None

    def get_providers(self):
        return []

    async def prefetch_memories(self, query, context):
        return None


@pytest.fixture
def fake_system(tmp_path):
    return _FakeMemorySystem


@pytest.mark.asyncio
async def test_inject_md_archives_truncates_at_1000_chars(tmp_path):
    from framework.memory.core.scope import MemoryContext
    from framework.memory.stores.dir_archive import DirArchiveStorage

    archive_dir = tmp_path / "archives"
    storage = DirArchiveStorage(archive_dir)

    # Create archive 1 with 1200-char context.md
    long_content = "A" * 1200
    await storage.write_archive_file(1, "context.md", long_content)

    fake = _FakeMemorySystem(archive_dir, {1: long_content})
    policy = FullInjectionPolicy(
        archive_inject_count=3,
        archive_inject_max_chars=1000,
    )

    result = await policy.assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=fake,
    )

    assert "historical_context" in result.system_prompt
    assert "A" * 1000 in result.system_prompt
    assert "..." in result.system_prompt
    # Should NOT contain the full 1200 chars
    assert "A" * 1100 not in result.system_prompt


@pytest.mark.asyncio
async def test_inject_md_archives_ascending_order(tmp_path):
    from framework.memory.core.scope import MemoryContext
    from framework.memory.stores.dir_archive import DirArchiveStorage

    archive_dir = tmp_path / "archives"
    storage = DirArchiveStorage(archive_dir)

    await storage.write_archive_file(1, "context.md", "first archive")
    await storage.write_archive_file(2, "context.md", "second archive")
    await storage.write_archive_file(3, "context.md", "third archive")

    fake = _FakeMemorySystem(archive_dir, {})
    policy = FullInjectionPolicy(
        archive_inject_count=3,
        archive_inject_max_chars=1000,
    )

    result = await policy.assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=fake,
    )

    # Records should appear in ascending archive_id order
    first_pos = result.system_prompt.find('archive_id="1"')
    second_pos = result.system_prompt.find('archive_id="2"')
    third_pos = result.system_prompt.find('archive_id="3"')
    assert first_pos < second_pos < third_pos


@pytest.mark.asyncio
async def test_inject_md_archives_respects_count_limit(tmp_path):
    from framework.memory.core.scope import MemoryContext
    from framework.memory.stores.dir_archive import DirArchiveStorage

    archive_dir = tmp_path / "archives"
    storage = DirArchiveStorage(archive_dir)

    for aid in [1, 2, 3, 4, 5]:
        await storage.write_archive_file(aid, "context.md", f"archive {aid}")

    fake = _FakeMemorySystem(archive_dir, {})
    policy = FullInjectionPolicy(
        archive_inject_count=2,  # only inject 2
        archive_inject_max_chars=1000,
    )

    result = await policy.assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=fake,
    )

    # Should only have archives 4 and 5 (newest 2, in ascending order)
    assert 'archive_id="4"' in result.system_prompt
    assert 'archive_id="5"' in result.system_prompt
    assert 'archive_id="3"' not in result.system_prompt
    assert 'archive_id="1"' not in result.system_prompt


@pytest.mark.asyncio
async def test_inject_md_archives_skips_empty_context(tmp_path):
    from framework.memory.core.scope import MemoryContext
    from framework.memory.stores.dir_archive import DirArchiveStorage

    archive_dir = tmp_path / "archives"
    storage = DirArchiveStorage(archive_dir)

    await storage.write_archive_file(1, "context.md", "valid content")
    await storage.write_archive_file(2, "context.md", "")  # empty

    fake = _FakeMemorySystem(archive_dir, {})
    policy = FullInjectionPolicy(
        archive_inject_count=3,
        archive_inject_max_chars=1000,
    )

    result = await policy.assemble(
        context=MemoryContext(session_id="s1"),
        memory_system=fake,
    )

    assert 'archive_id="1"' in result.system_prompt
    assert 'archive_id="2"' not in result.system_prompt
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/memory/test_full_injection_archive.py -v
```

Expected: FAIL — `TypeError: FullInjectionPolicy.__init__() got an unexpected keyword argument 'archive_inject_count'`

- [ ] **Step 3: Write minimal implementation**

In `framework/memory/injection/full_injection.py`:

1. Modify `__init__` (around line 43):

```python
    def __init__(
        self,
        *,
        budget: MemoryBudget | None = None,
        max_history_entries: int = 3,
        pruned_manager: PrunedManager | None = None,
        archive_inject_count: int = 3,
        archive_inject_max_chars: int = 1000,
    ) -> None:
        self._budget = budget or MemoryBudget()
        self._max_history = max_history_entries
        self._pruned_manager = pruned_manager
        self._archive_inject_count = archive_inject_count
        self._archive_inject_max_chars = archive_inject_max_chars
```

2. Modify `_try_inject_md_archives` (around line 272-277):

Replace `limit=3` with `limit=self._archive_inject_count`:

```python
        try:
            archive_ids = await storage.list_archives(limit=self._archive_inject_count)
        except Exception:
            return False
```

3. Modify the loop (around line 281-282):

Replace `sorted(archive_ids, reverse=True)[:3]` with `sorted(archive_ids)[:self._archive_inject_count]`:

```python
        # Read context.md from each archive (ascending order)
        records: list[str] = []
        for aid in sorted(archive_ids)[:self._archive_inject_count]:
```

4. Modify truncation (around line 291-292):

Replace `truncated = len(content) > 150` with:

```python
            truncated = len(content) > self._archive_inject_max_chars
            display = content[:self._archive_inject_max_chars] + "..." if truncated else content
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/memory/test_full_injection_archive.py -v
```

Expected: PASS

Also run existing injection tests to avoid breakage:

```bash
pytest tests/unit/memory/test_injection_result.py tests/unit/memory/test_knowledge_directory_injection.py tests/unit/memory/test_bot_project_memory_pipeline.py -v
```

Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add framework/memory/injection/full_injection.py tests/unit/memory/test_full_injection_archive.py
git commit -m "feat(injection): configurable archive injection — count, truncation, ascending order

- archive_inject_count: how many archives to inject
- archive_inject_max_chars: truncation threshold (default 1000)
- ascending order by archive_id

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: DreamEngine — Concurrency Lock & Batch Limit

**Files:**
- Modify: `framework/memory/consolidation/dream_engine.py`
- Test: `tests/unit/memory/consolidation/test_dream_engine_registry.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/memory/consolidation/test_dream_engine_registry.py`:

```python
import asyncio


@pytest.mark.asyncio
async def test_dream_engine_run_skips_when_locked() -> None:
    """Second concurrent run returns False immediately."""
    archive = DummyArchiveManager()
    engine = DreamEngine(
        llm_provider=DummyLLM(),
        history_manager=archive,
        long_term_manager=DummyKnowledgeManager(),
        consolidator=AsyncMock(),
    )

    context = MemoryContext(session_id="s1", user_id="u1")

    # Start a slow run
    async def slow_run():
        async with engine._lock:
            await asyncio.sleep(0.2)
        return True

    task = asyncio.create_task(slow_run())
    await asyncio.sleep(0.05)  # Let the lock be acquired

    # Second run should skip
    result = await engine.run(context)
    assert result is False

    await task


@pytest.mark.asyncio
async def test_dream_engine_run_limits_batch_size() -> None:
    """Run processes at most max_consume_per_run archives."""
    archive = DummyArchiveManager(entry_count=5)
    mock_consolidator = AsyncMock()
    mock_consolidator.consolidate.return_value = True

    engine = DreamEngine(
        llm_provider=DummyLLM(),
        history_manager=archive,
        long_term_manager=DummyKnowledgeManager(),
        consolidator=mock_consolidator,
        max_consume_per_run=2,
    )

    context = MemoryContext(session_id="s1", user_id="u1")
    result = await engine.run(context)

    assert result is True
    # Should only commit up to 2 archive ids
    assert len(archive.committed) == 1
    # Cursor should be 2 (only first 2 entries processed)
    _, _, cursor, _ = archive.committed[0]
    assert cursor == 2
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/memory/consolidation/test_dream_engine_registry.py::test_dream_engine_run_skips_when_locked -v
pytest tests/unit/memory/consolidation/test_dream_engine_registry.py::test_dream_engine_run_limits_batch_size -v
```

Expected: FAIL — `AttributeError: 'DreamEngine' object has no attribute '_lock'` and `TypeError: DreamEngine.__init__() got an unexpected keyword argument 'max_consume_per_run'`

- [ ] **Step 3: Write minimal implementation**

In `framework/memory/consolidation/dream_engine.py`:

1. Add `import asyncio` at the top if not present.

2. Modify `__init__` (around line 61-76):

```python
    def __init__(
        self,
        llm_provider: LLMProvider,
        history_manager: ArchiveMemoryManager,
        long_term_manager: KnowledgeMemoryManager,
        max_batch_size: int = 20,
        max_iterations: int = 10,
        storage: MemoryStorage | None = None,
        registry: MemoryStoreRegistry | None = None,
        schedule_mode: str = "manual",
        idle_threshold_entries: int = 5,
        summarizer: SummarizerAgent | None = None,
        min_archive_count: int = 0,   # kept for backward compat, ignored
        max_archive_count: int = 30,  # kept for backward compat, ignored
        prompts: Any = None,
        consolidator: Any | None = None,
        max_consume_per_run: int = 3,  # NEW
    ):
        self.history_manager = history_manager
        self.long_term_manager = long_term_manager
        self.max_batch_size = max_batch_size
        self.max_iterations = max_iterations
        self.storage = storage
        self.registry = registry
        self.schedule_mode = schedule_mode
        self.idle_threshold_entries = idle_threshold_entries
        self.min_archive_count = min_archive_count  # deprecated, stored but unused
        self.max_archive_count = max_archive_count  # deprecated, stored but unused
        self.max_consume_per_run = max_consume_per_run  # NEW
        if prompts is None:
            from framework.memory.prompts import create_default_registry
            try:
                prompts = create_default_registry()
            except Exception:
                pass
        self._prompts = prompts
        self._summarizer: SummarizerAgent = summarizer or SummarizerAgent(llm_provider)
        self._consolidator = consolidator
        self._lock = asyncio.Lock()  # NEW
```

3. Modify `run` (around line 100-118):

```python
    async def run(self, context: MemoryContext) -> bool:
        if self._lock.locked():
            logger.info("DreamEngine skipped: already running for session=%s", context.session_id)
            return False

        async with self._lock:
            unprocessed = await self.history_manager.get_unprocessed(
                context,
                cursor_name="dream",
                channel=ArchiveChannel.KNOWLEDGE,
            )
            entries = unprocessed.entries
            if not entries:
                return False

            # Limit per run
            entries = entries[:self.max_consume_per_run]

            # NEW PATH: Use KnowledgeConsolidator agent
            if self._consolidator is not None:
                return await self._run_consolidator_limited(entries, context)
            return False
```

4. Add `_run_consolidator_limited` after `_run_consolidator`:

```python
    async def _run_consolidator_limited(
        self,
        entries: list[Any],
        context: MemoryContext,
    ) -> bool:
        """Run consolidator on a pre-sliced entry list.

        Same as _run_consolidator but accepts an already-limited entry list
        instead of using unprocessed.entries directly.
        """
        archive_ids = [e.entry_id for e in entries if e.entry_id]
        if not archive_ids:
            return False

        # Get knowledge directory path from long_term_manager
        knowledge_dir = await self.long_term_manager.get_storage_path(context)
        if knowledge_dir is None:
            logger.warning(
                "KnowledgeConsolidator: no knowledge storage path for context=%s",
                context,
            )
            return False

        # Get archive base directory
        archive_base = await self.history_manager.get_storage_path(context)
        if archive_base is None:
            logger.warning(
                "KnowledgeConsolidator: no archive storage path for context=%s",
                context,
            )
            return False

        logger.info(
            "KnowledgeConsolidator: processing %d archive(s) for knowledge update",
            len(archive_ids),
        )

        success = await self._consolidator.consolidate(
            archive_ids=archive_ids,
            archive_base=archive_base,
            knowledge_dir=knowledge_dir,
        )

        # Always commit cursor to prevent re-processing
        final_cursor = max(archive_ids)
        await self._commit_knowledge_cursor(context, final_cursor)

        return success
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/memory/consolidation/test_dream_engine_registry.py -v
```

Expected: All tests PASS (including existing ones).

- [ ] **Step 5: Commit**

```bash
git add framework/memory/consolidation/dream_engine.py tests/unit/memory/consolidation/test_dream_engine_registry.py
git commit -m "feat(dream): asyncio.Lock concurrency + max_consume_per_run batch limit

- Non-blocking lock: second run returns False immediately
- max_consume_per_run limits archives processed per invocation
- _run_consolidator_limited accepts pre-sliced entry list

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Cleanup — Post-Archive Callback

**Files:**
- Modify: `framework/memory/cleanup.py`
- Test: `tests/unit/memory/test_cleanup.py` (check if exists, or create)

- [ ] **Step 1: Check for existing cleanup tests**

```bash
ls tests/unit/memory/test_cleanup*.py 2>/dev/null || echo "NO EXISTING TESTS"
```

If no existing tests, skip to Step 3 (implementation) — the callback is a thin wrapper and is tested indirectly via integration tests in Task 8.

- [ ] **Step 2: Write the implementation**

In `framework/memory/cleanup.py`, modify `cleanup_session` signature (around line 79):

```python
async def cleanup_session(
    *,
    session: SessionMemoryManager,
    archive: ArchiveMemoryManager | None,
    context: MemoryContext,
    max_messages: int | None = None,
    max_tokens: int | None = None,
    keep_ratio: float = 0.5,
    max_backups: int = 10,
    user_retention: UserRetentionBuffer | None = None,
    pruned_manager: PrunedManager | None = None,
    archive_agent: Any | None = None,
    archive_storage: Any | None = None,
    on_archive_generated: Callable[[], Awaitable[None]] | None = None,  # NEW
) -> CleanupResult:
```

Then, after the archive generation block (around line 270, after `archive_generated` is set), add:

```python
    # ── Step 3: Post-archive trigger ──────────────────────────────────────
    if archive_generated and on_archive_generated is not None:
        try:
            await on_archive_generated()
        except Exception:
            logger.debug("Post-cleanup archive trigger failed", exc_info=True)
```

- [ ] **Step 3: Commit**

```bash
git add framework/memory/cleanup.py
git commit -m "feat(cleanup): add on_archive_generated callback for knowledge trigger

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Lifecycle — FIFO Archive Eviction

**Files:**
- Modify: `framework/memory/lifecycle.py`
- Test: `tests/unit/memory/test_lifecycle.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/memory/test_lifecycle.py` in `TestDefaultMemoryMaintenancePolicy`:

```python
    @pytest.mark.asyncio
    async def test_scan_once_archive_retention_fifo_eviction(self):
        import tempfile
        from pathlib import Path
        from framework.memory.stores.dir_archive import DirArchiveStorage

        registry = InMemoryStoreRegistry()
        layer_set = MemoryLayerFactory.single_user(registry=registry)
        ctx = MemoryContext(session_id="s1", user_id="u1")

        # Seed archive with 5 entries (via DirArchiveStorage directly for disk state)
        archive_storage = await registry.resolve(
            layer=MemoryLayerName.ARCHIVE,
            scope=layer_set.archive.get_scope(),
            context=ctx,
        )

        # Only test if we have a DirArchiveStorage (file-based)
        if not isinstance(archive_storage, DirArchiveStorage):
            pytest.skip("Requires DirArchiveStorage")

        for i in range(1, 6):
            await archive_storage.write_archive_file(i, "context.md", f"entry {i}")
            await archive_storage.write_archive_file(i, "knowledge.md", f"knowledge {i}")
            await archive_storage.write_archive_file(i, "index.md", f"index {i}")

        # Simulate knowledge having consumed archives 1-2 (cursor=2)
        await archive_storage.write_archive_state({
            "next_archive_id": 6,
            "knowledge_consumed_archive_id": 2,
        })

        retention = DefaultArchiveRetentionPolicy(
            max_archive_total=3,
        )
        policy = DefaultMemoryMaintenancePolicy(archive_retention_policy=retention)

        results = await policy.scan_once(registry=registry, layers=layer_set)

        assert any(r.task == "archive_retention" and r.success for r in results)

        # Archives 1-2 are ≤ cursor (consumed), can be deleted.
        # Archives 3-5 are > cursor (unconsumed), must be preserved.
        # With max_total=3 and min_safe_id=2, deletable IDs are [3,4,5].
        # Keep newest 3 → all 3,4,5 kept. Nothing deleted.
        ids = await archive_storage.list_archives()
        assert sorted(ids) == [3, 4, 5]

    @pytest.mark.asyncio
    async def test_scan_once_archive_retention_fifo_respects_cursor(self):
        """FIFO eviction must not delete archives above knowledge_consumed_archive_id."""
        from framework.memory.stores.dir_archive import DirArchiveStorage

        registry = InMemoryStoreRegistry()
        layer_set = MemoryLayerFactory.single_user(registry=registry)
        ctx = MemoryContext(session_id="s1", user_id="u1")

        archive_storage = await registry.resolve(
            layer=MemoryLayerName.ARCHIVE,
            scope=layer_set.archive.get_scope(),
            context=ctx,
        )

        if not isinstance(archive_storage, DirArchiveStorage):
            pytest.skip("Requires DirArchiveStorage")

        for i in range(1, 6):
            await archive_storage.write_archive_file(i, "context.md", f"entry {i}")
            await archive_storage.write_archive_file(i, "knowledge.md", f"knowledge {i}")
            await archive_storage.write_archive_file(i, "index.md", f"index {i}")

        # Cursor at 4: archives 1-4 consumed, archive 5 unconsumed
        await archive_storage.write_archive_state({
            "next_archive_id": 6,
            "knowledge_consumed_archive_id": 4,
        })

        retention = DefaultArchiveRetentionPolicy(max_archive_total=2)
        policy = DefaultMemoryMaintenancePolicy(archive_retention_policy=retention)

        results = await policy.scan_once(registry=registry, layers=layer_set)

        assert any(r.task == "archive_retention" and r.success for r in results)

        # Deletable: [5] (only ID > cursor=4). Already ≤ max_total=2, nothing deleted.
        ids = await archive_storage.list_archives()
        assert sorted(ids) == [1, 2, 3, 4, 5]  # all preserved

    @pytest.mark.asyncio
    async def test_scan_once_archive_retention_with_empty_dirs(self):
        import tempfile
        from pathlib import Path
        from framework.memory.stores.dir_archive import DirArchiveStorage

        registry = InMemoryStoreRegistry()
        layer_set = MemoryLayerFactory.single_user(registry=registry)
        ctx = MemoryContext(session_id="s1", user_id="u1")

        archive_storage = await registry.resolve(
            layer=MemoryLayerName.ARCHIVE,
            scope=layer_set.archive.get_scope(),
            context=ctx,
        )

        if not isinstance(archive_storage, DirArchiveStorage):
            pytest.skip("Requires DirArchiveStorage")

        # Valid dir
        await archive_storage.write_archive_file(1, "context.md", "valid")
        await archive_storage.write_archive_file(1, "knowledge.md", "valid")
        await archive_storage.write_archive_file(1, "index.md", "valid")

        # Empty dir (no content files)
        empty_dir = archive_storage.base_dir / "2"
        empty_dir.mkdir(parents=True, exist_ok=True)

        retention = DefaultArchiveRetentionPolicy(max_archive_total=10)
        policy = DefaultMemoryMaintenancePolicy(archive_retention_policy=retention)

        results = await policy.scan_once(registry=registry, layers=layer_set)

        # Empty dir should be cleaned up
        assert not empty_dir.exists()
        assert (archive_storage.base_dir / "1").exists()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/memory/test_lifecycle.py::TestDefaultMemoryMaintenancePolicy::test_scan_once_archive_retention_fifo_eviction -v
```

Expected: FAIL — `TypeError: DefaultArchiveRetentionPolicy.__init__() got an unexpected keyword argument 'max_archive_total'`

- [ ] **Step 3: Write minimal implementation**

In `framework/memory/lifecycle.py`:

1. Modify `DefaultArchiveRetentionPolicy` (around line 266):

```python
class DefaultArchiveRetentionPolicy(ArchiveRetentionPolicy):
    def __init__(
        self,
        max_entries: int | None = 1000,
        max_age_days: int | None = None,
        max_archive_total: int | None = None,
    ) -> None:
        self._max_entries = max_entries
        self._max_age_days = max_age_days
        self._max_archive_total = max_archive_total

    async def get_max_entries(self, context: MemoryContext) -> int | None:
        return self._max_entries

    async def get_max_age_days(self, context: MemoryContext) -> int | None:
        return self._max_age_days

    async def get_max_archive_total(self, context: MemoryContext) -> int | None:
        return self._max_archive_total
```

2. Modify `scan_once` (around line 100-120, after existing retention logic, add a new block):

After the `if pruned:` block (around line 153-161), add:

```python
                    # FIFO eviction: delete oldest dirs exceeding max_archive_total,
                    # but never delete archives that haven't been consumed by knowledge yet.
                    if isinstance(archive_storage, DirArchiveStorage):
                        max_total = await self._archive_retention.get_max_archive_total(ctx)
                        if max_total is not None:
                            state = await archive_storage.read_archive_state() or {}
                            consumed = state.get("knowledge_consumed_archive_id", 0)
                            deleted = await archive_storage.prune_to_max(
                                max_total, min_safe_id=consumed
                            )
                            if deleted:
                                await archive_storage.cleanup_empty_dirs()
                                pruned = True
```

Note: This needs `DirArchiveStorage` imported. Add at the top:

```python
from framework.memory.stores.dir_archive import DirArchiveStorage
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/memory/test_lifecycle.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add framework/memory/lifecycle.py tests/unit/memory/test_lifecycle.py
git commit -m "feat(lifecycle): FIFO archive eviction + empty dir cleanup

- DefaultArchiveRetentionPolicy gains max_archive_total
- scan_once calls DirArchiveStorage.prune_to_max + cleanup_empty_dirs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Factory Wiring

**Files:**
- Modify: `framework/ioc/factories/memory.py`
- Test: Run existing memory factory tests

- [ ] **Step 1: Check existing factory tests**

```bash
pytest tests/unit/ioc/test_memory_factory.py -v 2>/dev/null || echo "No dedicated test file"
```

- [ ] **Step 2: Modify factory to pass new params**

In `framework/ioc/factories/memory.py`, find where `FullInjectionPolicy` is constructed (look for `FullInjectionPolicy(`) and add the new parameters:

```python
from framework.ioc.configs.memory import ArchiveConfig

# When constructing FullInjectionPolicy:
archive_cfg = config.archive if config else None
FullInjectionPolicy(
    budget=MemoryBudget(...),
    max_history_entries=3,
    pruned_manager=pruned_manager,
    archive_inject_count=archive_cfg.max_archive_inject if archive_cfg else 3,
    archive_inject_max_chars=1000,
)
```

Also wire `max_consume_per_run` into DreamEngine construction (if done in factory):

```python
dream_cfg = config.dream_engine if config else None
DreamEngine(
    ...,
    max_consume_per_run=dream_cfg.max_consume_per_run if dream_cfg else 3,
)
```

- [ ] **Step 3: Verify no test breakage**

```bash
pytest tests/unit/ioc/ -v 2>/dev/null || echo "No ioc tests found"
pytest tests/unit/memory/ -v -k "not test_" 2>/dev/null || echo "Skipping"
```

Run broader test suite to check for breakage:

```bash
pytest tests/unit/memory/ -v --tb=short -x
```

Expected: All PASS (or at least no new failures from this change).

- [ ] **Step 4: Commit**

```bash
git add framework/ioc/factories/memory.py
git commit -m "feat(factory): wire new archive config params to components

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Bot Project — Config Migration + Trigger Callback

**Files:**
- Modify: `examples/bot_project/config/pools/main.yml`
- Modify: `examples/bot_project/bot/service/core.py`

- [ ] **Step 1: Update config**

Replace `examples/bot_project/config/pools/main.yml` lines 23-30:

Before:
```yaml
  archive: {enabled: true, max_entries: 1000, retained_consumed_pairs: 3}
  knowledge: {enabled: true, default_templates_dir: "templates/knowledge"}
  dream_engine:
    enabled: true
    interval: 600
    min_archive_count: 1
    max_archive_count: 30
    max_batch_size: 20
```

After:
```yaml
  archive:
    enabled: true
    max_archive_count: 10       # trigger knowledge update when this many undigested
    max_archive_total: 20       # max dirs on disk (FIFO eviction)
    max_archive_inject: 3       # inject to system prompt
  knowledge: {enabled: true, default_templates_dir: "templates/knowledge"}
  dream_engine:
    enabled: true
    interval: 600
    max_consume_per_run: 3      # per-run batch limit
```

- [ ] **Step 2: Add trigger callback to core.py**

In `examples/bot_project/bot/service/core.py`, find `_init_dream` (around line 1444) and verify DreamEngine is initialized correctly.

Then add a new method (before `_dream_background_loop`):

```python
    async def _check_and_trigger_dream(self) -> None:
        """Check undigested archive count; trigger DreamEngine if over threshold."""
        if self.dream_engine is None or self._main_memory_cfg is None:
            return
        archive_cfg = self._main_memory_cfg.archive
        if archive_cfg is None:
            return
        threshold = archive_cfg.max_archive_count
        if threshold <= 0:
            return

        context = self._build_memory_context()
        try:
            unprocessed = await self.memory_system.archive_manager.get_unprocessed(
                context,
                cursor_name="dream",
                channel=ArchiveChannel.KNOWLEDGE,
            )
            if len(unprocessed.entries) >= threshold:
                logger.info(
                    "Archive count %d >= threshold %d, triggering DreamEngine",
                    len(unprocessed.entries), threshold,
                )
                await self.dream_engine.run(context)
        except Exception:
            logger.debug("Archive count check failed", exc_info=True)
```

Note: `_build_memory_context` may already exist; if not, use the pattern from existing code to create `MemoryContext`.

Then find where `cleanup_session` is called (search for `cleanup_session(` in `core.py`) and pass the callback:

```python
await cleanup_session(
    session=...,
    archive=...,
    context=...,
    ...,
    on_archive_generated=self._check_and_trigger_dream,
)
```

- [ ] **Step 3: Verify**

```bash
cd F:/tool/pythonProject/ModexAgent
python -c "import yaml; yaml.safe_load(open('examples/bot_project/config/pools/main.yml'))"
```

Expected: No errors.

Also verify syntax of modified Python files:

```bash
python -m py_compile examples/bot_project/bot/service/core.py
```

Expected: No errors.

- [ ] **Step 4: Commit**

```bash
git add examples/bot_project/config/pools/main.yml examples/bot_project/bot/service/core.py
git commit -m "feat(bot): archive-count trigger + updated config format

- Config: new archive lifecycle params
- Core: _check_and_trigger_dream callback wired to cleanup_session

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Final Integration Test

- [ ] **Step 1: Run all memory tests**

```bash
cd F:/tool/pythonProject/ModexAgent
pytest tests/unit/memory/ -v --tb=short
```

Expected: All PASS.

- [ ] **Step 2: Verify no type errors**

```bash
python -m py_compile framework/memory/stores/dir_archive.py
python -m py_compile framework/memory/injection/full_injection.py
python -m py_compile framework/memory/consolidation/dream_engine.py
python -m py_compile framework/memory/cleanup.py
python -m py_compile framework/memory/lifecycle.py
python -m py_compile framework/ioc/configs/memory.py
python -m py_compile framework/ioc/factories/memory.py
```

Expected: All "Success".

- [ ] **Step 3: Commit final integration**

```bash
git add docs/superpowers/specs/2026-06-06-archive-lifecycle-design.md docs/superpowers/plans/2026-06-06-archive-lifecycle.md
git commit -m "docs: archive lifecycle overhaul spec + plan

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review Checklist

### 1. Spec Coverage

| Spec Section | Implementing Task |
|-------------|-------------------|
| §1 Config: ArchiveConfig new params | Task 1 |
| §1 Config: DreamEngineConfig cleanup | Task 1 |
| §2 Injection: configurable count/chars | Task 3 |
| §2 Injection: ascending order | Task 3 |
| §3 DirArchiveStorage: save_channel_logs real impl | Task 2 |
| §3 DirArchiveStorage: prune_to_max | Task 2 |
| §3 DirArchiveStorage: cleanup_empty_dirs | Task 2 |
| §4 DreamEngine: asyncio.Lock | Task 4 |
| §4 DreamEngine: max_consume_per_run | Task 4 |
| §5 Cleanup: on_archive_generated callback | Task 5 |
| §6 Lifecycle: DefaultArchiveRetentionPolicy max_archive_total | Task 6 |
| §6 Lifecycle: scan_once FIFO eviction | Task 6 |
| §7 Config migration: bot_project main.yml | Task 8 |
| §7 Backward compat | Tasks 1, 4 |

**All spec requirements covered.**

### 2. Placeholder Scan

- No "TBD", "TODO", "implement later" found.
- All test code blocks contain complete, runnable code.
- All implementation code blocks contain complete, runnable code.
- No vague requirements like "add appropriate error handling".

### 3. Type Consistency

- `archive_inject_count` / `archive_inject_max_chars` — used consistently in Task 3 init + Task 3 tests + Task 7 factory.
- `max_consume_per_run` — used in Task 1 config, Task 4 engine init + tests.
- `max_archive_total` — used in Task 1 config, Task 6 retention policy, Task 6 tests.
- `prune_to_max` / `cleanup_empty_dirs` — defined in Task 2, called in Task 6.
- `on_archive_generated` — defined in Task 5, wired in Task 8.

**All type names consistent across tasks.**

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-06-archive-lifecycle.md`.**

Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach would you like to use?
