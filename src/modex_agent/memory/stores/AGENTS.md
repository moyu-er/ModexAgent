<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-15 -->

# stores

## Purpose
Concrete storage backend implementations for the memory system. Provides file-based, in-memory, archive, and core memory storage variants (the core memory storage class was renamed from "knowledge storage" per ADR-0035). These are the persistence layer that `MemoryStoreRegistry` resolves to, implementing the four split store ABCs (`MessageStore`/`KVStore`/`CursorStore`/`ArchiveStore`) composed by `MemoryStoreBundle`. The SQLite adapters live in `modex_agent.persistence.adapters`.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `file.py` | `FileStorage` — file-based cross-platform storage backend. Manages `messages.jsonl`, `history.jsonl`, `changelog.jsonl`, `kv.json` per scope directory with atomic JSON writes |
| `scoped_file.py` | `DefaultScopedStorage` — local-file storage for one layer/scope directory. Implements all four split store ABCs. Manages `messages.jsonl` (conversation history), `kv.json` (key-value metadata), archive state, and cursor tracking |
| `scoped_in_memory.py` | `InMemoryScopedStorage` — in-memory storage for one layer/scope target. Implements all four split store ABCs with per-key write locks |
| `dir_archive.py` | `DirArchiveStorage` — archive storage backed by a directory tree of markdown files. Implements the split store ABCs. Layout: `{base_dir}/state.json`, `{id}/context.md`, `{id}/knowledge.md`, `{id}/index.md` (the on-disk archive content file name `knowledge.md` is intentionally retained — only the dataclass field was renamed to `core` per ADR-0035) |
| `markdown_core.py` | `MarkdownCoreMemoryStorage(DefaultScopedStorage)` (renamed from `markdown_knowledge.py` / `MarkdownKnowledgeStorage` per ADR-0035) — Core Memory layer storage backed by individual `.md` files on disk. `set("SOUL.md", content)` writes `SOUL.md` as a real file; non-`.md` keys fall through to `kv.json` |
| `utils.py` | Storage utilities — `sanitize_scope_key()` (filesystem-safe directory name via `pathvalidate`), `ensure_scope_dir()` (create scope directory) |

The standalone `InMemoryStorage` (in `in_memory.py`) was removed in T03. Tests use `InMemoryScopedStorage` or temporary file fixtures.

## For AI Agents

### Working In This Directory
- File backends implement the split store ABCs from `modex_agent.memory.core.split_stores`: `MessageStore` (`load_messages`, `save_messages`, `append_message`, `get_revision`, `prune_messages`, `pin_message`, `unpin_message`, `delete_message`, `cleanup_expired`), `KVStore` (`get`, `set`, `delete`, `list_keys`), `CursorStore` (`get_last_cursor`, `set_last_cursor`), `ArchiveStore` (log + channel-log + state methods).
- `DefaultScopedStorage` implements all four ABCs in one class; `MemoryStoreBundle` wires its four faces into one bundle. The SQLite backend uses four independent `Sqlite*Store` adapters instead.
- File-based stores use atomic JSON writes (write to `.tmp` then rename) for crash safety
- `DefaultScopedStorage` is the primary file backend — used by `DefaultMemoryStoreRegistry`
- `MarkdownCoreMemoryStorage` (renamed from `MarkdownKnowledgeStorage` per ADR-0035) overrides `get()`/`set()` for `.md` keys to read/write actual markdown files; all other keys go to `kv.json`
- `DirArchiveStorage` organizes archives in numbered subdirectories with `context.md`, `knowledge.md`, `index.md` per archive ID (the `knowledge.md` filename is intentionally retained on disk)

### Common Patterns
- Each storage backend wraps a lock (`AioRWLock` or `NoOpStorageLock`) for concurrent access
- `safe_atomic_replace()` (from `modex_agent.memory.utils`) is used for crash-safe file writes across all file-based stores
- `sanitize_scope_key()` ensures scope keys are valid directory names on all platforms (Windows reserved names handled)
- `ensure_scope_dir()` creates the storage directory lazily

## Dependencies

### Internal
- `modex_agent.memory.core.split_stores` — `MessageStore`, `KVStore`, `CursorStore`, `ArchiveStore`, `MemoryStoreBundle` ABCs
- `modex_agent.memory.core.store_metadata` — `StoreMetadata` ABC (lock + base_path)
- `modex_agent.memory.core.lock` — `AioRWLock`, `NoOpStorageLock`, `StorageLock`
- `modex_agent.memory.core.models` — `StorageRevision`
- `modex_agent.core.scope` — `MemoryAgentRole`, `MemoryContext`, `ScopeRecord`, `MemoryLayerName`
- `modex_agent.memory.archive_models` — `ArchiveChannel`, archive file constants
- `modex_agent.memory.utils` — `safe_atomic_replace`
- `modex_agent.utils.file_io` — `read_json_robust`, `read_jsonl_robust`

<!-- MANUAL -->
