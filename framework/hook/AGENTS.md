<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# hook

## Purpose
Lifecycle extension points — lightweight observation, context injection, policy veto. Hooks execute at 9 defined `HookPoint`s and must be fast (default 10s timeout). Unlike Interceptors, hooks do NOT wrap execution — they observe and optionally modify context.

## Key Files
| File | Description |
|------|-------------|
| `abc.py` | `HookPoint` enum (9 points), `Hook` Protocol, `HookSpec`, `HookPayload`, `HookResult`, `HookErrorPolicy` |
| `runner.py` | `HookRunner` — sequential dispatch with per-hook timeout, error policy (IGNORE/LOG/ABORT), veto aggregation |
| `__init__.py` | Public API exports |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `builtin/` | Framework-provided hooks — logging, runtime-context, inbox-flush, peer-auto-send, subagent-cleanup, dynamic-tool-filter, tool-policy-guard, llm-output-guard, tool-result-transform, progress-report (see `builtin/AGENTS.md`) |

## For AI Agents

### Working In This Directory
- New hooks implement the `Hook` Protocol — all methods are optional
- Hook method names must match `HookPoint` values (e.g., `before_turn`, `after_llm_response`)
- Per-turn state MUST be stored in `ctx.metadata`, NOT instance attributes (pool mode safety)
- Instance-level state (if unavoidable) must be keyed by `session_id` (e.g., `self._state[sid]`)
- Hooks can return `HookResult(veto=True)` to veto an operation (lightweight denial)

### Hook Points
| HookPoint | When Called | Common Use |
|-----------|------------|------------|
| `before_turn` | Agent.run() entry | Reset state, flush inbox |
| `after_turn` | Agent.run() exit (all paths) | Logging, cleanup |
| `before_iteration` | Each ReAct loop iteration | Dynamic tool filtering, drain injections |
| `after_iteration` | After each iteration | Restore tool_manager |
| `before_tool_execution` | Before tool batch executes | Policy guard, filter tool list |
| `after_tool_execution` | After tool batch completes | Result transform, progress report |
| `after_llm_response` | After LLM response received | Output guard, content sanitize |
| `on_control_command` | When control command arrives | Veto control commands |
| `finalize_content` | Before sending final content | Content formatting (sync) |

### Testing Requirements
- Tests in `tests/unit/test_hooks.py`, `tests/unit/test_hook_error_policy.py`
- Test error policies: IGNORE, LOG, ABORT
- Test timeout behavior (hook exceeding `hook_timeout`)

### Common Patterns
```python
class MyHook:
    async def before_iteration(self, ctx: AgentContext) -> None:
        ctx.metadata["my_state"] = value

    async def after_iteration(self, ctx: AgentContext) -> None:
        ctx.metadata.pop("my_state", None)
```

## Dependencies

### Internal
- `framework.core.agent` — `AgentContext`

### External
- None
