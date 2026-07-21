<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# injection

## Purpose
Memory injection policies that convert MemorySystem state into a structured context bundle (system prompt + messages) for LLM consumption. Provides two strategies: `FullInjectionPolicy` (main agents — injects core memory, archive, session, and pruned catalog) and `RestrictedInjectionPolicy` (subagents/peers — session messages only, no core memory or archive).

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `policy.py` | `MemoryInjectionPolicy` ABC — abstract `assemble(context, memory_system, query)` returning `InjectionResult` |
| `full_injection.py` | `FullInjectionPolicy` — main agent policy. Injects core memory (priority 100; formerly "knowledge" — renamed per ADR-0035), archive summaries (70), provider blocks (60/50), pruned catalog XML (85), and session visible messages |
| `restricted_injection.py` | `RestrictedInjectionPolicy` — subagent/peer policy. Session messages only (+ optional pruned catalog injection), configurable `max_session_messages` (default 50). No core memory, archive, or provider blocks |

## For AI Agents

### Working In This Directory
- Injection policies are the bridge between persisted memory and LLM context — they decide what goes into the system prompt and messages
- `FullInjectionPolicy` follows a deterministic priority-ordered assembly: disclaimer (110) → core memory (100) → pruned catalog (85) → archive (70) → provider static (60) → provider prefetch (50) → session messages
- `RestrictedInjectionPolicy` is the default for subagents — deliberately limited to prevent context overflow
- Both policies inject pruned catalog XML (when available) at priority 85 to guide agents to cleaned-up session data

### Common Patterns
- Policies are constructed with `MemorySystem` + optional `PrunedManager` + layer-specific parameters
- `assemble()` is async — returns `InjectionResult(system_prompt, messages)`
- `max_session_messages` on `RestrictedInjectionPolicy` caps how many recent session messages are included

## Dependencies

### Internal
- `modex_agent.memory.core.models` — `InjectionResult`, `MemoryBudget`
- `modex_agent.memory.core.scope` — `MemoryContext`
- `modex_agent.memory.core.system` — `MemorySystem`
- `modex_agent.memory.pruned.manager` — `PrunedManager`
- `modex_agent.memory.tags` — `ArchiveTag`, `CoreMemoryTag` (renamed from `KnowledgeTag` per ADR-0035), `PrunedTag`
- `modex_agent.memory.utils` — `estimate_text_tokens`
- `modex_agent.utils.xml` — XML formatting helpers

<!-- MANUAL -->
