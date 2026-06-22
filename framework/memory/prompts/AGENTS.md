<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# prompts

## Purpose
Prompt template files used during memory consolidation and experience review. Contains system and user prompt pairs for three operations: archive consolidation (agent/user), experience review (system/user), and knowledge consolidation (system/user). Each pair is a markdown file loaded by the summarizer agents.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `archive/agent_system.md` | System prompt for the archive summarizer agent |
| `archive/agent_user.md` | User prompt template for archive summarization |
| `experience/review_system.md` | System prompt for the experience review agent |
| `experience/review_user.md` | User prompt template for experience review |
| `knowledge/consolidator_system.md` | System prompt for the knowledge consolidator agent |
| `knowledge/consolidator_user.md` | User prompt template for knowledge consolidation |

## For AI Agents

### Working In This Directory
- Prompts are plain markdown files with no Jinja/template syntax — loaded as-is by summarizer agents
- Files are organized into three subdirectories matching their consolidation stage: `archive/`, `experience/`, `knowledge/`
- Each subdirectory has a `*_system.md` (agent persona/instructions) and `*_user.md` (user input template with placeholders)

### Common Patterns
- Prompt files are referenced by path from summarizer implementations in `framework/agents/summarizer/`
- System prompts define agent behavior; user prompts define the input structure for each consolidation type
- To modify consolidation behavior, edit the relevant `*_system.md` file

## Dependencies

### Internal
- Consumed by `framework.agents.summarizer` — `ArchiveSummarizer`, `KnowledgeConsolidator`
- Referenced by path in summarizer agent configurations

<!-- MANUAL -->
