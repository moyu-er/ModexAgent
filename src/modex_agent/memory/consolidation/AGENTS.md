<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# consolidation

## Purpose
Offline background consolidation that processes archive entries through a ReAct-based `CoreMemoryConsolidator` agent (renamed from `KnowledgeConsolidator` per ADR-0035). Transforms session archives into core memory file updates (SOUL.md, USER.md, MEMORY.md) using per-user locking for independent consolidation across users.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `dream_engine.py` | `DreamEngine` — offline memory consolidation orchestrator. Takes unprocessed archive entries and sends them through a `CoreMemoryConsolidatorBase` agent (renamed from `KnowledgeConsolidatorBase` per ADR-0035) to generate targeted core memory updates |

## For AI Agents

### Working In This Directory
- Consolidation is triggered externally (not on every message append) — typically on a schedule or after a batch of new archive entries
- Per-user locks (`asyncio.Lock` per user ID) ensure consolidation for user A never blocks user B
- `DreamEngine` consumes a configurable `max_consume_per_run` entries, with `per_archive_iterations` controlling how many consolidation steps per archive entry
- Archives are read; core memory files are written; no session data is modified

### Common Patterns
- Create `DreamEngine` with `ArchiveMemoryManager`, `CoreMemoryManager`, optional `MemoryStoreRegistry`, and a `CoreMemoryConsolidatorBase` instance
- Call `consume()` to process unprocessed archive entries in a loop
- Use `max_consume_per_run=3` (default) to limit work per invocation

## Dependencies

### Internal
- `modex_agent.memory.archive_models` — `ArchiveChannel`, `ArchiveWrite`
- `modex_agent.memory.core.layers` — `ArchiveMemoryManager`, `CoreMemoryManager`
- `modex_agent.memory.core.models` — `ArchiveEntry`
- `modex_agent.memory.core.scope` — `MemoryAgentRole`, `MemoryContext`, `MemoryLayerName`
- `modex_agent.memory.registry.base` — `MemoryStoreRegistry`
- `modex_agent.agents.summarizer.abc` — `CoreMemoryConsolidatorBase` (renamed from `KnowledgeConsolidatorBase` per ADR-0035)

<!-- MANUAL -->
