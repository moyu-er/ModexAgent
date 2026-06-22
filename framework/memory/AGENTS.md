<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# memory

Multi-layer memory system with scope isolation and injection. Layers: Session (short-term), Archive (history), Knowledge (long-term), UserRetentionBuffer (pending context), Pruned (catalog of cleaned-up messages). Supports compaction, consolidation, governance, context injection, and XML truncation.

## Key Files

| File | Description |
|------|-------------|
| `system.py` | `MemorySystemContextManager(ContextManager)` — high-level facade wrapping `MemorySystem` for pipeline |
| `default_system.py` | `DefaultMemorySystem` — standard implementation wiring all layers |
| `history.py` | `MessageHistory`, `ListMessageHistory`, `ScopedMessageHistory`, `inject_attachments_to_history` |
| `context_governance.py` | `ContextGovernance` ABC — `CompositeGovernance`, `TokenBudgetGovernance`, `MicrocompactGovernance`, `ToolChainRepairGovernance` |
| `archive_models.py` | Archive data models (`ArchiveChannel`, `ArchiveWrite`, etc.) |
| `tags.py` | Injection XML element tag names (StrEnum) shared between injection, governance, and truncation |
| `cleanup.py` | `cleanup_session()`, `CleanupResult` — sanitize → prune boundary → archive → write pruned catalog |
| `sanitizer.py` | `DefaultSessionToolChainSanitizer` — removes invalid tool-chain records |
| `recorder.py` | `MemoryAppendRecorder` — records what gets appended and from where |
| `content_transform.py` | `ContentTransformer` ABC — transforms messages for injection |
| `history_search.py` | `HistorySearchStrategy` ABC — search over history |
| `knowledge_search.py` | `KnowledgeSearchStrategy` ABC — search over knowledge |
| `user_buffer.py` | User retention buffer for pending messages |
| `lifecycle.py` | `MemoryMaintenancePolicy`, `SessionRetentionPolicy`, `ArchiveRetentionPolicy`, `KnowledgeRetentionPolicy` ABCs |
| `xml_truncate.py` | XML-based content truncation for governance |
| `utils.py` | Memory utility helpers |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `core/` | ABCs — `MemorySystem`, `MemoryScope`, `MemoryStorage`, `ChatMessage`, `MemoryContext`, layer managers, consolidation (see `core/AGENTS.md`) |
| `layers/` | Concrete layer managers — Session, Archive, Knowledge + `MemoryLayerConfigSet` + `MemoryLayerFactory` |
| `consolidation/` | `DreamEngine` (offline background consolidation) |
| `injection/` | `MemoryInjectionPolicy` → `ContextState` assembly (`FullInjectionPolicy`, `RestrictedInjectionPolicy`) — both inject pruned catalog XML when available |
| `pruned/` | `PrunedManager` + `PrunedStorage` (ABC + FilePrunedStorage) + `PrunedIndexEntry` — catalog of cleaned-up session messages, session-scoped |
| `registry/` | `MemoryStoreRegistry` — storage provider registry |
| `pipeline/` | SystemPromptPipeline — ordered collection of versioned SystemPromptProviders (abc + pipeline + providers) |
| `prompts/` | Prompt package marker (currently __init__.py only) |
| `stores/` | Storage backend implementations (`FileStorage`, `InMemoryStorage`) |
| `tools/` | Agent-facing tools — 6 experience tools (read/write/edit/list/rename/delete), scoped file tools (see `tools/AGENTS.md`) |

## For AI Agents

### Working In This Directory
- Memory scopes: Session, User, Tenant, Agent, Channel, Chat, PeerPair, Composite, Global
- `cleanup_session()` runs after every message append — sanitize → keep/prune boundary → optional archive → write pruned catalog
- Pruned catalog is independent of archive: works with archive off/failed. Topic falls back to time range when no CONTEXT archive available.
- Tool-chain-aware boundary: never split an assistant tool_call from its tool results
- Governance mutates only LLM input copy, never persisted session data
- `archive=None` = session-only mode (standard for subagent)
- `RestrictedInjectionPolicy` is default for subagents — limits session messages to prevent context overflow
- Pruned injection priority: 85 (between knowledge=100 and archive=70). XML catalog points agent to per-session `pruned/{session_id}/` directory.

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
