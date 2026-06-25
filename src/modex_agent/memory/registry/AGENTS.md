<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# registry

## Purpose
Storage provider registry that resolves a memory layer + scope pair into a concrete `MemoryStorage` instance. Provides three implementations: abstract base, file-backed (default production), and in-memory (testing/ephemeral).

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init, re-exports registry classes |
| `base.py` | `MemoryStoreRegistry` ABC — defines `resolve(layer, scope, context)` → `MemoryStorage`, `list_records()`, `initialize()`, `close()` |
| `file.py` | `DefaultMemoryStoreRegistry` — local file-backed registry. Resolves layer/scope pairs to `DefaultScopedStorage` directories under a root path. Manages `.scope.json` metadata per scope directory |
| `in_memory.py` | `InMemoryStoreRegistry` — ephemeral in-memory registry. Creates one `InMemoryScopedStorage` per layer/scope tuple. Data lost on process restart |

## For AI Agents

### Working In This Directory
- `MemoryStoreRegistry` is the central resolver: given a `MemoryLayerName` + `MemoryScope` + `MemoryContext`, it returns the appropriate `MemoryStorage`
- `DefaultMemoryStoreRegistry` is the default for production — file persistence under a configured root directory
- `InMemoryStoreRegistry` is used for unit tests and ephemeral sessions
- Both implementations support `initialize()` / `close()` lifecycle methods

### Common Patterns
- Create registry, call `initialize()`, then repeatedly call `resolve()` to get storage for different layer/scope combinations
- `list_records()` returns all known `ScopeRecord` entries — useful for discovery and cleanup
- File registry stores metadata in `.scope.json` inside each scope directory
- In-memory registry holds stores in a `dict[(MemoryLayerName, scope_key), InMemoryScopedStorage]`

## Dependencies

### Internal
- `modex_agent.memory.core.scope` — `MemoryAgentRole`, `MemoryContext`, `MemoryLayerName`, `MemoryScope`, `ScopeRecord`
- `modex_agent.memory.core.storage` — `MemoryStorage`
- `modex_agent.memory.archive_models` — `ArchiveChannel`, archive file key constants
- `modex_agent.memory.stores.scoped_file` — `DefaultScopedStorage`
- `modex_agent.memory.stores.scoped_in_memory` — `InMemoryScopedStorage`
- `modex_agent.memory.stores.utils` — `sanitize_scope_key`

<!-- MANUAL -->
