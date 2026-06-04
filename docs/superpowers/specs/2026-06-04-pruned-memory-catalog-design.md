# Pruned Memory Catalog Design

**Date:** 2026-06-04
**Status:** Draft

## Problem

When `cleanup_session()` prunes old messages to stay within token/message limits, the pruned
content is only preserved as:

1. **Backup** — full pre-cleanup snapshot (all messages including kept portion), max 10 files.
2. **Archive summaries** — LLM-generated compressed summaries (CONTEXT/KNOWLEDGE channels).

Neither provides **retrievable original content with structured indexing**. Backup is an opaque
dump; archive is lossy. There is no way for the agent to know what historical memories exist
and selectively retrieve them.

## Proposal

Introduce a **pruned memory layer**: store the exact pruned messages as retrievable files with
a structured index, and inject a lightweight catalog into the system prompt telling the agent
where to find them.

This layer is independent of archive configuration and available to all agent types (main,
subagent, etc.).

## Design

### 1. Data Model

```python
@dataclass(frozen=True)
class PrunedIndexEntry:
    id: int                    # PrunedManager's own monotonic ID
    start_time: int            # epoch seconds of first pruned message, 0 if unknown
    end_time: int              # epoch seconds of last pruned message, 0 if unknown
    cleanup_time: int          # epoch seconds, always present
    start_time_display: str    # "YYYY-MM-DD HH:MM" or ""
    end_time_display: str      # "YYYY-MM-DD HH:MM" or ""
    cleanup_time_display: str  # "YYYY-MM-DD HH:MM"
    topic: str                 # from archive CONTEXT summary; fallback to time range when archive off
    message_count: int
    content_filename: str      # exactly matches the actual file name on disk
```

### 2. Storage Layout

```
data/memory/{pool}/
  pruned/
    pruned_2026-06-03_09.30-2026-06-03_10.20.jsonl   # start and end time both available
    pruned_2026-06-02_15.30.jsonl                     # missing start or end, uses cleanup time
    index.jsonl                                        # one JSON entry per line
  archive/default/   # existing
  backups/            # existing
```

**File naming rules:**
- Both start and end available: `pruned_{start}-{end}.jsonl`
- Either missing: `pruned_{cleanup_time}.jsonl` (single time point)
- Time format in filename: `YYYY-MM-DD_HH.MM`

**Pruned content file format:** one JSON dict per line (same format as `messages.jsonl`),
containing the raw pruned messages in original order.

**Index file format:** one JSON-serialized `PrunedIndexEntry` dict per line.

### 3. PrunedStorage ABC

```python
class PrunedStorage(ABC):
    @abstractmethod
    def write_pruned(self, filename: str, messages: list[dict]) -> None: ...

    @abstractmethod
    def append_index(self, entry: PrunedIndexEntry) -> None: ...

    @abstractmethod
    def read_index(self) -> list[PrunedIndexEntry]: ...

    @abstractmethod
    def has_content(self) -> bool: ...

    @abstractmethod
    def prune_oldest(self, keep_count: int) -> None: ...

    @abstractmethod
    def get_directory_path(self) -> str:
        """Return path for injection XML."""
```

### 4. FilePrunedStorage

Concrete implementation using local files:

- `pruned_dir: Path` — root directory for pruned files and index
- `index_filename: str = "index.jsonl"` — index file name
- Directory created on first write if not exists
- Atomic writes (tmp file + rename)

### 5. PrunedManager

```python
class PrunedManager:
    def __init__(
        self,
        storage: PrunedStorage,
        max_files: int = 50,
        topic_max_chars: int = 200,
    ) -> None: ...

    # -- Called by cleanup_session() --

    async def write_pruned(
        self,
        pruned_messages: list[dict],
        topic: str | None,
        cleanup_time: datetime,
    ) -> None:
        """Write pruned messages to file + append index entry.

        Steps:
        1. Extract start/end time from first/last message created_at.
        2. Generate filename (start-end or single cleanup time).
        3. If topic is None (archive off/failed), build fallback from time range + count.
        4. Write messages to file.
        5. Build PrunedIndexEntry with self-incremented ID.
        6. Append to index.jsonl.
        7. Prune oldest if over max_files.
        """

    # -- Called by injection policies --

    def get_injection_xml(self) -> str | None:
        """Return catalog XML for system prompt, or None if no pruned content."""

    # -- Internal --

    def _generate_filename(self, start: datetime | None, end: datetime | None,
                           cleanup_time: datetime) -> str: ...
    def _build_index_entry(self, ...) -> PrunedIndexEntry: ...
```

**Own monotonic ID:** PrunedManager maintains an internal counter (derived from existing index
entries on startup, or from a state file).

### 6. Timestamp Guarantee

Every `ChatMessage` gets `created_at` filled when entering the storage layer.

In `ScopedMessageHistory.append()` and `extend()`, before writing to storage:

```python
def _ensure_timestamps(self, messages: list[ChatMessage]) -> list[ChatMessage]:
    now = datetime.now(UTC)
    patched = []
    for msg in messages:
        if msg.created_at is None:
            patched.append(msg.model_copy(update={"created_at": now}))
        else:
            patched.append(msg)
    return patched
```

