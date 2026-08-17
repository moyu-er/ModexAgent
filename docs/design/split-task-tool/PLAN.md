# Implementation Plan: Split `task` Tool Out of `send_to_agent`

> Companion to `DESIGN.md`. Read that first for the full design rationale.
> This file defines the ordered, executable steps.
>
> **Historical note (2026-08-14):** `tests/integration/bot_project/test_external_pool_boot.py`
> (the pi external pool-boot suite referenced below as a verification surface) was retired —
> pi integration is no longer in use. All references to it in this plan are historical.

## Dependency Graph

```
C1 (Step 1: TaskDispatchTool + tests)     ← foundation, no deps
  │
  ├── C2 (Step 4: slim send_to_agent)     ← depends on C1
  ├── C5a (Step 8: __init__.py export)    ← depends on C1
  ├── C5b (Step 6: validator comment)     ← depends on C1
  └── C5c (Step 7: trace handoff)         ← depends on C1
       │
       ├── C3 (Step 2+3: registration)    ← depends on C1
       │    │
       │    └── C4 (Step 5: provider)     ← depends on C1 + C2
       │         │
       │         └── C6 (Step 9: docs)    ← depends on all
```

**Parallel after C1:** C2, C5a, C5b, C5c can proceed simultaneously.
**C4** depends on both C1 (TaskDispatchTool exists) and C2 (send_to_agent
slimmed, so provider content is consistent).
**C3** depends on C1 (class must exist to register).

---

## C1 — Create `TaskDispatchTool` class + unit tests

**Files:**
- `src/modex_agent/multi_agent/tools.py` — add `_TASK_PARAMS` + `TaskDispatchTool` class after `SendToAgentTool`
- `tests/unit/multi_agent/test_task_dispatch_tool.py` — NEW

**TDD: write tests FIRST, then implement.**

### Tests to write (13):

1. `test_task_tool_name_is_task` — `tool.name == "task"`
2. `test_params_have_target_agent_and_content_only` — params contain `target_agent`, `content`; no `invocation_id`
3. `test_target_agent_enum_only_subagent_targets` — `get_dynamic_schema()` enum = only `kind == SUBAGENT` names
4. `test_target_agent_enum_excludes_normal_targets` — store with NORMAL + SUBAGENT → enum has only SUBAGENT
5. `test_execute_calls_send_async_with_invocation_id_none` — calls `service.send_async(target, content, invocation_id=None, context)`
6. `test_execute_rejects_unknown_target` — unknown name → error listing available subagents
7. `test_execute_rejects_normal_target` — NORMAL target name → error "task dispatches to subagents only"
8. `test_description_contains_prompt_construction_guidance` — description mentions TASK, CONTEXT, SCOPE, OUTPUT, VERIFICATION, BOUNDARIES
9. `test_description_contains_when_not_to_use` — description mentions "When NOT to use" and at least one alternative (read, grep, glob)
10. `test_description_contains_concurrency_guidance` — description mentions "concurrently" or "multiple"
11. `test_description_lists_available_subagents` — description lists SUBAGENT targets with descriptions
12. `test_dynamic_schema_not_mutating_static_params` — `get_dynamic_schema()` does not mutate `_TASK_PARAMS`
13. `test_self_dispatch_rejected` — `target_agent == caller_name` → self-dispatch error

### Implementation:

