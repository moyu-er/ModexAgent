<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-02 | E1/E2 ownership -->

# memory

Multi-layer memory system with scope isolation and injection. Layers: Session (short-term), Archive (history), Core (long-term, formerly "Knowledge"; renamed per ADR-0035), Compact (structured session summaries), Pruned (catalog of cleaned-up messages). Supports compaction, consolidation, governance, context injection, and XML truncation.

Storage is backend-pluggable: the file backend (`DefaultScopedStorage`) ships as the framework default; the SQLite backend (`Sqlite*Store` adapters under `modex_agent.persistence`) is the bot's default (ADR-0023). Both implement the same split store ABCs.

## Purpose

The `memory/` module provides a comprehensive memory system for agents. It manages message histories across multiple scopes (Session, User, Tenant, Agent, Channel, Chat, Composite, Global), handles compaction and consolidation (transforming session messages into archive files and core memory summaries), governs context windows via injection policies, and provides agent-facing scoped file tools for summarizer agents.

## Key Files

| File | Description |
|------|-------------|
| `context.py` | `ContextManager` ABC, `ContextState`, `InMemoryContextManager`, and shared runtime prompt helpers. |
| `scope.py` | `MemoryContext`, `Scope` and its concrete dimensions, memory ownership enums/records, `build_scope()`, and `scope_path_key()`; canonical `RecordScope` remains in `core/scope.py`. |
| `system.py` | `MemorySystemContextManager(ContextManager)` — high-level facade wrapping `MemorySystem` for pipeline integration. `load()` renders the capability-section anchor block after fork context and before core memory. Skills prompt injection arrives through the `SkillsCapability` provider in that generic block; memory has no skill-specific load parameter. A custom `MEMORY_SYSTEM` owns its full prompt assembly, while command resolution remains independent through `SkillResolver`. |
| `default_system.py` | `DefaultMemorySystem` — standard implementation wiring all layers |
| `history.py` | Concrete `ListMessageHistory` and `ScopedMessageHistory` implementations of `core.history.MessageHistory`. |
| `context_governance.py` | `ContextGovernance`, `CompositeGovernance`, `ContextBudgetGovernance`, and `ToolChainRepairGovernance`; transforms only the LLM input copy. |
| `archive_models.py` | Archive data models — typed generated documents, channel writes, bundle results, and archive state |
| `tags.py` | Injection XML element tag names (StrEnum) shared between injection, governance, and truncation |
| `cleanup.py` | `cleanup_session()`, `CleanupResult` — 5-phase pipeline: trigger+boundary → compact generation → session commit (`[compact_summary]`+`[tail]`) → pruned catalog write (topic from compact's `## Objective`) → archive generation (optional, default off; archive state advances atomically inside this phase, DreamEngine polling is the only archive-consolidation trigger). `CleanupResult` carries `tokens_before`/`tokens_after` (char-estimated via `TokenEstimator`) for savings/thrash metrics. `cleanup_session()` takes `compactor` param instead of `user_retention` |
| `sanitizer.py` | `DefaultSessionToolChainSanitizer` — removes invalid tool-chain records (never split assistant.tool_calls from matching tool results) |
| `recorder.py` | `MemoryAppendRecorder` — records what gets appended and from where |
| `content_transform.py` | `ContentTransformer` ABC — transforms messages for injection |
| `history_search.py` | `HistorySearchStrategy` ABC — search over message history |
| `core_memory_search.py` | `CoreMemorySearchStrategy` ABC (renamed from `knowledge_search.py` / `KnowledgeSearchStrategy` per ADR-0035) — search over core memory |
| `lifecycle.py` | `DefaultMemoryMaintenancePolicy` (concrete background-maintenance scan) and retention ABCs — `ArchiveRetentionPolicy`, `CoreMemoryRetentionPolicy` — with per-scope thresholds called from `scan_once` |
| `xml_truncate.py` | XML-based content truncation for governance — ensures injected XML stays within token budget |
| `utils.py` | Memory utility helpers |
| `hooks.py` | Typed cleanup, context-assembly, core-update, and consolidation hook contracts plus `MemoryHookRunner`. |
| `cleanup_hooks.py` | `TodoReorientationHook`, which persists the post-cleanup Todo reminder without re-entering history cleanup. |

## Subdirectories

| Directory | Files | Purpose |
|-----------|-------|---------|
| `core/` | Memory system and split-store ABCs, shared layer/consolidation models, metadata, and locking. |
| `layers/` | Concrete session, archive, and core-memory managers plus layer configuration/factory. |
| `consolidation/` | `DreamEngine` offline consolidation. |
| `injection/` | Full/restricted memory injection policies and archive injection. |
| `prompt_pipeline/` | Consumer-owned `SystemPromptProvider` implementations; the provider/pipeline seams remain in `core/prompt.py`. |
| `prompts/` | Archive, compact, and core-memory templates. |
| `pruned/` | Pruned-message catalog models, rendering, storage, and manager. |
| `registry/` | File-backed `MemoryStoreRegistry` implementation. |
| `stores/` | File and in-memory split-store implementations. |
| `tools/` | Scoped read/write/edit/list tools for summarizer agents. |

### Memory Scope Hierarchy

Configurable scopes live in `modex_agent.memory.scope`; only the canonical
`RecordScope` value remains in `modex_agent.core.scope`. The `Scope` ABC extracts
a `RecordScope` from a `MemoryContext`. Config accepts
`scope: list[str]` (a single string is auto-wrapped by `build_scope`).

```
Scope (ABC) — extract(context) -> RecordScope
├── SessionScope       — single conversation session
├── UserScope          — user-level across sessions
├── TenantScope        — tenant/organization level
├── AgentScope         — agent-level memory
├── ChannelScope       — channel/room level
├── ChatScope          — chat-level
├── CompositeScope     — composite of multiple scopes (merges via RecordScope.merge)
└── GlobalScope        — global memory (empty path segment)
```

`build_scope(dims)` is the factory that turns dimension short-names into a
`Scope`. See `core/AGENTS.md` for the foundational `RecordScope` contract
(`canonical()`, `to_path_segment()`, `merge()`).

### Injection Architecture

```
MemoryInjectionPolicy (ABC — single method: assemble)
├── FullInjectionPolicy         — core memory (budget-trimmed; no disclaimer when core memory empty)
└── RestrictedInjectionPolicy   — session messages only, empty system prompt

Archive, pruned catalog, provider blocks, prefetch → SystemPromptProvider
pipeline providers (version-cached, in prompt_pipeline/providers.py)
```

### Cleanup Flow

```
Message appended
  → (1) trigger + boundary (sanitizer removes invalid tool chains; prune boundary is tool-chain-aware)
  → (2) compact generation (SessionCompactorAgent → structured compact summary via single LLM call)
  → (3) session commit ([compact_summary] + [tail messages])
  → (4) pruned catalog write (topic from compact summary's ## Objective section)
  → (5) archive generation (optional, default off — context.md + knowledge.md, no index.md; archive state advances atomically, DreamEngine polling is the only consolidation trigger)
```

### Memory Lifecycle Hooks

Memory lifecycle hooks are a **separate dispatch system** from the ReAct
`HookRunner` — they fire directly from `cleanup_session()` via
`MemoryHookRunner`, with no ReAct coupling. One runner per memory system
(`DefaultMemorySystem._hook_runner`), passed by reference to every
`ScopedMessageHistory` so late registration is visible to all histories
sharing the same system.

**Dispatch points**: `CLEANUP_TRIGGERED` (after the 3 early returns:
under-threshold, all-invalid, no-safe-boundary — fires before phase 2) and
`CLEANUP_FINISHED` (before every `triggered=True` return — 4 return points:
all-invalid, no-safe-boundary, revision-conflict, normal).

**Tuple-snapshot dispatch**: `dispatch()` iterates a `tuple(self._hooks)`
snapshot, so hooks added during dispatch do not affect the current pass.

**Timeout/error isolation**: 10s per-hook timeout
(`_DEFAULT_MEMORY_HOOK_TIMEOUT`). `CancelledError` propagates; `TimeoutError`
and all other exceptions are logged with the hook class name + point and
swallowed — cleanup continues regardless.

**Truth table** (5 paths, verified by `TestCleanupHookTruthTable`):

| path | triggered | pruned | TRIGGERED | FINISHED |
|---|---|---|---|---|
| under_threshold | False | 0 | 0 | 0 |
| all_invalid | True | total | 0 | 1 |
| no_safe_boundary | True | 0 | 0 | 1 |
| revision_conflict | True | 0 | 1 | 1 |
| normal | True | prune_count | 1 | 1 |

**Todo reorientation persistence path**: `TodoReorientationHook` (in
`cleanup_hooks.py`) persists its reminder via
`SessionMemoryManager.add_messages` directly (Path A) — NOT
`ScopedMessageHistory.append`. This bypasses `MemoryAppendRecorder` /
`MemoryProvider` fan-out and prevents cleanup recursion. The reminder is
visible to the agent on the next iteration via `ScopedMessageHistory.to_list()`
(cache invalidated after append).

**Registration**: `DefaultMemorySystem.add_cleanup_hook(hook)` delegates to
the shared runner. `TodoReorientationHook` arrives as a `todo`-capability
roster entry (ADR-0047) — the assembly core dispatches it onto the memory
runner only for agents where the capability is effective, landing before the
bot-registered `UserNoticeCleanupHook` (triggered + finished); the retired
unconditional `factory.py` registration is gone.

### Pruned Catalog
- Independent of archive: works with archive off or failed
- Topic comes from compact summary's `## Objective` section
- Injection priority: 85 (between core memory=100 and archive=70)
- XML catalog points agent to per-session `pruned/{session_id}/` directory

### Memory Layers

| Layer | Manager | Persistence | Purpose |
|-------|---------|-------------|---------|
| Session | `SessionMemoryManager` | `MemoryStoreBundle` | Short-term conversation messages |
| Archive | `ArchiveMemoryManager` | `ArchiveStore` (file or SQLite), with optional markdown path capability | Historical context, automatically generated |
| Core | `CoreMemoryManager` | File-based (markdown) | Core memory (SOUL.md, USER.md, MEMORY.md) |
| Pruned | `PrunedManager` + `PrunedStorage` | File-based (Markdown transcripts + JSONL index) | Catalog of cleaned-up messages |

## For AI Agents

### Working In This Directory
- Memory scopes: Session, User, Tenant, Agent, Channel, Chat, Composite, Global (PeerPair removed in T04)
- `cleanup_session()` runs after every message append — 5-phase pipeline (see Cleanup Flow above)
- Pruned catalog is independent of archive: works with archive off/failed
- Tool-chain-aware boundary: never split an assistant tool_call from its tool results
- Governance mutates only LLM input copy, never persisted session data
- `archive=None` = session-only mode (standard for all agents — archive/core off by default, compact always on)
- `RestrictedInjectionPolicy` is default for subagents — limits session messages to prevent context overflow
- Pruned injection priority: 85 (between core memory=100 and archive=70)

### Subagent Memory Lifecycle
1. Each subagent gets its own `MemorySystemContextManager` with isolated workspace
2. `MemoryAgentRole.SUBAGENT` scope — session-only by default, no core memory layer
3. `cleanup_session()` runs directly from `ScopedMessageHistory` after every message append
4. `AgentPool` session eviction (TTL + LRU cap) triggers context cleanup
5. `_cleanup_subagent_memory()` called on session end via explicit cleanup

### Common Patterns
- `Scope.extract(context)` in `modex_agent.memory.scope` returns the foundational `core.scope.RecordScope`; consumers derive a DB key via `.canonical()` or a filesystem path via `scope_path_key()`.
- Plugin memory providers hook into `add()`, `search()`, `prefetch()`, `on_pre_compress()`
- `MemorySystemModifier` wraps internal managers via plugin injection
- `MemoryLayerConfigSet` holds all layer configs; `MemoryLayerFactory` builds from it

### Testing
- Tests in `tests/unit/memory/`
- Mock storage backends, test with in-memory implementations

## Dependencies

### Internal
- `modex_agent.core.message` — `ChatMessage`, `MessageRole`, `ToolCall`
- `modex_agent.core.prompt` — `SystemPromptProvider`, `SystemPromptPipeline`
- `modex_agent.core.scope` — canonical `RecordScope`
- `modex_agent.core.session_id` — `SessionInfo`
- `modex_agent.core.history` — `MessageHistory` ABC
- `modex_agent.utils` — xml, helpers, sanitizer
- `modex_agent.agents.summarizer` — `ArchiveSummarizer`, `CoreMemoryConsolidator`, `SessionCompactorAgent` (for consolidation and compact pipelines)

### External
- `pydantic` — data models
- `pyyaml` — frontmatter parsing

<!-- MANUAL -->
<!-- Additional manual entries can be added below this line. -->
