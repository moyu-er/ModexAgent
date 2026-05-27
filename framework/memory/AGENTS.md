<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-16 | Updated: 2026-05-22 -->

# memory

Three-layer memory system with scope isolation. Layers: Session (short-term), Archive (history), Knowledge (long-term SOUL/USER/MEMORY.md). Supports compaction, consolidation, governance, and context injection.

## Key Files

| File | Description |
|------|-------------|
| `system.py` | `MemorySystemContextManager(ContextManager)`, `create_memory_system()` — high-level facade for pipeline |
| `default_system.py` | `DefaultMemorySystem` — standard implementation wiring all layers |
| `history.py` | `MessageHistory`, `ListMessageHistory`, `inject_attachments_to_history` |
| `context_governance.py` | `ContextGovernance` ABC — `CompositeGovernance`, `TokenBudgetGovernance`, `MicrocompactGovernance`, `ToolChainRepairGovernance` |
| `archive_generation.py` | `ArchiveGenerationStrategy` (ABC), `DualLLMArchiveGenerationStrategy`, `ArchiveInputMessage`, `SummarizerLike` |
| `cleanup.py` | `cleanup_session()`, `CleanupResult` — main entry point for session cleanup + archive |
| `sanitizer.py` | `DefaultSessionToolChainSanitizer` — removes invalid tool-chain records |
| `pending.py` | `PendingPrunedInputExtractor`/`Injector` — handles messages pruned from session but not yet delivered |
| `recorder.py` | `MemoryAppendRecorder` — records what gets appended and from where |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `core/` | ABCs — `MemoryScope`, `MemoryStorage`, `ChatMessage`, `MemoryContext`, scope metadata, layer managers |
| `layers/` | Concrete layer managers — Session, Archive, Knowledge, Pending + `MemoryLayerFactory` + config |
| `consolidation/` | `DreamEngine` (offline background consolidation) |
| `injection/` | `MemoryInjectionPolicy` → `ContextState` assembly (`FullInjectionPolicy`, `RestrictedInjectionPolicy`, `ToolMessageFilterStrategy`) |
| `registry/` | `MemoryStoreRegistry` — storage provider registry |

## For AI Agents

### Working In This Directory
- Memory scopes: Session, User, Tenant, Agent, Channel, Chat, PeerPair, Composite, Global
- `cleanup_session()` runs after every message append — sanitize → keep/prune boundary → optional archive
- Tool-chain-aware boundary: never split an assistant tool_call from its tool results
- Governance mutates only LLM input copy, never persisted session data
- `archive=None` = session-only mode (standard for subagent)
- `RestrictedInjectionPolicy` is default for subagents — limits session messages to prevent context overflow

### Subagent Memory Lifecycle
1. Each subagent gets its own `MemorySystemContextManager` with isolated workspace
2. `MemoryAgentRole.SUBAGENT` scope — session-only by default, no knowledge layer
3. `cleanup_session()` runs directly from `ScopedMessageHistory` after every message append
4. `AgentPool` session eviction (TTL + LRU cap) triggers context cleanup
5. `_cleanup_subagent_memory()` called on session end via explicit cleanup

### Common Patterns
- `MemoryScope` resolves to scope keys via `MemoryContext`
- Plugin memory providers hook into `add()`, `search()`, `prefetch()`, `on_pre_compress()`
- `MemorySystemModifier` wraps internal managers via plugin injection
- `MemoryLayerConfigSet` holds all layer configs; `MemoryLayerFactory` builds from it
