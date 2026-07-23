<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-23 -->

# agents

Agent system prompt templates (Markdown). Each file defines the personality, capabilities, and behavioral instructions for a specific agent role.

## Key Files

| File | Description |
|------|-------------|
| `orchestrator.md` | Coder pool main agent — explores, plans, reviews, delegates to explore + coder |
| `coder.md` | Coder subagent — external coding (OpenCode) implementation execution |
| `explore.md` | Explore subagent — fast read-only codebase exploration and reconnaissance |
| `default.md` | Default pool main agent — general-purpose conversational assistant |
| `office-expert.md` | Office expert subagent — document processing (Word/Excel/PPT/PDF) |

## For AI Agents

### Working In This Directory
- These files are loaded by `pool_builder.py` when constructing agent descriptors.
- File names correspond to agent `address.name` in pool config yml files.
- Editing these directly changes agent behavior — no code changes needed.
