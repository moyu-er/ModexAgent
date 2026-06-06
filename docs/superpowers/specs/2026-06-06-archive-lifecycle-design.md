# Archive Lifecycle & Injection Overhaul

Date: 2026-06-06

## Problem

1. **Archive injection is truncated to 150 chars** — too aggressive, agent gets almost no useful context from archives.
2. **Archive directories grow indefinitely** — `DirArchiveStorage.save_channel_logs` is a no-op; `prune_to_max` and cleanup logic never delete subdirectories.
3. **DreamEngine has dead config** — `min_archive_count` / `max_archive_count` are stored but never read.
4. **No archive-count trigger for knowledge** — knowledge updates rely solely on timer; archives can pile up between intervals.
5. **No concurrency protection** — two DreamEngine runs can execute concurrently (timer + manual trigger).

## Design Principles

- **Three independent parameters**: knowledge trigger threshold, max storage, injection count — each controls a different concern.
- **Knowledge and cleanup are decoupled**: archive cleanup is pure FIFO, unaffected by knowledge digestion status.
- **Non-blocking lock for DreamEngine**: try-lock with skip + log; never block the caller.

---

## 1. Configuration Model

### ArchiveConfig (`framework/ioc/configs/memory.py`)

```python
class ArchiveConfig(BaseModel):
    enabled: bool = False
    max_entries: int = 1000                      # kept for backward compat
    retained_consumed_archive_pairs: int = 3     # kept for backward compat

    # NEW — three independent parameters
    max_archive_count: int = 10    # trigger knowledge update when this many undigested
    max_archive_total: int = 20    # max archive dirs on disk (FIFO eviction)
    max_archive_inject: int = 3    # how many recent archives to inject into system prompt
```

### DreamEngineConfig (`framework/ioc/configs/memory.py`)

```python
class DreamEngineConfig(BaseModel):
    enabled: bool = False
    interval: int = 600
    max_consume_per_run: int = 3    # renamed from max_batch_size, clearer semantics
    # REMOVED: min_archive_count (was dead code)
    # REMOVED: max_archive_count (moved to ArchiveConfig)
```

`max_batch_size` is kept as an alias for backward compat during migration; both resolve to the same value.

### New config wiring (`framework/ioc/factories/memory.py`)

`create_memory` reads `ArchiveConfig.max_archive_count` and passes it to the cleanup callback (see §4), not to DreamEngine.

---

## 2. Archive Injection

### FullInjectionPolicy (`framework/memory/injection/full_injection.py`)

`__init__` gains two parameters:

```python
def __init__(
    self,
    ...,
    archive_inject_count: int = 3,       # maps to ArchiveConfig.max_archive_inject
    archive_inject_max_chars: int = 1000, # truncation threshold for context.md
) -> None:
```

### `_try_inject_md_archives` changes

| Before | After |
|--------|-------|
| Hard-coded `limit=3` | `limit=archive_inject_count` (from config) |
| Truncate to 150 chars | Truncate to `archive_inject_max_chars` (default 1000) |
| `reverse=True` (newest first) | Ascending order (smallest archive_id first) |
| Reads `context.md` | Same — reads `context.md` (unchanged) |

Injection XML format is unchanged:

```xml
<historical_context>
  <record archive_id="1" file="/abs/path/1/context.md">...content...</record>
  <record archive_id="2" file="/abs/path/2/context.md">...content...</record>
  <record archive_id="3" file="/abs/path/3/context.md">...content...</record>
</historical_context>
```

Empty `context.md` files are skipped (existing behavior, unchanged).

---

## 3. Knowledge Consumption Cursor

### How it works

Knowledge digestion uses a **cursor** (`knowledge_consumed_archive_id` in `ArchiveState`) to track which archives have already been processed. This guarantees **no duplicate consumption**.

```
ArchiveState:
  next_archive_id: 6                    # next archive to be created
  knowledge_consumed_archive_id: 3      # archives 1-3 already consumed
```

**Flow:**
1. `get_unprocessed(cursor_name="dream", channel=KNOWLEDGE)` reads entries with `archive_id > knowledge_consumed_archive_id`
2. DreamEngine processes a batch of entries (up to `max_consume_per_run`)
3. `_commit_knowledge_cursor(cursor=max(archive_ids))` advances the cursor
4. Next call starts from the new cursor — already-consumed archives are never revisited

