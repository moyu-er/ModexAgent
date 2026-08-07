<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-08-04 -->

# injection

## Purpose
Memory injection policies that assemble a budget-trimmed core memory bundle (core memory XML; no disclaimer when core memory is empty) for LLM consumption. Archive, pruned catalog, provider blocks, and prefetch are handled exclusively by `SystemPromptProvider` pipeline providers (version-cached), NOT by injection policies.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `policy.py` | `MemoryInjectionPolicy` ABC — single-method contract: abstract `assemble(context, memory_system, query)` returning `InjectionResult` |
| `full_injection.py` | `FullInjectionPolicy` — main agent policy. Assembles core memory (priority 100), token-budget-trimmed. When core memory is empty: no disclaimer, empty system_prompt, CoreMemoryProvider not added to pipeline. Does NOT inject archive, pruned, blocks, or prefetch — those are pipeline providers |
| `restricted_injection.py` | `RestrictedInjectionPolicy` — subagent/peer policy. Session messages only, empty system prompt. No core memory, archive, or provider blocks |

## For AI Agents

### Working In This Directory
- Injection policies assemble the **core memory bundle only** (core memory XML, budget-trimmed; no disclaimer when core memory empty). All other system prompt content is handled by dedicated `SystemPromptProvider` pipeline providers with version-based caching.
- `FullInjectionPolicy.assemble()` calls `_inject_core_memory()` → `_trim_by_priority()`. When core memory is empty, no disclaimer is injected and system_prompt is empty. No archive, pruned, blocks, or prefetch injection.
- `RestrictedInjectionPolicy.assemble()` returns `InjectionResult(system_prompt="", messages=history)` — no content injection at all.
- The `MemoryInjectionPolicy` ABC is a single-method contract (`assemble`). No capability-query methods (`injects_archive`, `injects_pruned`, etc.) — those were removed in the convergence.

### Common Patterns
- Policies are constructed with `budget: MemoryBudget | None = None` (FullInjectionPolicy) or no args (RestrictedInjectionPolicy).
- `assemble()` is async — returns `InjectionResult(system_prompt, messages)`.
- Archive config is passed to `MemorySystemContextManager(archive_injection_config=...)`, not to the policy.

## Dependencies

### Internal
- `modex_agent.memory.core.models` — `InjectionResult`, `MemoryBudget`
- `modex_agent.memory.core.scope` — `MemoryContext`
- `modex_agent.memory.core.system` — `MemorySystem`
- `modex_agent.memory.tags` — `CoreMemoryTag` (renamed from `KnowledgeTag` per ADR-0035)
- `modex_agent.utils.xml` — XML formatting helpers

<!-- MANUAL -->
