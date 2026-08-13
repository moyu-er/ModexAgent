<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-13 -->

# agents

Agent system prompt templates (Markdown). Each file defines the role, behavioral contract, and communication style for a specific agent.

## Key Files

| File | Description |
|------|-------------|
| `orchestrator.md` | Main agent of the coder pool — software engineering agent that investigates, plans, implements, and verifies. Delegates to subagents for isolated-context work. |
| `explore.md` | Read-only codebase exploration subagent — searches, reads, and analyzes code. Returns structured findings with file paths and line numbers. |
| `general.md` | General-purpose subagent with full capabilities — research, planning, implementation, documentation. Same tool access as the main agent, in a fresh isolated context. |
| `coder.md` | (Disabled) Former implementation subagent. Retained for reference; no longer loaded as a template. |
| `default.md` | Default pool main agent — general-purpose conversational assistant. |
| `reviewer.md` | Review pool main agent — senior code reviewer that inspects changes, runs verification, and approves or requests revisions. |
| `office-expert.md` | Office document specialist — creates, reads, edits, and analyzes Word/Excel/PowerPoint files via OfficeCLI. |

## For AI Agents

### Working In This Directory
- These files are loaded by `pool_builder.py` when constructing agent descriptors.
- File names correspond to agent `address.name` in pool config yml files.
- Editing these directly changes agent behavior — no code changes needed.
- A `.md` file without a matching `templates/*.yml` is not loaded as a subagent (e.g. `coder.md` is retained but `coder.yml` is disabled).