```python
_TASK_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_agent": {
            "type": "string",
            "description": (
                "REQUIRED: exact name of the target subagent. "
                "MUST be one of the names listed under 'Available subagents:' "
                "in the tool description."
            ),
        },
        "content": {
            "type": "string",
            "description": (
                "Complete, self-contained task description. The subagent starts "
                "with a fresh context — it cannot see your conversation, "
                "reasoning, or tool results. Include: concrete objective, "
                "relevant context (file paths, constraints), scope (code or "
                "research), expected output, verification method, and boundaries."
            ),
        },
    },
    "required": ["target_agent", "content"],
}


class TaskDispatchTool(Tool):
    """Dispatch a new task to a subagent — detailed prompt construction guidance.

    Only dispatches NEW subagent tasks (invocation_id=None). For continuing
    an existing subagent session, use send_to_agent with invocation_id.
    Both tools converge on AgentCommunicationService.send_async().
    """

    def __init__(
        self,
        *,
        store: CommunicationTargetStore,
        service: AgentCommunicationService,
    ) -> None:
        self._store = store
        self._service = service
        super().__init__(
            name="task",
            parameters=_TASK_PARAMS,
            config=ToolConfig(),
        )

    @property
    def description(self) -> str:
        return self._build_description()

    def _build_description(self) -> str:
        subagent_targets = [
            t for t in self._store.list() if t.kind == AgentCommKind.SUBAGENT
        ]
        lines = [
            "Dispatch a new task to a subagent. The subagent starts with a fresh context",
            "and runs autonomously — it cannot see your conversation, reasoning, or prior",
            "tool results. Everything it needs must be in `content`.",
            "",
            "When NOT to use this tool:",
            "- If you want to read a specific file, use the read tool directly — it's faster",
            "- If you are searching for a specific pattern, use grep or glob directly",
            "- If no available subagent is a good fit for the task, do it yourself",
            "",
            "When to use:",
            "- Complex, multi-step tasks that need autonomous execution",
            "- Tasks that require a specialized subagent's tools or knowledge",
            "",
            "Usage notes:",
            "1. Launch multiple tasks concurrently when they are independent — use multiple",
            "   tool calls in a single message.",
            "2. Once you delegate work to a subagent, do not duplicate that work yourself.",
            "   Continue with non-overlapping tasks, or end your turn and wait for the result.",
            "3. The subagent's result is returned to you only — relay a concise summary to",
            "   the user if needed.",
            "4. Construct a high-quality task with:",
            "   - TASK: What exactly to do (concrete objective, not a topic)",
            "   - CONTEXT: Relevant file paths, patterns, constraints",
            "   - SCOPE: Write code or just research (search/read/analyze)",
            "   - OUTPUT: Exactly what to return in the final reply",
            "   - VERIFICATION: How to verify (e.g., test commands)",
            "   - BOUNDARIES: What NOT to do, out-of-scope items",
            "5. The subagent's output should generally be trusted.",
            "",
            "A one-line task like \"fix the bug\" is insufficient — the subagent's result",
            "quality is directly proportional to your prompt quality.",
            "",
            "After dispatching, end your turn. You'll be resumed with the result when",
            "the subagent finishes. To CONTINUE an existing session, use send_to_agent",
            "with the invocation_id from a prior task result.",
            "",
        ]
        if not subagent_targets:
            lines.append("No subagents currently available.")
            return "\n".join(lines)
        lines.append("Available subagents (use the exact name as target_agent):")
        for t in subagent_targets:
            entry = f"  - {t.name}"
            if t.description:
                entry += f": {t.description}"
            lines.append(entry)
        return "\n".join(lines)

    def get_dynamic_schema(self) -> dict[str, Any]:
        schema = super().get_dynamic_schema()
        function = dict(schema.get("function", {}))
        parameters = dict(function.get("parameters", {}))
        properties = dict(parameters.get("properties", {}))

        subagent_names = [
            t.name for t in self._store.list() if t.kind == AgentCommKind.SUBAGENT
        ]
        if subagent_names and "target_agent" in properties:
            properties["target_agent"] = {
                **properties["target_agent"],
                "enum": subagent_names,
            }

        parameters["properties"] = properties
        function["parameters"] = parameters
        return {**schema, "function": function}

    async def execute(self, **kwargs: Any) -> str:
        target_agent = str(kwargs.get("target_agent", ""))
        content = str(kwargs.get("content", ""))

        context = self._get_context()
        if context is None:
            return "Error: no agent context available"

        caller_name = context.session.agent_name
        if caller_name and target_agent == caller_name:
            return (
                f"Error: You are {caller_name!r} — you cannot dispatch a task "
                f"to yourself. Choose a different subagent."
            )

        target = self._store.get(target_agent)
        if target is None:
            available = ", ".join(
                t.name
                for t in self._store.list()
                if t.kind == AgentCommKind.SUBAGENT
            )
            return (
                f"Error: '{target_agent}' is not a valid subagent. "
                f"Available: {available}"
            )

        if target.kind != AgentCommKind.SUBAGENT:
            return (
                f"Error: '{target_agent}' is not a subagent. "
                f"The task tool dispatches to subagents only."
            )

        return await self._service.send_async(
            target=target,
            content=content,
            invocation_id=None,
            context=context,
        )

    @staticmethod
    def _get_context() -> AgentContext | None:
        from modex_agent.core.agent import current_agent_context

        return current_agent_context.get(None)
```

