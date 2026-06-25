<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-10 | Updated: 2026-06-10 -->

# summarizer

## Purpose

Summarizer and file-scoped ReAct agents for offline memory processing. Three concrete agents handle different consolidation tasks, all extending the `ScopedFileAgent` base class.

## Key Files

| File | Description |
|------|-------------|
| `agent.py` | `SummarizerAgent(Agent)` — single-turn LLM call, no tools. Used for compression, fact extraction, memory update prompts |
| `scoped_file_agent.py` | `ScopedFileAgent` — ReAct agent base with scoped file tools (read/write/edit/list), `SummarizerTrajectoryEmitter` for JSONL traces, 2-attempt retry |
| `archive_agent.py` | `ArchiveSummarizer(ScopedFileAgent, ArchiveGenerator)` — generates `context.md`/`knowledge.md`/`index.md` from pruned messages. Message filtering, transcript formatting, prompt templates |
| `consolidator.py` | `KnowledgeConsolidator(ScopedFileAgent, KnowledgeConsolidatorBase)` — reads `knowledge.md` from archives, updates `SOUL.md`/`USER.md`/`MEMORY.md` via ReAct |
| `emitter.py` | `SummarizerTrajectoryEmitter` — JSONL trace file writer for agent observability |
| `abc.py` | `ArchiveGenerator` ABC, `KnowledgeConsolidatorBase` ABC, `ArchiveSummarizerResult`, `_get_registry()` lazy prompt loader |
| `strategy.py` | `SummarizationStrategy` — configurable summarization approach |

## Agent Hierarchy

```
Agent[E]
└── SummarizerAgent           (single-turn, no tools)
└── ScopedFileAgent           (ReAct with scoped file tools)
    ├── ArchiveSummarizer     (pruned → archive files)
    ├── KnowledgeConsolidator (archive → knowledge files)
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
- `modex_agent.utils.helpers` — `strip_think`
