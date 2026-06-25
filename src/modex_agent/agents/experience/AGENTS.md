<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-10 | Updated: 2026-06-10 -->

# experience (agents)

## Purpose

Experience review agent — creates and updates EXPERIENCE.md files from conversation snapshots. Extends `ScopedFileAgent` pattern with custom experience tool assembly.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Re-exports `ExperienceReviewAgent` |
| `review_agent.py` | `ExperienceReviewAgent(ScopedFileAgent)` — ReAct agent with experience tools, 2-attempt retry, JSONL trace observability |

## How It Works

1. `review()` receives a conversation snapshot + existing experiences XML
2. Builds system prompt from `experience/review` prompt template
3. Assembles a standalone `ReActAgent` with 6 experience tools: Read, Write, Edit, List, RenameDir, Delete
4. Runs up to 100 iterations (configurable) with `SummarizerTrajectoryEmitter` for tracing
5. Retries once on failure (2 attempts total)
6. All file operations are scoped to `experience_dir`

## For AI Agents

### Working In This Directory
- Uses the same `ExperienceReadTool`/`WriteTool`/etc. as the main agent — no separate tool implementations
- Prompt templates come from `SummarizerPromptRegistry` under `experience/review`
- Trace files go to `experience_dir.parent / "review_traces"` (never inside experience data)

### Dependencies
- `modex_agent.agents.summarizer.scoped_file_agent` — `ScopedFileAgent` base class
- `modex_agent.core.experience.meta` — `ExperienceMetaStore` for lifecycle tracking
- `modex_agent.memory.tools.experience` — `ExperienceReadTool`, `ExperienceWriteTool`, etc.
- `modex_agent.agents.summarizer.emitter` — `SummarizerTrajectoryEmitter` for JSONL traces