**Verification:** `pytest tests/unit/multi_agent/test_task_dispatch_tool.py -v`

---

## C2 — Slim `send_to_agent` description and params

**Files:**
- `src/modex_agent/multi_agent/tools.py` — `_NORMAL_PARAMS` (L69-94), `_build_normal()` (L224-258)
- `tests/unit/multi_agent/test_send_to_agent_tools.py` — update description-content tests

### Changes:

**`_NORMAL_PARAMS.content` description (L82-83):**

```python
# Before:
"description": "Complete task description with necessary context.",

# After:
"description": (
    "Message content — a continuation of an existing subagent session, "
    "a message to a peer, or a consultation. Not for dispatching new "
    "subagent tasks (use the `task` tool)."
),
```

**`_build_normal()` (L224-258):** restructure per DESIGN.md §3.2.

Add `_truncate_desc()` helper at module level:

```python
_SUBAGENT_DESC_LIMIT = 40

def _truncate_desc(desc: str, limit: int = _SUBAGENT_DESC_LIMIT) -> str:
    if len(desc) <= limit:
        return desc
    return desc[:limit].rstrip() + "..."
```

In `_build_normal()`, when listing SUBAGENT targets, apply `_truncate_desc()`
to their descriptions. NORMAL targets keep full descriptions.

### Tests to update:

- Tests asserting "Delegate a self-contained subtask" text — remove/update.
- Tests asserting `content` param description — update to new text.
- `test_description_contains_targets` — still passes (targets still listed).
- Add: `test_subagent_description_truncated_in_send_to_agent` — long subagent
  description appears truncated with `...` in `_build_normal()` output.

**Verification:** `pytest tests/unit/multi_agent/test_send_to_agent_tools.py -v`

---

## C3 — Register `TaskDispatchTool` for main agents

**Files:**
- `examples/bot_project/bot/service/pool_builder.py` — L455-469 block (registration) + L760-794 (parity helper)
- `tests/integration/bot_project/test_external_pool_boot.py` — update
- `examples/bot_project/tests/test_agent_communication.py` — update

### Registration:

Inside `if strategy.requires_main_agent_tools:` block, after
`SendToAgentTool` registration, add:

```python
tool_manager.register(
    TaskDispatchTool(store=main_store, service=main_service)
)
```

Import: add `TaskDispatchTool` to the import from
`modex_agent.multi_agent.tools`.

### Parity helper:

At L793, after `names.add("send_to_agent")`, add:
```python
names.add("task")
```

### Tests:

- `test_external_pool_boot.py`: assert `"task" in default_pi.tool_manager.list_tools()`
  (default pool has subagents) and `"task" not in pool_pi.tool_manager.list_tools()`
  (external pool).
- `test_agent_communication.py`: add `TaskDispatchTool` assertion.
- `examples/bot_project/tests/unit/service/test_main_agent_tools.py`:
  `assert "task" in names`.

**Verification:** `pytest tests/integration/bot_project/test_external_pool_boot.py -v -m integration`

---

## C4 — Modify `_SubagentDispatchSubProvider`

**Files:**
- `src/modex_agent/memory/prompt_pipeline/providers.py` — L193-250
- `tests/unit/memory/prompt_pipeline/test_providers.py` — update fixtures

### Changes:

**`_subagent_target_names()` (L211-223):**

```python
def _subagent_target_names(self) -> list[str]:
    if self._tool_manager is None:
        return []
    tool = self._tool_manager.get_tool("task")
    if tool is None:
        return []
    from modex_agent.multi_agent.tools import TaskDispatchTool

    if not isinstance(tool, TaskDispatchTool):
        return []
    return sorted(
        t.name
        for t in tool.list_targets()
        if t.kind == AgentCommKind.SUBAGENT
    )
```

**`applies()` (L225-228):**

```python
def applies(self) -> bool:
    if self._comm_kind == AgentCommKind.SUBAGENT:
        return False
    if self._tool_manager is None:
        return False
    return self._tool_manager.get_tool("task") is not None
```

