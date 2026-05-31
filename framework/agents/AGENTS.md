<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-31 -->

# agents

Agent reasoning pattern implementations. Each sub-package implements a specific strategy following the `Agent[E]` generic contract.

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `react/` | `ReActAgent` — Thought→Action→Observation loop with `ReActEvent` events (see `react/AGENTS.md`) |
| `summarizer/` | `SummarizerAgent` — single-turn tool-free summarization with predefined prompts |

## For AI Agents

### Working In This Directory
- New agent strategies go in new subdirectories
- Each agent must inherit `Agent[E]` and define `event_enum`
- `SummarizerAgent` uses predefined prompt types: PROMPT_COMPRESSION, PROMPT_FACT_EXTRACTION, PROMPT_MEMORY_UPDATE, PROMPT_KNOWLEDGE_CONSOLIDATION

### Common Patterns
```python
class MyAgent(Agent[MyEvent]):
    event_enum = MyEvent

    async def run(self, context: AgentContext, emitter: ContentEmitter[MyEvent]) -> AgentResult:
        ...
```