- Storage: `datetime` (existing format in JSONL)
- Index: epoch seconds (int) for efficient comparison
- Injection XML: `"YYYY-MM-DD HH:MM"` for model readability
- LLM API: not sent — filtered by `_sanitize_api_messages()` allow-list (already works)

### 7. Cleanup Flow Change

Before:

```
1. Backup
2. Sanitize tool chains
3. Compute keep/prune boundary
4. Extract user retention entries
5. Archive generation (LLM)
6. Replace messages
```

After:

```
1. Backup
2. Sanitize tool chains
3. Compute keep/prune boundary
4. Archive generation (LLM, if enabled) → extract topic
5. Write pruned content + index entry     ← NEW (topic="" if archive off)
6. Prune oldest pruned files              ← NEW
7. Extract user retention entries
8. Replace messages
```

Archive generation runs first (when enabled) so `topic` can be reused. When archive is off
or fails, `topic=""`.

### 8. System Prompt Injection

#### XML Format

```xml
<memory_archives>
<!-- Pruned conversation segments are stored as read-only files in the directory below.
     An index.jsonl in the same directory catalogs each segment with topic, time range,
     and file path.
     NOTE: index.jsonl is editable — you should update it to improve topic descriptions
     or categorization when you have better context. The pruned segment files themselves
     must NOT be modified. -->
  <directory path="F:/tool/pythonProject/ModexAgent/examples/bot_project/data/memory/main/pruned"/>
</memory_archives>
```

- Only injected when `has_content()` returns True
- `path` is absolute (via `Path.resolve()`), consistent with knowledge injection's `file="..."` attribute
- No hardcoded index.jsonl full path — just mentions it exists in the same directory

#### Priority

| Priority | Section |
|----------|---------|
| 100 | Knowledge (SOUL/USER/MEMORY) |
| **85** | **Pruned catalog (new)** |
| 70 | Archive (compressed summaries) |
| 60 | Provider static blocks |
| 50 | Provider prefetch |

Catalog is tiny (a few lines), rarely affected by token trimming. If budget is tight, archive
summaries are trimmed before catalog — the catalog is more valuable because it tells the model
more memories are available on demand.

#### Both Injection Policies

Both `FullInjectionPolicy` and `RestrictedInjectionPolicy` accept optional `PrunedManager`:

```python
if self._pruned_manager:
    xml = self._pruned_manager.get_injection_xml()
    if xml:
        sections.append(_PromptSection(content=xml, priority=85))
```

Shared logic lives in `PrunedManager.get_injection_xml()`. Policies just call and append.

### 9. Dependency Injection Path

```
bot/service/builders.py (or framework factory)
  → FilePrunedStorage(pruned_dir=<workspace>/data/memory/{pool}/pruned)
  → PrunedManager(storage, max_files=config.max_pruned_files)
  → pass to FullInjectionPolicy(pruned_manager=manager)
  → pass to RestrictedInjectionPolicy(pruned_manager=manager)
  → pass to cleanup_session() call site
```

Same `PrunedManager` instance shared by cleanup (write) and injection (read).

When `memorySystem` is None, `PrunedManager` is also None — both cleanup and injection skip it.

### 10. Eviction

- Configurable `max_pruned_files` (default: 50)
- When count exceeds limit, oldest pruned file is deleted
- Corresponding entry removed from `index.jsonl`
- Triggered at the end of `PrunedManager.write_pruned()`

### 11. Independence from Archive

Pruned layer works regardless of archive configuration:

| Archive Status | Pruned Behavior |
|---------------|-----------------|
| Enabled | topic from CONTEXT summary, truncated to 200 chars |
| Disabled | topic = time range display, e.g. `"2026-06-03 09:30 ~ 10:20 (45 messages)"` |
| Failed (threshold) | topic = time range display, pruned still written |

When archive is off, `topic` falls back to `"{start_display} ~ {end_display} ({count} messages)"`
using the time fields already in the entry. If start/end are also missing, uses `cleanup_time_display`.

### 12. Extensibility Points

The design preserves future extension paths without premature abstraction:

- **Index entry schema**: `PrunedIndexEntry` can add fields (tags, categories) without breaking
  existing storage or injection.
- **Storage backend**: `PrunedStorage(ABC)` can be implemented with database or vector store.
- **Search**: `read_index()` can be replaced with semantic search in a subclass.
- **Injection content**: `get_injection_xml()` can be extended to include more metadata.

### 13. Configuration

New config fields (in `MemoryConfig` / pool YAML):

```yaml
pruned:
  enabled: true           # default true
  max_files: 50           # max pruned files to retain
  topic_max_chars: 200    # truncate topic from archive summary
```

### 14. Test Plan

| Test Category | What to Verify |
|--------------|---------------|
| PrunedIndexEntry | Serialization round-trip, default values, display format |
| FilePrunedStorage | Write/read index, write pruned file, has_content, prune_oldest |
| PrunedManager | Filename generation (both/single time), write_pruned flow, injection XML |
| Timestamp guarantee | Messages without created_at get auto-filled on append/extend |
| Cleanup integration | Pruned written after archive gen, works with archive off |
| Injection | FullInjectionPolicy + RestrictedInjectionPolicy both inject catalog |
| Injection skip | No injection when no pruned content, no injection when manager is None |
| Eviction | Oldest files deleted, index cleaned up, respects max_files |
| Subagent | Subagent sessions produce pruned content, catalog injected |
