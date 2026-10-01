<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-30 | W2 domain-ownership moves -->

# builtin hooks

## Purpose
Generic framework hooks that are not owned by a specific domain. After the
W2 domain-ownership wave, domain-specific builtin hooks moved to their
domains — only the domain-neutral hooks remain here:

- ReAct turn-lifecycle hooks (`checkpoint`, `deliver_retry`,
  `todo_continuation`, `todo_planning_nudge`, `loop_detection`,
  `length_guard`, `knowledge_hook`, `env_injection`) →
  `modex_agent/agents/react/hooks/`
- `subagent_auto_send` → the `subagents` capability package
  (`modex_agent/plugins/defaults/capabilities/subagents/auto_send.py`)
- `inbox_flush` → `modex_agent/multi_agent/inbox/flush_hook.py`
- `training_data` → `modex_agent/trace/training_data_hook.py`
- Session-cleanup re-orientation lives in `memory/cleanup_hooks.py`
  (`TodoReorientationHook`, a `MemoryHook` — not a ReAct `HookRunner` hook).
- `RuntimeContextHook` moved to `runtime/hooks.py` (plan §15 B2).
  `experience_review.py` moved to the `experience` capability package
  (plan §10).
- Also hosts `control_drain.py`, which despite living under
  `hook/builtin/` actually defines *interceptors* (not hooks) that consume
  the control channel — see the separate table below.

## Hooks
| File | Class | ABC(s) | HookPoint(s) | Description |
|------|-------|--------|--------------|-------------|
| `logging.py` | `RunLoggingHook` | `AfterLLMResponseHook`, `BeforeToolExecutionHook`, `AfterToolExecutionHook` | after_llm_response, before/after_tool_execution | Basic execution logging |
| `current_time.py` | `CurrentTimeInjectionHook` | `StartNodeTurnHook` | start_node_turn | Injects second-precision current time (with timezone and weekday) as a system-reminder at fresh-turn start |

## Continuation Gate (AfterTurnNode)

`AfterTurnNode` consumes two one-shot flags set by AfterTurnHook continuation
sources (now in `agents/react/hooks/`) and routes to `BEFORE` (continuation)
or `END` (terminal):

- **`CONTINUATION_REQUEST`** — any hook wants another turn attempt.
- **`CONTINUATION_RENEW_MAX_TURNS`** — a hook authorizes extending `MAX_TURNS`
  past the current upper bound (watchdog renewal). Currently
  `TodoContinuationHook` and `LengthGuardHook` set this. The gate increments
  `MAX_TURNS` by 1 only once regardless of how many hooks set it.

Default `MAX_TURNS` is 3 (set in `TurnContextBuilder.build_runtime_and_context`).
Hooks act independently — each checks its own trigger condition, injects its own
reminder, and sets flags without consulting other hooks. Multiple hooks setting
the same flag is harmless (dict assignment). The gate pops both flags on every
path, ensuring clean state for the next turn attempt.

## Non-Hook Files In This Directory
`control_drain.py` defines **interceptors**, not hooks, and is the single shared
cancel-drain utility:

| File | Defines | Kind | Status |
|------|---------|------|--------|
| `control_drain.py` | `drain_control_channel()` (shared fn), `ControlDrainInterceptor` (TOOL_CALL), `LlmCancelInterceptor` (LLM_STREAM) | interceptor + helper | drains an always-empty queue (see `modex_agent/control/AGENTS.md`) |

`drain_control_channel()` is also called directly from `ReActAgent`,
`LLMNode`, and `ToolNode._execute_batch` at safe points.

## Design Rules
- One hook class per file
- Each hook inherits from per-point ABCs (`BeforeGraphHook`, `StartNodeTurnHook`, `BeforeTurnHook`, `AfterTurnHook`, `AfterLLMResponseHook`, etc.) via multiple inheritance
- **Hooks MUST be stateless** — see `hook/AGENTS.md` Rule 1. Per-turn state goes in `ctx.runtime.state.custom` via `TurnCustomKey`; the ONLY acceptable instance attributes are immutable configuration injected at construction. Never use `self._state[session_id]` dicts — they leak across the pool's lifetime.
- Register via `HookRunner.add(HookSpec(hook=MyHook(), on_error=...))`

## Dependencies
- `modex_agent.core` -- AgentContext
- `modex_agent.control` -- ControlChannel, ControlCommandType, ControlScope
- `modex_agent.interceptor.abc` -- ToolCallInterceptor, LlmStreamInterceptor (control_drain.py only)
- `modex_agent.hook.abc` -- Hook base ABC + per-point ABC hierarchy
- `modex_agent.core.turn.enums` -- `TurnCustomKey` (typed keys for per-turn `state.custom`)
