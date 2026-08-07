<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-10 | Updated: 2026-06-10 -->

# summarizer

## Purpose

Summarizer and file-scoped ReAct agents for offline memory processing. Four concrete agents handle different consolidation tasks — `SessionCompactorAgent` (tool-less, single LLM call) plus three `ScopedFileAgent` subclasses.

## Key Files

| File | Description |
|------|-------------|
| `agent.py` | `SummarizerAgent(Agent)` — single-turn LLM call, no tools. Used for compression, fact extraction, memory update prompts |
| `session_compactor.py` | `SessionCompactorAgent(Agent)` — tool-less agent that generates a structured compact summary via single LLM call. Uses `MessageRole.COMPACT` (mapped to ASSISTANT before LLM call) |
| `scoped_file_agent.py` | `ScopedFileAgent` — ReAct agent base with scoped file tools (read/write/edit/list), `SummarizerTrajectoryEmitter` for JSONL traces, 2-attempt retry |
| `archive_agent.py` | `ArchiveSummarizer(ScopedFileAgent, ArchiveGenerator)` — generates `context.md`/`knowledge.md` from pruned messages (no `index.md`). Topic comes from compact summary's `## Objective` section. Message filtering, transcript formatting, prompt templates |
| `consolidator.py` | `CoreMemoryConsolidator(ScopedFileAgent, CoreMemoryConsolidatorBase)` (renamed from `KnowledgeConsolidator` per ADR-0035) — reads `knowledge.md` from archives, updates `SOUL.md`/`USER.md`/`MEMORY.md` via ReAct |
| `emitter.py` | `SummarizerTrajectoryEmitter` — JSONL trace file writer for agent observability |
| `abc.py` | `ArchiveGenerator` ABC, `CoreMemoryConsolidatorBase` ABC (renamed from `KnowledgeConsolidatorBase` per ADR-0035), `ArchiveSummarizerResult`, `_get_registry()` lazy prompt loader |
| `strategy.py` | `SummarizationStrategy` — configurable summarization approach |

## Agent Hierarchy

```
Agent[E]
└── SummarizerAgent           (single-turn, no tools)
└── SessionCompactorAgent     (tool-less, single LLM call → compact summary)
└── ScopedFileAgent           (ReAct with scoped file tools)
    ├── ArchiveSummarizer     (pruned → archive files)
    ├── CoreMemoryConsolidator (archive → core memory files; renamed from KnowledgeConsolidator per ADR-0035)
    └── ExperienceReviewAgent (conversation → EXPERIENCE.md, in agents/experience/)
```

## For AI Agents

### Working In This Directory
- `ScopedFileAgent._run_agent()` is the shared entry point for all ReAct-based summarizers
- Prompt templates come from `SummarizerPromptRegistry` loaded via `_get_registry()`
- `SummarizerTrajectoryEmitter` writes JSONL traces to `traces/` or `review_traces/` (never inside data directories)
- All file operations are scoped to `allowed_dirs` — agents cannot write outside their target directory

### Testing
- Tests in `tests/unit/agents/`

### Dependencies
- `modex_agent.core.agent` — `Agent[E]`, `AgentContext`
- `modex_agent.core.tool_manager` — `InMemoryToolManager`, `Tool`
- `modex_agent.memory.prompts` — `SummarizerPromptRegistry`
- `modex_agent.memory.prompts.compact` — compact summary prompts (`agent_system.md`, `agent_user.md`) consumed by `SessionCompactorAgent`
- `modex_agent.utils.helpers` — `strip_think`