**`content()` (L234-250):** per DESIGN.md §4.1.

### Tests:

- Update `_make_tool_manager` helper to register `TaskDispatchTool`.
- `test_comm_provider_subagent_target_emits_dispatch_contract`: assert
  `"task" in result` and `"Dispatching Subagents" in result`.
- `test_comm_provider_no_send_to_agent_tool_emits_nothing`: ensure dispatch
  sub-provider emits nothing when `task` is not registered.
- Add: `test_dispatch_provider_does_not_fire_without_task_tool`.

**Verification:** `pytest tests/unit/memory/prompt_pipeline/test_providers.py -v`

---

## C5 — Supporting infrastructure (parallel: a + b + c)

### C5a — Package exports

**File:** `src/modex_agent/multi_agent/__init__.py`

```python
# L19:
from modex_agent.multi_agent.tools import SendToAgentTool, TaskDispatchTool
# L34 (__all__):
"TaskDispatchTool",
```

### C5b — Validator documentation comment

**File:** `src/modex_agent/multi_agent/subagent_validator.py` (L33-37)

Add comment documenting structural exclusion. No code change.

### C5c — Trace handoff exclusion

**File:** `src/modex_agent/trace/hooks.py` (L596-610)

```python
# Before:
if tc.tool_name != "send_to_agent":

# After:
_DISPATCH_TOOL_NAMES = frozenset({"send_to_agent", "task"})
if tc.tool_name not in _DISPATCH_TOOL_NAMES:
```

**Tests:**
- `tests/unit/trace/test_hooks.py`: add `test_task_tool_excluded_from_handoff_span`.
- `tests/unit/trace/test_agent_handoff_span.py`: add `tool_name="task"` variant.

**Verification:** `pytest tests/unit/trace/ -v`

---

## C6 — AGENTS.md documentation update

**Files (doc-only):**

1. `AGENTS.md` (root) — "Single LLM-facing tool: `send_to_agent`" →
   "Two LLM-facing tools: `task` (dispatch new subagent tasks) +
   `send_to_agent` (continuation, consultation, peer communication). Both
   converge on `AgentCommunicationService.send_async()`."

2. `src/modex_agent/multi_agent/AGENTS.md`:
   - Tool table: add `TaskDispatchTool` row.
   - "Single LLM-facing comm tool" → "Two LLM-facing tools".
   - Communication Contract section: update tool descriptions.

3. `src/modex_agent/memory/prompt_pipeline/AGENTS.md`:
   - Provider table: `_SubagentDispatchSubProvider` fires on `task` tool
     existence (not `send_to_agent`).

4. `examples/bot_project/AGENTS.md`:
   - "Communication: `send_to_agent`" → "`send_to_agent` + `task`".

---

## Verification Checklist (run after all commits)

```bash
# Unit tests
pytest tests/unit/multi_agent/test_task_dispatch_tool.py -v
pytest tests/unit/multi_agent/test_send_to_agent_tools.py -v
pytest tests/unit/memory/prompt_pipeline/test_providers.py -v
pytest tests/unit/trace/ -v

# Integration tests
pytest tests/integration/bot_project/test_external_pool_boot.py -v -m integration

# Lint + type check
ruff check src/modex_agent/multi_agent/ tests/unit/multi_agent/
ruff format --check src/modex_agent/multi_agent/ tests/unit/multi_agent/
mypy src/modex_agent/multi_agent/

# Architecture guard
pytest tests/architecture/ -v
```

## Commit Strategy

| Commit | Steps | Message |
|--------|-------|---------|
| C1 | Step 1 | `feat(multi_agent): add TaskDispatchTool for subagent task dispatch` |
| C2 | Step 4 | `refactor(multi_agent): slim send_to_agent to communication-only` |
| C3 | Step 2+3 | `feat(bot_project): register TaskDispatchTool for main agents` |
| C4 | Step 5 | `refactor(prompt_pipeline): _SubagentDispatchSubProvider references task tool` |
| C5 | Step 6+7+8 | `feat: wire TaskDispatchTool into trace, exports, validator docs` |
| C6 | Step 9 | `docs: update AGENTS.md for two-tool dispatch architecture` |
