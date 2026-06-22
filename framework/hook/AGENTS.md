<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# hook

## Purpose
Lifecycle extension points — lightweight observation, context injection, policy veto.
Hooks execute at 9 defined `HookPoint`s and must be fast (default 10s timeout).
Unlike Interceptors, hooks do NOT wrap execution — they observe and optionally modify context.

## Key Files
| File | Description |
|------|-------------|
| `abc.py` | `HookPoint` enum (9 points), `Hook` Protocol (all methods optional), `HookSpec[R]`, `HookPayload`, `HookResult`, `HookErrorPolicy` |
| `runner.py` | `HookRunner[R]` -- sequential dispatch with per-hook timeout, error policy (IGNORE/LOG/ABORT), veto aggregation, `dispatch_finalize` for sync chain |
| `__init__.py` | Public API: Hook, HookRunner, HookPoint, HookSpec, HookResult, HookErrorPolicy, HookPayload |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `builtin/` | hooks (logging, runtime_context, inbox_flush, subagent_auto_send) + `progress_report` (dead) + `control_drain.py` (interceptors, not hooks). See `hook/builtin/AGENTS.md`. |

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
- Hook method names must match HookPoint values (e.g. `before_turn`)
- Per-turn state MUST go in `ctx.runtime.state`, NOT instance attributes (pool mode safety)
- Instance-level state keyed by `session_id` if unavoidable
- `HookResult(veto=True)` for lightweight denial; does NOT exit the agent
- ReAct clean mode runs without hook services
- ON_CONTROL_COMMAND / progress_report are tied to the (vestigial) control plane; see ramework/control/AGENTS.md.

## Dependencies
- `framework.core.agent` -- AgentContext
