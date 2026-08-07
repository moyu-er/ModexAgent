<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-28 -->

# hook

## Purpose

Lifecycle extension points — lightweight observation and context injection. Hooks execute at defined `HookPoint`s and must be fast (default 10s timeout). Unlike Interceptors, hooks do NOT wrap execution — they observe and optionally modify context.

## Key Files

| File | Description |
|------|-------------|
| `abc.py` | `HookPoint` enum, `Hook` ABC (with `name` property defaulting to the concrete class name), per-point ABCs (`BeforeTurnHook`, `BeforeIterationHook`, etc.), `HookSpec`, `HookPayload`, `HookErrorPolicy`, `FinalizeContentHook` |
| `runner.py` | `HookRunner` — sequential dispatch with per-hook timeout, error policy (IGNORE/LOG/ABORT), `dispatch_finalize` for sync chain |
| `notification.py` | Hook notification utilities — notification payloads and dispatch helpers for hook lifecycle events |
| `__init__.py` | Public API re-exports: `Hook`, `HookRunner`, `HookPoint`, `HookSpec`, `HookErrorPolicy`, `HookPayload`, `FinalizeContentHook` |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `builtin/` | Built-in hook implementations — `logging.py`, `runtime_context.py`, `inbox_flush.py`, `subagent_auto_send.py`, `env_injection.py`, `loop_detection.py`, `control_drain.py` (interceptors, not hooks), `experience_review.py`. See `hook/builtin/AGENTS.md`. |

## HookPoint Dispatch

| HookPoint | Method | When | Common Use |
|-----------|--------|------|------------|
| `BEFORE_TURN` | `before_turn` | Agent.run() entry, once | Reset state, flush inbox |
| `AFTER_TURN` | `after_turn` | Agent.run() exit (all paths), once | Logging, cleanup |
| `BEFORE_ITERATION` | `before_iteration` | Each ReAct loop iteration | Dynamic tool filtering |
| `AFTER_ITERATION` | `after_iteration` | After each iteration | Restore state |
| `BEFORE_TOOL_EXECUTION` | `before_tool_execution` | Before tool batch | Policy guard |
| `AFTER_TOOL_EXECUTION` | `after_tool_execution` | After tool batch | Result transform |
| `AFTER_LLM_RESPONSE` | `after_llm_response` | After LLM response | Output guard, loop detection |
| `BEFORE_LLM` | `before_llm` | Before LLM provider call | Prompt capture, timing |
| `FINALIZE_CONTENT` | `finalize_content` | Before final output (sync) | Content formatting |
| `FINALLY_TURN` | `finally_turn` | Turn teardown (all paths) | Cleanup, cassette flush |
| `AFTER_APPROVAL` | `after_approval` | After approval decision applied | Approval timing |

## Design Rules

### Rule 1: Hooks MUST be stateless (CRITICAL)

Hooks are **per-pool** instances shared across all sessions served by that pool (see `TurnContextBuilder._hook_runner`). A single hook instance handles turns from many sessions concurrently. Any instance-level state keyed by `session_id` is a **memory leak**: sessions are created and destroyed at runtime, but the hook instance lives for the pool's lifetime, so the dict grows unbounded with no cleanup path.

**Per-turn state MUST go in `ctx.runtime.state.custom`** (typed `dict[str, Any]` on `ReActTurnState`). It is created fresh each turn, isolated per-session by construction, and automatically reclaimed when the turn's state object is rebuilt. Use a `TurnCustomKey` enum member (with `_` prefix for transient values that should never persist in snapshots).

**NEVER do this** — instance dict keyed by session_id (pool-wide leak, no cleanup):
```python
class BadHook(BeforeIterationHook):
    def __init__(self) -> None:
        self._prev_state: dict[str, Snapshot] = {}  # ❌ grows forever

    async def before_iteration(self, ctx: AgentContext) -> None:
        sid = str(ctx.session)
        prev = self._prev_state.get(sid)
        # ... compare ...
        self._prev_state[sid] = current  # ❌ never removed on session end
```

**DO this** — per-turn state via `state.custom` (auto-reclaimed, session-isolated):
```python
class GoodHook(BeforeIterationHook):
    async def before_iteration(self, ctx: AgentContext) -> None:
        state = get_react_state(ctx)
        if state is None:
            return
        prev = state.custom.get(TurnCustomKey.MY_KEY)
        # ... compare ...
        state.custom[TurnCustomKey.MY_KEY] = current  # ✅ dies with the turn
```

The only acceptable instance attributes are **immutable configuration** injected at construction time (e.g. `self._todo_store`, `self._has_archive: bool`, `self._reminder_interval: int`). These are pool-wide constants, not per-session state.

**Known limitation of per-turn state**: state is destroyed when the turn ends, so signals that need to cross turn boundaries (e.g. "cleanup happened on the last iteration of the previous turn") cannot be detected without a persistence-backed store. This is an accepted trade-off — document it in the hook's docstring rather than reintroducing instance-level state.

### Rule 2: Hook method names must match HookPoint values

`before_turn` matches `HookPoint.BEFORE_TURN`, `before_iteration` matches `HookPoint.BEFORE_ITERATION`, etc.

### Rule 3: ReAct clean mode runs without hook services

Hooks are only active in "full" mode (when `AgentRuntimeServices` is wired).

## For AI Agents

- Hooks are for **observation and context injection** — use Interceptors for execution wrapping.
- All hooks run synchronously with a 10-second timeout by default.
- `Hook.name` defaults to the concrete class name (`type(self).__name__`); override for custom diagnostics.
- The veto/result mechanism has been removed. Hooks return `None` — observation only. For execution denial, use Interceptors.
- `HookErrorPolicy` (IGNORE/LOG/ABORT) is retained per-hook via `HookSpec.on_error`.
- `FinalizeContentHook` + `dispatch_finalize` are retained for synchronous content formatting before final output.
- There are 11 hook points; a hook implementation only needs to define the methods it cares about (all are optional).
- `notification.py` provides utilities for hook lifecycle event notifications.
- **Before writing any hook, re-read Rule 1.** If you find yourself reaching for `self._something[session_id]`, stop — use `ctx.runtime.state.custom` instead.

## Dependencies

- `modex_agent.core.agent` — `AgentContext` (passed as payload to all hooks)
- `modex_agent.runtime.enums` — `TurnCustomKey` (typed keys for `state.custom`)
