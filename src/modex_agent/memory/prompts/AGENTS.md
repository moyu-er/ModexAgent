<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# prompts

## Purpose
Prompt template files used during memory consolidation and compact generation. Contains system and user prompt pairs for archive consolidation, core memory consolidation (formerly "knowledge consolidation"; renamed per ADR-0035), and session compact summaries. Experience review prompts live with the Experience capability.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `archive/agent_system.md` | System prompt for the archive summarizer agent |
| `archive/agent_user.md` | User prompt template for archive summarization |
| `core_memory/consolidator_system.md` | System prompt for the core memory consolidator agent (renamed from `knowledge/consolidator_system.md` per ADR-0035) |
| `core_memory/consolidator_user.md` | User prompt template for core memory consolidation (renamed from `knowledge/consolidator_user.md` per ADR-0035) |
| `compact/agent_system.md` | System prompt for the session compactor agent |
| `compact/agent_user.md` | User prompt template for session compact summary generation |

## For AI Agents

### Working In This Directory
- Prompts are plain markdown files with no Jinja/template syntax — loaded as-is by summarizer agents
- Files are organized into three subdirectories matching their consolidation stage: `archive/`, `core_memory/` (renamed from `knowledge/` per ADR-0035), `compact/`
- Each subdirectory has a `*_system.md` (agent persona/instructions) and `*_user.md` (user input template with placeholders)

### Common Patterns
- Prompt files are referenced by path from summarizer implementations in `modex_agent/agents/summarizer/`
- System prompts define agent behavior; user prompts define the input structure for each consolidation type
- To modify consolidation behavior, edit the relevant `*_system.md` file

## Dependencies

### Internal
- Consumed by `modex_agent.agents.summarizer` — `ArchiveSummarizer`, `CoreMemoryConsolidator`, `SessionCompactorAgent` (renamed from `KnowledgeConsolidator` per ADR-0035)
- Referenced by path in summarizer agent configurations

<!-- MANUAL -->
