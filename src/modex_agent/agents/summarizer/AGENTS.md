<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-10 | Updated: 2026-06-10 -->

# summarizer

## Purpose

Summarizer and file-scoped ReAct agents for offline memory processing. `SessionCompactorAgent` performs tool-less LLM calls — single-pass by default, or sliding-window map + reduce when the turn's `ContextBudget` says the transcript exceeds the model's window (per-model compaction PRD §4.4); `ArchiveSummarizer` and `CoreMemoryConsolidator` specialize `ScopedFileAgent`.

## Key Files

| File | Description |
|------|-------------|
| `session_compactor.py` | `SessionCompactorAgent(Agent)` — tool-less agent that generates a structured compact summary. Without a declared limit: single LLM call (legacy behavior). With `ContextBudget` on `compact()`: derives `S = min(max_output_tokens, clamp(limit×5%, 1000, 4000))` and `B = limit − S − estimated prompt overhead`; transcript ≤ B → single pass with output cap S, else message-granularity segments (tool_call/result pairs never split, ≤ B×0.7 each, concurrency 3, one retry per segment, first segment carries `previous_summary`) → one reduce call; oversized reduce input re-batches one more layer (depth cap 2); total call cap `MAX_COMPACTION_LLM_CALLS = 12`; any failure degrades to an empty summary (tail-only). Usage aggregated over every call via one `UsageCollectingProvider`. Uses `MessageRole.COMPACT` (mapped to ASSISTANT before LLM call); tool results truncated to `tool_output_max_chars` (default 2000) in serialization |
| `scoped_file_agent.py` | `ScopedFileAgent` — ReAct agent base with scoped file tools (read/write/edit/list), `SummarizerTrajectoryEmitter` for JSONL traces, 2-attempt retry |
| `archive_agent.py` | `ArchiveSummarizer(ScopedFileAgent, ArchiveGenerator)` — generates `context.md`/`knowledge.md` from pruned messages (no `index.md`). Topic comes from compact summary's `## Objective` section. Message filtering, transcript formatting, prompt templates |
| `consolidator.py` | `CoreMemoryConsolidator(ScopedFileAgent, CoreMemoryConsolidatorBase)` (renamed from `KnowledgeConsolidator` per ADR-0035) — reads `knowledge.md` from archives, updates `SOUL.md`/`USER.md`/`MEMORY.md` via ReAct |
| `emitter.py` | `SummarizerTrajectoryEmitter` — JSONL trace file writer for agent observability |
| `abc.py` | `ArchiveGenerator` ABC, `CoreMemoryConsolidatorBase` ABC (renamed from `KnowledgeConsolidatorBase` per ADR-0035), `ArchiveSummarizerResult`, `_get_registry()` lazy prompt loader |

## Agent Hierarchy

```
Agent[E]
├── SessionCompactorAgent     (tool-less; single call → compact summary, or segmented map+reduce under a declared budget)
└── ScopedFileAgent           (ReAct with scoped file tools)
    ├── ArchiveSummarizer     (pruned → archive files)
    ├── CoreMemoryConsolidator (archive → core memory files; renamed from KnowledgeConsolidator per ADR-0035)
    ```

## For AI Agents

### Working In This Directory
- `ScopedFileAgent._run_agent()` is the shared entry point for all ReAct-based summarizers
- Prompt templates come from `PromptRegistry` loaded via `_get_registry()`
- `SummarizerTrajectoryEmitter` writes JSONL traces to `traces/` or `review_traces/` (never inside data directories)
- All file operations are scoped to `allowed_dirs` — agents cannot write outside their target directory

### Testing
- Tests in `tests/unit/agents/`

### Dependencies
- `modex_agent.core.agent` — `Agent[E]`, `AgentContext`
- `modex_agent.core.tool_manager` — `InMemoryToolManager`, `Tool`
- `modex_agent.memory.prompts` — `PromptRegistry`
- `modex_agent.memory.prompts.compact` — compact summary prompts (`agent_system.md`, `agent_user.md`) consumed by `SessionCompactorAgent`
- `modex_agent.utils.helpers` — `strip_think`