### Cursor + FIFO cleanup interaction

`prune_to_max` (FIFO disk cleanup) must **not delete archives below the consumed cursor** — those are already processed and safe to delete. But it must also **not delete archives above the consumed cursor** that haven't been consumed yet.

To enforce this, `prune_to_max` accepts a `min_safe_id` parameter:

```python
async def prune_to_max(self, max_total: int, min_safe_id: int = 0) -> int:
    """Delete oldest archive dirs exceeding max_total, but never below min_safe_id."""
    ids = await self.list_archives(limit=10_000)
    # Only consider IDs > min_safe_id for deletion
    deletable = [aid for aid in ids if aid > min_safe_id]
    if len(deletable) <= max_total:
        return 0
    ascending = sorted(deletable)
    to_delete = ascending[:-max_total]
    for aid in to_delete:
        shutil.rmtree(self._base / str(aid), ignore_errors=True)
    return len(to_delete)
```

In `scan_once`, the caller reads `knowledge_consumed_archive_id` from `ArchiveState` and passes it as `min_safe_id`:

```python
state = await archive_storage.read_archive_state() or {}
consumed = state.get("knowledge_consumed_archive_id", 0)
deleted = await archive_storage.prune_to_max(max_total, min_safe_id=consumed)
```

This ensures:
- Archives already consumed (≤ cursor) can be deleted freely
- Archives not yet consumed (> cursor) are preserved
- The safety gap between `max_archive_count` (10, trigger) and `max_archive_total` (20, cleanup) provides a buffer for normal operation

---

## 4. DirArchiveStorage Cleanup

### `save_channel_logs` (`framework/memory/stores/dir_archive.py`)

Currently a no-op. Changed to delete archive subdirectories not present in the `entries` list:

```python
async def save_channel_logs(
    self, channel: str, entries: list[dict[str, Any]]
) -> None:
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

### New: `prune_to_max`

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

### New: `cleanup_empty_dirs`

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

---

## 4. DreamEngine Concurrency & Batch Control

### Lock (`framework/memory/consolidation/dream_engine.py`)

```python
class DreamEngine:
    def __init__(self, ...):
        ...
        self._lock = asyncio.Lock()
        self._max_consume_per_run: int = 3  # from DreamEngineConfig
```

### `run` method

```python
async def run(self, context: MemoryContext) -> bool:
    if self._lock.locked():
        logger.info("DreamEngine skipped: already running")
        return False

    async with self._lock:
        unprocessed = await self.history_manager.get_unprocessed(...)
        entries = unprocessed.entries
        if not entries:
            return False

        # Limit per run
        entries = entries[:self._max_consume_per_run]

        if self._consolidator is not None:
            return await self._run_consolidator_limited(unprocessed, entries, context)
        return False
```

`_run_consolidator_limited` is a variant of existing `_run_consolidator` that accepts a pre-sliced `entries` list instead of using `unprocessed.entries` directly.

### `scan_all` — unchanged

Existing logic (iterate registry records, call `run` per scope). Lock protection is inside `run`.

---

## 5. Archive-Count Trigger for Knowledge

### Trigger point: after `cleanup_session`

**File**: `framework/memory/cleanup.py`

Add optional callback parameter:

```python
async def cleanup_session(
    ...
    on_archive_generated: Callable[[], Awaitable[None]] | None = None,
) -> CleanupResult:
    ...
    # After successful archive generation
    if archive_generated and on_archive_generated is not None:
        try:
            await on_archive_generated()
        except Exception:
            logger.debug("Post-cleanup archive trigger failed", exc_info=True)
```

### Callback in bot_project

**File**: `examples/bot_project/bot/service/core.py`

```python
async def _check_and_trigger_dream(self):
    """Check undigested archive count; trigger DreamEngine if over threshold."""
    if self.dream_engine is None or self._main_memory_cfg is None:
        return
    archive_cfg = self._main_memory_cfg.archive
    if archive_cfg is None:
        return
    threshold = archive_cfg.max_archive_count
    context = self._build_memory_context()
    unprocessed = await self.memory_system.archive_manager.get_unprocessed(
        context, cursor_name="dream", channel=ArchiveChannel.KNOWLEDGE,
    )
    if len(unprocessed.entries) >= threshold:
        logger.info(
            "Archive count %d >= threshold %d, triggering DreamEngine",
            len(unprocessed.entries), threshold,
        )
        await self.dream_engine.run(context)
