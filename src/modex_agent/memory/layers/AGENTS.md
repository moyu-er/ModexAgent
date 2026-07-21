<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# layers

## Purpose
Concrete implementations of the four memory layer managers — Session, Archive, Core (formerly "Knowledge"; renamed per ADR-0035), and UserRetentionBuffer — plus their configuration models and a factory for assembling a complete `MemoryLayerSet`. Each manager resolves storage through a `StorageFactory` callback for flexible backend wiring.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `config.py` | Configuration dataclasses — `SessionMemoryConfig` (max_messages, scope), `ArchiveMemoryConfig` (max_entries, cursor_name, scope), `CoreMemoryConfig` (renamed from `KnowledgeMemoryConfig` per ADR-0035; scope, default_files paths, max_changelog_entries), `UserRetentionBufferConfig`, and `MemoryLayerConfigSet` + `StorageFactory` type alias |
| `session.py` | `ScopedSessionMemoryManager` — session layer that resolves storage through `StorageFactory`, delegates to core `SessionMemoryManager` ABC |
| `archive.py` | `ScopedArchiveMemoryManager` — archive layer with coordinate model (`archive_id` monotonic counter), CONTEXT/CORE channel management (channel renamed from KNOWLEDGE per ADR-0035), cursor tracking via `.archive_state.json` |
| `core.py` | `ScopedCoreMemoryManager` (renamed from `knowledge.py` / `ScopedKnowledgeMemoryManager` per ADR-0035) — Core Memory layer backed by markdown files (SOUL.md, USER.md, MEMORY.md). Supports auto-consolidation when file exceeds token threshold |
| `user_buffer.py` | `ScopedUserRetentionBuffer` — storage-backed lifecycle for pruned user context. Persists entries under `".user_retention_entries"` key in scoped storage |
| `factory.py` | `MemoryLayerFactory` — builds a typed `MemoryLayerSet` from a `MemoryStoreRegistry` + `MemoryLayerConfigSet`. Wires all four managers with correct storage factories |

## For AI Agents

### Working In This Directory
- Four layer managers map to the four memory layers: Session, Archive, Core, UserRetentionBuffer
- Each manager is backed by a `StorageFactory` (a callable that receives `MemoryContext` → returns `MemoryStoreBundle`)
- Configs are frozen dataclasses — immutable by design
- `MemoryLayerFactory.build()` is the single entry point for constructing a complete layer set

### Common Patterns
- `ScopedArchiveMemoryManager` uses a coordinate model: `archive_id` is a monotonic counter shared across CONTEXT and CORE channels
- `ScopedCoreMemoryManager` supports an optional `consolidation_fn` callback called when a core memory file exceeds `consolidation_threshold_tokens`
- `MemoryLayerConfigSet` holds configs for all four layers; pass it to `MemoryLayerFactory.build()`
- Archive cursor is persisted in `.archive_state.json` — the sole source of truth for next_archive_id and core_consumed_archive_id (renamed from `knowledge_consumed_archive_id` per ADR-0035)

## Dependencies

### Internal
- `modex_agent.memory.core.layers` — `ArchiveMemoryManager`, `CoreMemoryManager`, `SessionMemoryManager`, `MemoryLayerSet` ABCs
- `modex_agent.core.scope` — `MemoryContext`, `Scope`, `MemoryLayerName`, `SessionScope`, `UserScope`
- `modex_agent.memory.core.split_stores` — `MessageStore`, `KVStore`, `CursorStore`, `ArchiveStore`, `MemoryStoreBundle`
- `modex_agent.memory.core.models` — `ArchiveEntry`, `CoreMemoryContents` (renamed from `LongTermMemory` per ADR-0035), `UnprocessedResult`, `StorageRevision`
- `modex_agent.memory.archive_models` — `ArchiveChannel`, `ArchiveBundleResult`, `ArchiveState`, `ArchiveWrite`
- `modex_agent.memory.history_search` — `HistorySearchStrategy`, `RecentFirstHistorySearch`
- `modex_agent.memory.core_memory_search` — `CoreMemorySearchStrategy`, `FullDumpCoreMemoryStrategy` (renamed from `knowledge_search` / `KnowledgeSearchStrategy` / `FullDumpKnowledgeStrategy` per ADR-0035)
- `modex_agent.memory.registry` — `MemoryStoreRegistry`
- `modex_agent.memory.user_buffer` — `UserBufferEntry`
- `modex_agent.core.provider` — `LLMProvider`

<!-- MANUAL -->
