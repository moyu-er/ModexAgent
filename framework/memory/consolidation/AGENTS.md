<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# consolidation

## Purpose
Offline background consolidation that processes archive entries through a ReAct-based KnowledgeConsolidator agent. Transforms session archives into knowledge file updates (SOUL.md, USER.md, MEMORY.md) using per-user locking for independent consolidation across users.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `dream_engine.py` | `DreamEngine` — offline memory consolidation orchestrator. Takes unprocessed archive entries and sends them through a `KnowledgeConsolidatorBase` agent to generate targeted knowledge updates |

## For AI Agents

### Working In This Directory
- Consolidation is triggered externally (not on every message append) — typically on a schedule or after a batch of new archive entries
- Per-user locks (`asyncio.Lock` per user ID) ensure consolidation for user A never blocks user B
- `DreamEngine` consumes a configurable `max_consume_per_run` entries, with `per_archive_iterations` controlling how many consolidation steps per archive entry
- Archives are read; knowledge files are written; no session data is modified

### Common Patterns
- Create `DreamEngine` with `ArchiveMemoryManager`, `KnowledgeMemoryManager`, optional `MemoryStoreRegistry`, and a `KnowledgeConsolidatorBase` instance
- Call `consume()` to process unprocessed archive entries in a loop
- Use `max_consume_per_run=3` (default) to limit work per invocation

## Dependencies

### Internal
- `framework.memory.archive_models` — `ArchiveChannel`, `ArchiveWrite`
- `framework.memory.core.layers` — `ArchiveMemoryManager`, `KnowledgeMemoryManager`
- `framework.memory.core.models` — `ArchiveEntry`
- `framework.memory.core.scope` — `MemoryAgentRole`, `MemoryContext`, `MemoryLayerName`
- `framework.memory.registry.base` — `MemoryStoreRegistry`
- `framework.agents.summarizer.abc` — `KnowledgeConsolidatorBase`

<!-- MANUAL -->
