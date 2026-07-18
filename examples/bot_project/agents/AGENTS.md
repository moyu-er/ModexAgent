<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# agents

Agent system prompt templates (Markdown). Each file defines the personality, capabilities, and behavioral instructions for a specific agent role.

## Key Files

| File | Description |
|------|-------------|
| `main.md` | Main agent system prompt — general-purpose assistant with tool access |
| `office-expert.md` | Office expert subagent — document processing (Word/Excel/PPT/PDF) |
| `orchestrator.md` | Coder pool main agent — software development orchestrator (dispatches planner/worker/reviewer/scout/oracle) |
| `context-builder.md` | DEPRECATED — context builder subagent; no longer referenced by any pool.yml |
| `delegate.md` | DEPRECATED — delegate subagent; no longer referenced by any pool.yml |
| `oracle.md` | Oracle subagent — knowledge lookup and synthesis |
| `planner.md` | Planner subagent — architectural planning |
| `reviewer.md` | Reviewer subagent — code review |
| `scout.md` | Scout subagent — exploratory search |
| `worker.md` | Worker subagent — implementation execution |

## For AI Agents

### Working In This Directory
- These files are loaded by `pool_builder.py` when constructing agent descriptors.
- File names correspond to agent `address.name` in pool config yml files.
- Editing these directly changes agent behavior — no code changes needed.
- Keep prompts in Chinese/English bilingual style consistent with existing files.
