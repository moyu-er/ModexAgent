<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# hook

## Purpose

Lifecycle extension points — lightweight observation, context injection, policy veto. Hooks execute at 9 defined `HookPoint`s and must be fast (default 10s timeout). Unlike Interceptors, hooks do NOT wrap execution — they observe and optionally modify context.

## Key Files

| File | Description |
|------|-------------|
| `abc.py` | `HookPoint` enum (9 points), `Hook` Protocol (all methods optional), `HookSpec[R]`, `HookPayload`, `HookResult`, `HookErrorPolicy` |
| `runner.py` | `HookRunner[R]` — sequential dispatch with per-hook timeout, error policy (IGNORE/LOG/ABORT), veto aggregation, `dispatch_finalize` for sync chain |
| `notification.py` | Hook notification utilities — notification payloads and dispatch helpers for hook lifecycle events |
| `__init__.py` | Public API re-exports: `Hook`, `HookRunner`, `HookPoint`, `HookSpec`, `HookResult`, `HookErrorPolicy`, `HookPayload` |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `builtin/` | Built-in hook implementations — `logging.py`, `runtime_context.py`, `inbox_flush.py`, `subagent_auto_send.py`, `control_drain.py` (interceptors, not hooks), `experience_review.py`, `progress_report.py` (dead). See `hook/builtin/AGENTS.md`. |

## HookPoint Dispatch

| HookPoint | Method | When | Common Use |
|-----------|--------|------|------------|
| `BEFORE_TURN` | `before_turn` | Agent.run() entry, once | Reset state, flush inbox |
| `AFTER_TURN` | `after_turn` | Agent.run() exit (all paths), once | Logging, cleanup |
| `BEFORE_ITERATION` | `before_iteration` | Each ReAct loop iteration | Dynamic tool filtering |
| `AFTER_ITERATION` | `after_iteration` | After each iteration | Restore state |
| `BEFORE_TOOL_EXECUTION` | `before_tool_execution` | Before tool batch | Policy guard |
| `AFTER_TOOL_EXECUTION` | `after_tool_execution` | After tool batch | Result transform |
| `AFTER_LLM_RESPONSE` | `after_llm_response` | After LLM response | Output guard |
| `ON_CONTROL_COMMAND` | `on_control_command` | Control command arrives | Veto commands |
| `FINALIZE_CONTENT` | `finalize_content` | Before final output (sync) | Content formatting |

## Design Rules

- Hook method names must match HookPoint values (e.g. `before_turn` matches `HookPoint.BEFORE_TURN`)
- Per-turn state MUST go in `ctx.runtime.state`, NOT instance attributes (pool mode safety)
- Instance-level state keyed by `session_id` if unavoidable
- `HookResult(veto=True)` for lightweight denial; does NOT exit the agent
- ReAct clean mode runs without hook services
- `ON_CONTROL_COMMAND` / `progress_report` are tied to the (vestigial) control plane; see `framework/control/AGENTS.md`

## For AI Agents

- Hooks are for **observation and lightweight policy** — use Interceptors for execution wrapping.
- All hooks run synchronously with a 10-second timeout by default.
- Use `HookResult(veto=True, message="...")` to reject an action without raising an exception.
- There are 9 hook points; a hook implementation only needs to define the methods it cares about (all are optional).
- `notification.py` provides utilities for hook lifecycle event notifications.
- The `FINALIZE_CONTENT` hook point is synchronous (runs in `dispatch_finalize`) and is intended for content formatting before final output.

## Dependencies

- `framework.core.agent` — `AgentContext` (passed as payload to all hooks)
