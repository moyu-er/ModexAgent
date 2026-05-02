<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# memory

## Purpose
Three-layer memory system with scope isolation. Layers: Short-term (session window), History (persistent), Long-term (SOUL/USER/MEMORY.md). Supports compaction, consolidation (DreamEngine), and context injection.

## Key Files
| File | Description |
|------|-------------|
| `system.py` | `MemorySystem` — high-level memory system facade |
| `default_system.py` | `DefaultMemorySystem` — standard implementation wiring all layers |
| `history.py` | `MessageHistory`, `ListMessageHistory`, `history_to_list`, `inject_attachments_to_history` |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `core/` | ABCs — `MemoryScope`, `MemoryStorage`, `ChatMessage`, scope metadata |
| `managers/` | Layer managers — `ShortTerm`, `History`, `LongTerm` |
| `compaction/` | `MessageCompactionPolicy`, `BoundaryPolicy`, `SummaryStrategy` |
| `compression/` | Compression utilities |
| `consolidation/` | `Consolidator` (online LLM-based) + `DreamEngine` (offline background) |
| `injection/` | `MemoryInjectionPolicy` → `ContextState` assembly |
| `stores/` | `FileStorage` (JSONL+KV), `InMemoryStorage` |
| `layers/` | Concrete layer implementations |
| `archive/` | Archival strategies |
| `registry/` | Memory provider registry |

## For AI Agents

### Working In This Directory
- Memory scopes: `Session`, `User`, `Tenant`, `Agent`, `Channel`, `Chat`, `PeerPair`, `Composite`, `Global`
- Two-phase compaction: trigger → plan → summary → commit
- `DreamEngine` runs background consolidation; `Consolidator` runs online compression
- `BoundaryPolicy` ensures tool-call chains are not broken by truncation
- `ScopeRecord` + `.scope.json` for recoverable scope metadata

### Design Documents
- `agent_docs/memory-system-redesign.md`
- `agent_docs/memory-system-redesign-plan.md`
## Current Runtime Status

Memory providers are separate from ReAct runtime-state persistence. Suspend/resume
uses the `RuntimeStateStore` naming described in `docs/current-runtime.md`.
