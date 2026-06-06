# Dynamic Communication Tool Description Design

**Date:** 2026-06-06
**Status:** Approved
**Scope:** Communication tool description generation, target management, ListCommunicationTargetsTool deprecation

---

## Problem

The communication tool (`SendToAgentTool`) uses static or minimally dynamic descriptions
that fail to inform the LLM about available targets. The current `build_targets_description()`
only shows `{name} ({comm_kind})` — no agent descriptions, no template info. The LLM must
call a separate `ListCommunicationTargetsTool` to discover targets, but frequently doesn't.

Additionally:
- Every ReAct iteration re-queries the registry (no caching).
- Subagent descriptions are completely static fixed strings.
- Agent descriptions from YAML config (`AgentTemplate.description`,
  `AgentDescriptor.role_description`) are never surfaced in tool descriptions.

## Design Decisions

### D1: CommunicationTargetStore — encapsulated target management

A `CommunicationTargetStore` class holds the mutable target list and cached description
string. External code can only:

- `add(target)` — add a target (no-op if name already exists)
- `pop_by_name(name)` — remove by name (no-op if not found)
- `list()` — returns a **copy** of the target list
- `has(name)` — check existence

The store holds `_send_description: str | None`. After every `add` / `pop_by_name`,
`_refresh()` regenerates the description from current targets and caches it.
First access via `send_description` property triggers lazy generation if cache is `None`.

The `_for_subagent` flag controls which description format to generate (normal vs subagent).

**Key invariant:** no add/pop → no refresh. The description is stable until the target
list actually changes.

### D2: CommunicationTargetsProvider ABC

An ABC between `Tool` and `SendToAgentTool` that delegates to a shared
`CommunicationTargetStore`. Exposes `add_target()`, `pop_target_by_name()`,
`list_targets()`, `has_target()` as thin wrappers.

Currently only `SendToAgentTool` inherits this ABC. Future tools (e.g., a reintroduced
list tool) can also inherit it and share the same store.

### D3: Description content — all target info in send tool

The `send_to_agent` tool description is the **single source of truth** for the LLM.
It contains:

- Base instructions (dispatch, async behavior)
- **Full target list** with name, kind (normal/subagent), and description
- invocation_id usage guide (simplified: only matters for subagent targets)
- Behavioral notes

For subagents, the description is minimal (single parent target, simplified instructions).

### D4: Deprecate ListCommunicationTargetsTool

`ListCommunicationTargetsTool` is **completely deprecated**:

- Class definition preserved in codebase (not deleted, not modified)
- All registration points removed (`pool_builder.py`, `_build_subagent_tool_manager`)
- All references in bot_project agent prompts and docs removed
- The send tool description now contains everything the list tool provided

If a list/discovery tool is needed in the future, it will be reimplemented from scratch
(potentially with a different design), not resurrected from the current class.

### D5: Target population strategy is external

The `CommunicationTargetStore` is a pure data container — it doesn't know **which**
targets to add. The population strategy lives in `pool_builder.py`:

- **Main agent:** all registered pool agents (minus self) + all templates from YAML
- **Subagent:** parent agent only (determined dynamically at creation time in
  `_build_subagent_tool_manager()`)

To change the strategy, modify only `pool_builder.py` or the subagent tool builder.
The store and tools remain unchanged.

### D6: Runtime target lifecycle

| Trigger | Action | Caller |
|---|---|---|
| Bot init | `store.add()` all targets | `pool_builder.py` |
| Dynamic subagent created | `store.add()` new agent | `_create_dynamic_subagent()` |
| Dynamic subagent evicted/shutdown | `store.pop_by_name()` | pool eviction / shutdown |

`AgentCommunicationService` holds a reference to the main agent's `CommunicationTargetStore`
(via new `target_store` constructor parameter) for runtime add/pop.

---

## Data Model

```python
@dataclass(frozen=True)
class CommunicationTarget:
    """A single communicable agent."""
    name: str
    kind: AgentCommKind       # NORMAL or SUBAGENT
    description: str = ""     # From YAML description or role_description
```

No `is_template` field — whether a target is a template or a registered agent is an
internal routing detail handled by `_resolve_target()` / `_create_dynamic_subagent()`.
The LLM only needs to know "I can send to this name, and here's what it does."

---

## Class Hierarchy

```
Tool (framework/core/tool_manager.py)
  └── CommunicationTargetsProvider (ABC, framework/multi_agent/tools.py)
        └── SendToAgentTool (framework/multi_agent/tools.py)

CommunicationTargetStore (framework/multi_agent/tools.py) — standalone, not a Tool
CommunicationTarget (dataclass, framework/multi_agent/tools.py)
```

