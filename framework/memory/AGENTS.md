<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-16 | Updated: 2026-05-16 -->

# memory

Three-layer memory system with scope isolation. Layers: Session (short-term), Archive (history), Knowledge (long-term SOUL/USER/MEMORY.md). Supports compaction, consolidation, governance, and context injection.

## Key Files

| File | Description |
|------|-------------|
| `system.py` | `MemorySystem` — high-level facade |
| `default_system.py` | `DefaultMemorySystem` — standard implementation wiring all layers |
| `history.py` | `MessageHistory`, `ListMessageHistory`, `inject_attachments_to_history` |
| `content_transform.py` | Content transformation utilities for memory |
| `context_governance.py` | `ContextGovernance` — LLM input copy governance chain (lossy_compaction, tool_chain_repair, token_budget) |
| `history_search.py` | History search functionality |
| `knowledge_search.py` | Knowledge layer search |
| `lifecycle.py` | Memory lifecycle hooks (AutoCompact, retention) |
| `pending.py` | Pending message handling |
| `recorder.py` | Message recording utilities |
| `utils.py` | Shared memory utilities |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `core/` | ABCs — `MemoryScope`, `MemoryStorage`, `ChatMessage`, scope metadata |
| `layers/` | Concrete layer managers — Session, Archive, Knowledge, Pending + factory |
| `compaction/` | `MessageCompactionPolicy`, `BoundaryPolicy` — per-message compaction decisions |
| `compression/` | Compression coordinator, planners, policies, semantic filter, tool-chain awareness |
| `consolidation/` | `Consolidator` (online LLM-based) + `DreamEngine` (offline background) |
| `retention/` | `RetentionPolicy`, `RetentionConfig` — message lifecycle |
| `injection/` | `MemoryInjectionPolicy` → `ContextState` assembly (full/restricted) |
| `stores/` | `FileStorage` (JSONL+KV), `InMemoryStorage` (plain + scoped variants) |
| `registry/` | Memory provider registry (base, file, in_memory) |
| `archive/` | Archival strategies |

## For AI Agents

### Working In This Directory
- Memory scopes: Session, User, Tenant, Agent, Channel, Chat, PeerPair, Composite, Global
- Two-phase compaction: trigger → plan → summary → commit (tool-chain-aware)
- `BoundaryPolicy` ensures tool-call chains are not broken by truncation
- Governance mutates only LLM input copy, never persisted session data
- `archive=None` = session-only mode (standard for peer/subagent)

### Common Patterns
- `MemoryScope` resolves to scope keys via `MemoryContext`
- Plugin memory providers hook into `add()`, `search()`, `prefetch()`, `on_pre_compress()`
- `MemorySystemModifier` wraps internal managers via plugin injection
