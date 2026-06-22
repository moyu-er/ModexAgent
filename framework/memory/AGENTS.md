<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# memory

Multi-layer memory system with scope isolation and injection. Layers: Session (short-term), Archive (history), Knowledge (long-term), UserRetentionBuffer (pending context), Pruned (catalog of cleaned-up messages). Supports compaction, consolidation, governance, context injection, and XML truncation.

## Purpose

The `memory/` module provides a comprehensive memory system for agents. It manages message histories across multiple scopes (Session, User, Tenant, Agent, Channel, Chat, PeerPair, Composite, Global), handles compaction and consolidation (transforming session messages into archive files and knowledge summaries), governs context windows via injection policies, and provides agent-facing tools for experience and file operations.

## Key Files

| File | Description |
|------|-------------|
| `system.py` | `MemorySystemContextManager(ContextManager)` — high-level facade wrapping `MemorySystem` for pipeline integration |
| `default_system.py` | `DefaultMemorySystem` — standard implementation wiring all layers |
| `history.py` | `MessageHistory`, `ListMessageHistory`, `ScopedMessageHistory`, `inject_attachments_to_history()` |
| `context_governance.py` | `ContextGovernance` ABC — `CompositeGovernance`, `TokenBudgetGovernance`, `MicrocompactGovernance`, `ToolChainRepairGovernance`. Mutates only LLM input copy, never persisted session data |
| `archive_models.py` | Archive data models — `ArchiveChannel`, `ArchiveWrite`, archive metadata types |
| `tags.py` | Injection XML element tag names (StrEnum) shared between injection, governance, and truncation |
| `cleanup.py` | `cleanup_session()`, `CleanupResult` — sanitize → prune boundary → archive → write pruned catalog. Runs after every message append |
| `sanitizer.py` | `DefaultSessionToolChainSanitizer` — removes invalid tool-chain records (never split assistant.tool_calls from matching tool results) |
| `recorder.py` | `MemoryAppendRecorder` — records what gets appended and from where |
| `content_transform.py` | `ContentTransformer` ABC — transforms messages for injection |
| `history_search.py` | `HistorySearchStrategy` ABC — search over message history |
| `knowledge_search.py` | `KnowledgeSearchStrategy` ABC — search over knowledge base |
| `user_buffer.py` | User retention buffer for pending messages |
| `lifecycle.py` | `MemoryMaintenancePolicy`, `SessionRetentionPolicy`, `ArchiveRetentionPolicy`, `KnowledgeRetentionPolicy` ABCs — lifecycle hooks for memory maintenance |
| `xml_truncate.py` | XML-based content truncation for governance — ensures injected XML stays within token budget |
| `utils.py` | Memory utility helpers |

## Subdirectories

| Directory | Files | Purpose |
|-----------|-------|---------|
| `core/` | 9 py | ABCs — `MemorySystem`, `MemoryScope` (9 implementations), `MemoryStorage`, `ChatMessage`, `MemoryContext`, layer managers, consolidation types, `StorageLock` (see `core/AGENTS.md`) |
| `layers/` | 6 py | Concrete layer managers — `SessionMemoryManager`, `ArchiveMemoryManager`, `KnowledgeMemoryManager`, `UserRetentionBufferManager` + `MemoryLayerConfigSet` + `MemoryLayerFactory` |
| `consolidation/` | 1 py | `DreamEngine` — offline background consolidation of session → archive → knowledge |
| `injection/` | 3 py | `MemoryInjectionPolicy` → `ContextState` assembly: `FullInjectionPolicy` (full context), `RestrictedInjectionPolicy` (session-only, limited context window). Both inject pruned catalog XML when available |
| `pruned/` | 3 py | `PrunedManager` + `PrunedStorage` (ABC + `FilePrunedStorage`) + `PrunedIndexEntry` — catalog of cleaned-up session messages, session-scoped |
| `registry/` | 3 py | `MemoryStoreRegistry` — storage provider registry (`BaseMemoryStoreRegistry`, `FileMemoryStoreRegistry`, `InMemoryMemoryStoreRegistry`) |
| `pipeline/` | 3 py | `SystemPromptPipeline` — ordered collection of versioned `SystemPromptProvider` (ABC + pipeline orchestrator + provider implementations) |
| `prompts/` | 6 files | Prompt templates: `archive/` (agent_system.md, agent_user.md), `experience/` (review_system.md, review_user.md), `knowledge/` (consolidator_system.md, consolidator_user.md) |
| `stores/` | 7 py | Storage backend implementations — `FileStorage`, `InMemoryStorage`, `DirArchiveStorage`, `ScopedFileStorage`, `ScopedInMemoryStorage`, `MarkdownKnowledgeStorage`, storage utilities |
| `tools/` | 7 py | Agent-facing tools — 6 experience tools (read/write/edit/list/rename/delete), scoped file tools (read/write/edit/list) for summarizer agents (see `tools/AGENTS.md`) |

