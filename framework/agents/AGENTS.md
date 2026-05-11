<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# agents

## Purpose
Agent reasoning pattern implementations. Each sub-package implements a specific reasoning strategy (ReAct, Summarizer) following the `Agent[E]` generic contract.

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `react/` | `ReActAgent` — Thought → Action → Observation loop with `ReActEvent` events (see `react/AGENTS.md`) |
| `summarizer/` | Summarizer agent — content summarization variant |

## For AI Agents

### Working In This Directory
- New agent strategies go in new subdirectories
- Each agent must inherit `Agent[E]` and define `event_enum`
- Builder pattern: each agent subdirectory should have a `builder.py` with `build_agent()` and `build_emitter_factory()`

### Common Patterns
```python
class MyAgent(Agent[MyEvent]):
    event_enum = MyEvent

    async def run(self, context: AgentContext, emitter: ContentEmitter[MyEvent]) -> AgentResult:
        ...
```
## Current Runtime Status

`framework.agents.react` is graph-based (`StartNode`, `LLMNode`, `ToolNode`,
`EndNode`). Clean/full mode behavior and runtime service boundaries are
layered runtime services.