---

## Description Templates

### Normal agent (send_to_agent)

```
Dispatch a task to another agent. Results arrive via inbox asynchronously.

Available targets:
  - coding (normal): Full-featured coding expert
  - scout (subagent): Fast codebase recon — returns compressed context for handoff
  - worker (subagent): Implementation with terminal — the single writer thread
  - planner (subagent): Creates implementation plans, returns plan.md
  - reviewer (subagent): Code review with 5 review types
  - context-builder (subagent): Deep requirements analysis
  - delegate (subagent): Lightweight catch-all for simple tasks

Usage:
  target_agent: Name from the list above.
  content: Complete task description with context.
  invocation_id: Only for subagent targets — omit or null for new task,
    pass previous invocation_id to continue. Ignored for normal agents.

Important: Does NOT wait for result. Results arrive asynchronously.
```

### Subagent (send_to_agent)

```
Send a message to your parent agent for coordination.

Available target:
  - main (normal): AI assistant

Usage:
  target_agent: "main"
  content: "NEED_DECISION: <question>" for blocking decisions,
    "PROGRESS_UPDATE: <info>" for non-blocking updates.
  invocation_id: Not used (ignored).

Important: You can ONLY message your parent.
```

Description content is generated from the store's target list at cache time.
No static per-tool strings — the store builds everything from data.

---

## Execution: target permission check

`SendToAgentTool.execute()` validates the target against the store before dispatch:

```python
async def execute(self, **kwargs):
    target_agent = str(kwargs.get("target_agent", ""))
    if not self.has_target(target_agent):
        available = ", ".join(t.name for t in self.list_targets())
        return f"Error: '{target_agent}' is not a valid communication target. Available: {available}"
    # ... existing execution logic
```

---

## Files Changed

| File | Change |
|---|---|
| `framework/multi_agent/tools.py` | Add `CommunicationTarget`, `CommunicationTargetStore`, `CommunicationTargetsProvider` ABC; rewrite `SendToAgentTool`; keep `ListCommunicationTargetsTool` class but it is no longer registered anywhere |
| `framework/multi_agent/communication.py` | Add `target_store` param to `__init__`; call `self._target_store.add()` at end of `_create_dynamic_subagent()`; in `_build_subagent_tool_manager()` create subagent-specific store and only register `SendToAgentTool` |
| `examples/bot_project/bot/service/pool_builder.py` | Create `CommunicationTargetStore`, populate from pool agents + templates, inject into `SendToAgentTool` and `AgentCommunicationService`; stop registering `ListCommunicationTargetsTool` |
| `examples/bot_project/agents/main.md` | Remove `list_communication_targets` references from multi-agent communication rules |
| `examples/bot_project/agents/coding.md` | Same |
| `examples/bot_project/agents/scout.md` | Remove "First, call list_communication_targets" instruction |
| `examples/bot_project/agents/worker.md` | Same |
| `examples/bot_project/agents/planner.md` | Same |
| `examples/bot_project/agents/reviewer.md` | Same |
| `examples/bot_project/agents/oracle.md` | Same |
| `examples/bot_project/agents/context-builder.md` | Same |
| `examples/bot_project/agents/delegate.md` | Same |
| `examples/bot_project/README.md` | Remove `list_communication_targets` documentation |
| `examples/bot_project/README.zh-CN.md` | Same |
| `examples/bot_project/AGENTS.md` | Same |
| `examples/bot_project/config/pools/coding.yml` | Update comment |

### Not Changed

- `AgentDescriptor`, `AgentProfile`, `AgentTemplate` — structure unchanged
- `_validate_invocation_id()` — existing logic preserved
- `ListCommunicationTargetsTool` class — code preserved, just unused
- `AgentRegistry`, `AgentPool` — no changes to core pool/registry logic
- `build_targets_description()` on `AgentCommunicationService` — removed (dead code cleanup)

---

## Key Invariants

1. **No add/pop → no refresh.** The description cache persists until the target list changes.
2. **Subagent description is determined at creation time.** The subagent's store has exactly one
   target (parent). Since no further add/pop occurs, the description never changes.
3. **External code cannot mutate the internal list.** Only `add()` / `pop_by_name()` / `list()`
   (returns copy) are exposed.
4. **Population strategy is in pool_builder.** The store is a passive container.
   To change who can communicate with whom, change the builder.
