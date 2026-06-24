<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# layers

## Purpose
Concrete implementations of the four memory layer managers — Session, Archive, Knowledge, and UserRetentionBuffer — plus their configuration models and a factory for assembling a complete `MemoryLayerSet`. Each manager resolves storage through a `StorageFactory` callback for flexible backend wiring.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `config.py` | Configuration dataclasses — `SessionMemoryConfig` (max_messages, scope), `ArchiveMemoryConfig` (max_entries, cursor_name, scope), `KnowledgeMemoryConfig` (scope, default_files paths, max_changelog_entries), `UserRetentionBufferConfig`, and `MemoryLayerConfigSet` + `StorageFactory` type alias |
| `session.py` | `ScopedSessionMemoryManager` — session layer that resolves storage through `StorageFactory`, delegates to core `SessionMemoryManager` ABC |
| `archive.py` | `ScopedArchiveMemoryManager` — archive layer with coordinate model (`archive_id` monotonic counter), CONTEXT/KNOWLEDGE channel management, cursor tracking via `.archive_state.json` |
| `knowledge.py` | `ScopedKnowledgeMemoryManager` — knowledge layer backed by markdown files (SOUL.md, USER.md, MEMORY.md). Supports auto-consolidation when file exceeds token threshold |
| `user_buffer.py` | `ScopedUserRetentionBuffer` — storage-backed lifecycle for pruned user context. Persists entries under `".user_retention_entries"` key in scoped storage |
| `factory.py` | `MemoryLayerFactory` — builds a typed `MemoryLayerSet` from a `MemoryStoreRegistry` + `MemoryLayerConfigSet`. Wires all four managers with correct storage factories |

## For AI Agents

### Working In This Directory
- Four layer managers map to the four memory layers: Session, Archive, Knowledge, UserRetentionBuffer
- Each manager is backed by a `StorageFactory` (a callable that receives `MemoryContext` → returns `MemoryStorage`)
- Configs are frozen dataclasses — immutable by design
- `MemoryLayerFactory.build()` is the single entry point for constructing a complete layer set

### Common Patterns
- `ScopedArchiveMemoryManager` uses a coordinate model: `archive_id` is a monotonic counter shared across CONTEXT and KNOWLEDGE channels
- `ScopedKnowledgeMemoryManager` supports an optional `consolidation_fn` callback called when a knowledge file exceeds `consolidation_threshold_tokens`
- `MemoryLayerConfigSet` holds configs for all four layers; pass it to `MemoryLayerFactory.build()`
- Archive cursor is persisted in `.archive_state.json` — the sole source of truth for next_archive_id and knowledge_consumed_archive_id

## Dependencies

### Internal
- `framework.memory.core.layers` — `ArchiveMemoryManager`, `KnowledgeMemoryManager`, `SessionMemoryManager`, `MemoryLayerSet` ABCs
- `framework.memory.core.scope` — `MemoryContext`, `MemoryScope`, `MemoryLayerName`, `SessionScope`, `UserScope`
- `framework.memory.core.storage` — `MemoryStorage`
- `framework.memory.core.models` — `ArchiveEntry`, `LongTermMemory`, `UnprocessedResult`, `StorageRevision`
- `framework.memory.archive_models` — `ArchiveChannel`, `ArchiveBundleResult`, `ArchiveState`, `ArchiveWrite`
- `framework.memory.history_search` — `HistorySearchStrategy`, `RecentFirstHistorySearch`
- `framework.memory.knowledge_search` — `KnowledgeSearchStrategy`, `FullDumpKnowledgeStrategy`
- `framework.memory.registry` — `MemoryStoreRegistry`
- `framework.memory.user_buffer` — `UserBufferEntry`
- `framework.core.provider` — `LLMProvider`

<!-- MANUAL -->