```

This callback is passed as `on_archive_generated` when calling `cleanup_session`.

---

## 6. Lifecycle Maintenance

### `DefaultArchiveRetentionPolicy` (`framework/memory/lifecycle.py`)

```python
class DefaultArchiveRetentionPolicy(ArchiveRetentionPolicy):
    def __init__(
        self,
        max_entries: int | None = 1000,
        max_age_days: int | None = None,
        max_archive_total: int | None = None,   # NEW
    ) -> None:
        ...

    async def get_max_archive_total(self, context: MemoryContext) -> int | None:
        return self._max_archive_total
```

### `scan_once` — additional step

After existing retention logic, add FIFO eviction:

```python
# FIFO eviction: delete oldest dirs exceeding max_archive_total,
# but never delete archives that haven't been consumed by knowledge yet.
if isinstance(archive_storage, DirArchiveStorage):
    max_total = await self._archive_retention.get_max_archive_total(ctx)
    if max_total is not None:
        state = await archive_storage.read_archive_state() or {}
        consumed = state.get("knowledge_consumed_archive_id", 0)
        deleted = await archive_storage.prune_to_max(max_total, min_safe_id=consumed)
        if deleted:
            await archive_storage.cleanup_empty_dirs()
```

This runs in the same `scan_once` pass, after channel-log-based retention.

---

## 8. Config Migration

### `main.yml` (bot_project example)

Before:
```yaml
archive: {enabled: true, max_entries: 1000, retained_consumed_pairs: 3}
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
  max_archive_count: 10       # trigger knowledge update
  max_archive_total: 20       # max dirs on disk
  max_archive_inject: 3       # inject to system prompt
dream_engine:
  enabled: true
  interval: 600
  max_consume_per_run: 3      # per-run batch limit
```

`min_archive_count` and `max_archive_count` in `DreamEngineConfig` are removed. `max_batch_size` is kept as deprecated alias.

### Backward compatibility

- `ArchiveConfig.max_entries` and `retained_consumed_archive_pairs` are preserved with their existing defaults. Old configs that only set these continue to work (no archive-count trigger, no FIFO eviction).
- `DreamEngineConfig.max_batch_size` maps to `max_consume_per_run` if the latter is not explicitly set.
- Missing new fields use defaults — no config breakage.

---

## 9. Files Changed

| File | Change |
|------|--------|
| `framework/ioc/configs/memory.py` | Add 3 params to `ArchiveConfig`; remove 2 from `DreamEngineConfig`; add `max_consume_per_run` |
| `framework/memory/injection/full_injection.py` | `_try_inject_md_archives`: configurable count + chars + ascending order |
| `framework/memory/stores/dir_archive.py` | `save_channel_logs` real impl; add `prune_to_max`, `cleanup_empty_dirs` |
| `framework/memory/consolidation/dream_engine.py` | asyncio.Lock, `max_consume_per_run`, remove dead params, `_run_consolidator_limited` |
| `framework/memory/cleanup.py` | Add `on_archive_generated` callback |
| `framework/memory/lifecycle.py` | `DefaultArchiveRetentionPolicy` gains `max_archive_total`; `scan_once` calls `prune_to_max` |
| `framework/memory/layers/archive.py` | `_do_prune` updated for new storage behavior |
| `framework/ioc/factories/memory.py` | Wire new config params to components |
| `examples/bot_project/config/pools/main.yml` | Updated config format |
| `examples/bot_project/bot/service/core.py` | `_check_and_trigger_dream` callback, pass to cleanup |

---

## 10. Test Coverage

- Unit: `DirArchiveStorage.prune_to_max` — correct deletion order; respects `min_safe_id`
- Unit: `DirArchiveStorage.prune_to_max` with cursor — archives ≤ cursor can be deleted, archives > cursor are preserved
- Unit: `DirArchiveStorage.cleanup_empty_dirs` — empty dirs deleted, non-empty preserved
- Unit: `DreamEngine.run` — lock contention (second call returns False), batch limiting
- Unit: `DreamEngine.run` — cursor advances correctly after batch processing
- Unit: `FullInjectionPolicy._try_inject_md_archives` — truncation at 1000, ascending order, configurable count
- Unit: `ArchiveConfig` — defaults, backward compat, migration
- Integration: cleanup → archive generated → trigger callback → DreamEngine run → cursor advanced
