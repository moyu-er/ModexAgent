<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# stores

## Purpose
Concrete storage backend implementations for the memory system. Provides file-based, in-memory, archive, knowledge, and scoped storage variants. These are the persistence layer that `MemoryStoreRegistry` resolves to, implementing the `MemoryStorage` ABC.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `file.py` | `FileStorage` — file-based cross-platform storage backend. Manages `messages.jsonl`, `history.jsonl`, `changelog.jsonl`, `kv.json` per scope directory with atomic JSON writes |
| `in_memory.py` | `InMemoryStorage` — ephemeral in-memory storage using nested dicts. No persistence across restarts. Suitable for unit tests and development |
| `scoped_file.py` | `DefaultScopedStorage(MemoryStorage)` — local-file storage for one layer/scope directory. Manages `messages.jsonl` (conversation history), `kv.json` (key-value metadata), archive state, and cursor tracking |
| `scoped_in_memory.py` | `InMemoryScopedStorage(MemoryStorage)` — in-memory storage for one layer/scope target. Implements `MemoryStorage` ABC with per-key write locks |
| `dir_archive.py` | `DirArchiveStorage(MemoryStorage)` — archive storage backed by a directory tree of markdown files. Layout: `{base_dir}/state.json`, `{id}/context.md`, `{id}/knowledge.md`, `{id}/index.md` |
| `markdown_knowledge.py` | `MarkdownKnowledgeStorage(DefaultScopedStorage)` — knowledge layer storage backed by individual `.md` files on disk. `set("SOUL.md", content)` writes `SOUL.md` as a real file; non-`.md` keys fall through to `kv.json` |
| `utils.py` | Storage utilities — `sanitize_scope_key()` (filesystem-safe directory name via `pathvalidate`), `ensure_scope_dir()` (create scope directory) |

## For AI Agents

### Working In This Directory
- All stores implement `MemoryStorage` ABC methods: `get()`, `set()`, `delete()`, `add_message()`, `get_messages()`, `get_logs()`, `get_revision()`, etc.
- File-based stores use atomic JSON writes (write to `.tmp` then rename) for crash safety
- `DefaultScopedStorage` is the primary production backend — used by `DefaultMemoryStoreRegistry`
- `MarkdownKnowledgeStorage` overrides `get()`/`set()` for `.md` keys to read/write actual markdown files; all other keys go to `kv.json`
- `DirArchiveStorage` organizes archives in numbered subdirectories with `context.md`, `knowledge.md`, `index.md` per archive ID

### Common Patterns
- Each storage backend wraps a lock (`AioRWLock` or `NoOpStorageLock`) for concurrent access
- `safe_atomic_replace()` (from `framework.memory.utils`) is used for crash-safe file writes across all file-based stores
- `sanitize_scope_key()` ensures scope keys are valid directory names on all platforms (Windows reserved names handled)
- `ensure_scope_dir()` creates the storage directory lazily

## Dependencies

### Internal
- `framework.memory.core.storage` — `MemoryStorage` ABC
- `framework.memory.core.lock` — `AioRWLock`, `NoOpStorageLock`, `StorageLock`
- `framework.memory.core.models` — `StorageRevision`
- `framework.memory.core.scope` — `MemoryAgentRole`, `MemoryContext`, `ScopeRecord`, `MemoryLayerName`
- `framework.memory.archive_models` — `ArchiveChannel`, archive file constants
- `framework.memory.utils` — `safe_atomic_replace`
- `framework.utils.file_io` — `read_json_robust`, `read_jsonl_robust`

<!-- MANUAL -->