### Memory Scope Hierarchy

```
MemoryScope (ABC)
├── SessionScope       — single conversation session
├── UserScope          — user-level across sessions
├── TenantScope        — tenant/organization level
├── AgentScope         — agent-level memory
├── ChannelScope       — channel/room level
├── ChatScope          — chat-level
├── PeerPairScope      — user↔agent pair
├── CompositeScope     — composite of multiple scopes
└── GlobalScope        — global memory
```

### Injection Architecture

```
MemoryInjectionPolicy (ABC)
├── FullInjectionPolicy         — full context for main agents
└── RestrictedInjectionPolicy   — session-only, limited context (default for subagents)

Both inject pruned catalog XML at priority 85
```

### Cleanup Flow

```
Message appended
  → sanitizer removes invalid tool chains
  → prune boundary (tool-chain-aware: never split tool_call from results)
  → optional archive (session → archive files)
  → write pruned catalog
  → injection reads pruned catalog when building LLM context
```

### Pruned Catalog
- Independent of archive: works with archive off or failed
- Topic falls back to time range when no CONTEXT archive available
- Injection priority: 85 (between knowledge=100 and archive=70)
- XML catalog points agent to per-session `pruned/{session_id}/` directory

### Memory Layers

| Layer | Manager | Persistence | Purpose |
|-------|---------|-------------|---------|
| Session | `SessionMemoryManager` | `MemoryStorage` | Short-term conversation messages |
| Archive | `ArchiveMemoryManager` | File-based (markdown) | Historical context, automatically generated |
| Knowledge | `KnowledgeMemoryManager` | File-based (markdown) | Long-term knowledge (SOUL.md, USER.md, MEMORY.md) |
| UserBuffer | `UserRetentionBufferManager` | In-memory | Pending user messages |
| Pruned | `PrunedManager` + `PrunedStorage` | File-based (JSON) | Catalog of cleaned-up messages |

## For AI Agents

### Working In This Directory
- Memory scopes: Session, User, Tenant, Agent, Channel, Chat, PeerPair, Composite, Global
- `cleanup_session()` runs after every message append — sanitize → keep/prune boundary → optional archive → write pruned catalog
- Pruned catalog is independent of archive: works with archive off/failed
- Tool-chain-aware boundary: never split an assistant tool_call from its tool results
- Governance mutates only LLM input copy, never persisted session data
- `archive=None` = session-only mode (standard for subagent)
- `RestrictedInjectionPolicy` is default for subagents — limits session messages to prevent context overflow
- Pruned injection priority: 85 (between knowledge=100 and archive=70)

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

### Testing
- Tests in `tests/unit/memory/`
- Mock storage backends, test with in-memory implementations

## Dependencies

### Internal
- `framework.core.types` — `MessageRole`, `MessageType`, `ToolCall`
- `framework.core.context` — `ContextManager`, `ContextState`
- `framework.core.session_id` — `SessionInfo`
- `framework.core.events` — `AgentEvent`
- `framework.core.history` — `MessageHistory` ABC
- `framework.utils` — xml, helpers, sanitizer
- `framework.agents.summarizer` — `ArchiveSummarizer`, `KnowledgeConsolidator` (for consolidation pipeline)

### External
- `pydantic` — data models
- `pyyaml` — frontmatter parsing

<!-- MANUAL -->
<!-- Additional manual entries can be added below this line. -->

