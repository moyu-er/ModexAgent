<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-15 -->

# registry

## Purpose
Storage provider registry that resolves a memory layer + scope pair into a `MemoryStoreBundle`. The `resolve()` method returns a bundle holding `MessageStore`/`KVStore`/`CursorStore` (+ optional `ArchiveStore`), not a single god-interface storage object. Provides two implementations: abstract base and file-backed (default production). The in-memory registry was removed in T03.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init, re-exports registry classes |
| `base.py` | `MemoryStoreRegistry` ABC — defines `resolve(layer, scope, context)` → `MemoryStoreBundle`, `list_records()`, `initialize()`, `close()` |
| `file.py` | `DefaultMemoryStoreRegistry` — local file-backed registry. Resolves layer/scope pairs to `DefaultScopedStorage` directories under a root path, wiring each into a `MemoryStoreBundle`. Manages `.scope.json` metadata per scope directory |
| `modex_agent.persistence.memory_registry` | `HybridMemoryStoreRegistry` — bot SQLite adapter that routes structured state through the workspace persistence manager while retaining knowledge and generated archive documents on files |

## For AI Agents

### Working In This Directory
- `MemoryStoreRegistry` is the central resolver: given a `MemoryLayerName` + `Scope` + `MemoryContext`, it returns a `MemoryStoreBundle`
- `DefaultMemoryStoreRegistry` is the default for production — file persistence under a configured root directory
- The in-memory registry (`InMemoryStoreRegistry`) was removed in T03; tests use temporary file fixtures or `InMemoryScopedStorage` directly
- Both implementations support `initialize()` / `close()` lifecycle methods

### Common Patterns
- Create registry, call `initialize()`, then repeatedly call `resolve()` to get a `MemoryStoreBundle` for different layer/scope combinations
- `list_records()` returns all known `ScopeRecord` entries — useful for discovery and cleanup
- File registry stores metadata in `.scope.json` inside each scope directory

## Dependencies

### Internal
- `modex_agent.core.scope` — `MemoryAgentRole`, `MemoryContext`, `MemoryLayerName`, `Scope`, `ScopeRecord`
- `modex_agent.memory.core.split_stores` — `MemoryStoreBundle`
- `modex_agent.memory.archive_models` — `ArchiveChannel`, archive file key constants
- `modex_agent.memory.stores.scoped_file` — `DefaultScopedStorage`
- `modex_agent.memory.stores.scoped_in_memory` — `InMemoryScopedStorage`
- `modex_agent.memory.stores.utils` — `sanitize_scope_key`

<!-- MANUAL -->
