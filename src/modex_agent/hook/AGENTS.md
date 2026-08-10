<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-10 -->

# hook

## Purpose

Lifecycle extension points — lightweight observation and context injection. Hooks execute at defined `HookPoint`s and must be fast (default 10s timeout). Unlike Interceptors, hooks do NOT wrap execution — they observe and optionally modify context.

## Key Files

| File | Description |
|------|-------------|
| `abc.py` | `HookPoint` enum (15 values), `Hook` ABC (with `name` property defaulting to the concrete class name), per-point ABCs organized by 4-level hierarchy (`BeforeGraphHook`/`AfterGraphHook`/`FinallyGraphHook` graph-level, `StartNodeTurnHook`/`EndNodeTurnHook` node-level, `BeforeTurnHook`/`AfterTurnHook` turn-attempt, `BeforeIterationHook`/`AfterIterationHook`/`BeforeToolExecutionHook`/`AfterToolExecutionHook`/`AfterLLMResponseHook`/`BeforeLLMHook`/`AfterApprovalHook` iteration-level, `FinalizeContentHook`), `HookSpec`, `HookPayload`, `HookErrorPolicy` |
| `runner.py` | `HookRunner` — sequential dispatch with per-hook timeout, error policy (IGNORE/LOG/ABORT), `dispatch_finalize` for sync chain |
| `notification.py` | Hook notification utilities — notification payloads and dispatch helpers for hook lifecycle events |
| `__init__.py` | Public API re-exports: `Hook`, `HookRunner`, `HookPoint`, `HookSpec`, `HookErrorPolicy`, `HookPayload`, `FinalizeContentHook` |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `builtin/` | Built-in hook implementations — `logging.py`, `runtime_context.py`, `inbox_flush.py`, `subagent_auto_send.py`, `env_injection.py`, `loop_detection.py`, `deliver_retry.py` (`DeliverRetryHook` — `AfterTurnHook`, sets `CONTINUATION_REQUEST` when the agent stops without delivering; covers both normal stop and max-iteration exits), `current_time.py` (`CurrentTimeInjectionHook` — `StartNodeTurnHook`), `todo_continuation.py` (`TodoContinuationHook` — `AfterTurnHook`), `control_drain.py` (interceptors, not hooks), `experience_review.py`. See `hook/builtin/AGENTS.md`. |

## Four-Level Hook Hierarchy

Hooks fire at four levels, each with a distinct lifetime boundary. The level determines where the hook is dispatched and how often it fires:

| Level | HookPoints | Dispatch Site | Fires On |
|-------|-----------|---------------|----------|
| **Graph** | `BEFORE_GRAPH`, `AFTER_GRAPH`, `FINALLY_GRAPH` | `ReActAgent.run()` / `actual_turn()` via `hook_runner.dispatch()` | Once per `actual_turn()` call (re-fires on approval resume) |
| **Node** | `START_NODE_TURN`, `END_NODE_TURN` | `StartNode` / `EndNode` via `ctx.runtime.dispatch_hook()` | Fresh-turn start / terminal end (does NOT re-fire on approval resume) |
| **TurnAttempt** | `BEFORE_TURN`, `AFTER_TURN` | `BeforeTurnNode` / `AfterTurnNode` via `ctx.runtime.dispatch_hook()` | Each turn attempt within a graph execution |
| **Iteration** | `BEFORE_ITERATION`, `AFTER_ITERATION`, `BEFORE_LLM`, `AFTER_LLM_RESPONSE`, `BEFORE_TOOL_EXECUTION`, `AFTER_TOOL_EXECUTION`, `FINALIZE_CONTENT`, `AFTER_APPROVAL` | `LLMNode` / `ToolNode` via `ctx.runtime.dispatch_hook()` | Each ReAct loop iteration or specific sub-operation |

### Graph-Level Constraint

Graph-level hooks (`BeforeGraphHook` / `AfterGraphHook` / `FinallyGraphHook`) fire once per `actual_turn()` call. Approval resume re-enters `actual_turn()`, causing these hooks to fire again. Avoid mutating `ctx.history` from graph-level hooks — use `StartNodeTurnHook` or `BeforeTurnHook` instead, which fire only on fresh turns (not on resume).

### Dispatch Flow Diagram

```
                        ┌─────────────────────────────────────────────────┐
                        │           actual_turn()  (每 user turn)          │
                        │                                                 │
                        │  ① BEFORE_GRAPH  ← engine.run_async() 之前      │
                        │     │  (审批恢复也触发 ⚠️ 避免操作 ctx.history)   │
                        │     ▼                                           │
                        │  engine.run_async(ctx)                          │
                        │     │                                           │
                        │     ▼                                           │
                        │  ┌─StartNode──────────────────────────────┐     │
                        │  │  审批恢复? → deliver(TOOL) → return     │     │
                        │  │  (跳过所有 hook)                        │     │
                        │  │                                         │     │
                        │  │  Fresh turn:                            │     │
                        │  │    ② START_NODE_TURN                    │     │
                        │  │    (只在 fresh turn 触发,恢复不触发)      │     │
                        │  │    → deliver(BEFORE)                    │     │
                        │  └─────────────┬───────────────────────────┘     │
                        │                │                                 │
                        │     ┌──────────▼──────────────┐                  │
                        │     │  BEFORE ↔ LLM ↔ TOOL 循环 (每 turn attempt)│
                        │     │                         │                  │
                        │     │  ┌─BeforeTurnNode──┐    │                  │
                        │     │  │ turn_attempt++  │    │                  │
                        │     │  │ ③ BEFORE_TURN   │    │                  │
                        │     │  │ → deliver(LLM)  │    │                  │
                        │     │  └──────┬──────────┘    │                  │
                        │     │         ▼               │                  │
                        │     │  ┌─LLMNode──────────┐   │                  │
                        │     │  │ BEFORE_ITERATION │   │                  │
                        │     │  │ BEFORE_LLM       │   │ (iteration 级)   │
                        │     │  │ [LLM 调用]        │   │                  │
                        │     │  │ AFTER_LLM_RESPONSE│  │                  │
                        │     │  │ tool_calls? ──────┼───┘                  │
                        │     │  │    YES → TOOL     │                      │
                        │     │  │    NO  → AFTER    │                      │
                        │     │  │ AFTER_ITERATION  │                      │
                        │     │  └──────────────────┘                      │
                        │     │  ┌─ToolNode─────────────┐                  │
                        │     │  │ BEFORE_TOOL_EXECUTION│ (iteration 级)   │
                        │     │  │ [执行工具]            │                  │
                        │     │  │ AFTER_TOOL_EXECUTION │ (iteration 级)   │
                        │     │  │ → LLM                │                  │
                        │     │  └──────────────────────┘                  │
                        │     └──────────────┬──────────────────────────────┘
                        │                    │                             │
                        │     ┌──────────────▼───────────────────┐         │
                        │     │   AfterTurnNode                  │         │
                        │     │   state.result = result          │         │
                        │     │   ④ AFTER_TURN                   │         │
                        │     │   (带 {"result": result} 数据)    │         │
                        │     │   续作门控:                       │         │
                        │     │     CONTINUATION_REQUEST?        │         │
                        │     │       YES → deliver(BEFORE) ↖ 循环│         │
                        │     │       NO  → deliver(END)         │         │
                        │     └──────────────┬───────────────────┘         │
                        │                    │                             │
                        │     ┌──────────────▼───────────────────┐         │
                        │     │   EndNode                        │         │
                        │     │   ⑤ END_NODE_TURN                │         │
                        │     │   → deliver(GraphNode.END)       │         │
                        │     └──────────────────────────────────┘         │
                        │                                                 │
                        │  ⑥ AFTER_GRAPH   ← engine.run_async() 之后      │
                        │  ⑦ FINALLY_GRAPH ← finally 块 (所有路径)         │
                        │     │  (审批恢复也触发 ⚠️)                        │
                        └─────────────────────────────────────────────────┘
```

### Trigger Matrix

| 事件 | ①BEFORE_GRAPH | ②START_NODE_TURN | ③BEFORE_TURN | ④AFTER_TURN | ⑤END_NODE_TURN | ⑥AFTER_GRAPH | ⑦FINALLY_GRAPH |
|---|---|---|---|---|---|---|---|
| **Fresh turn** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **续作 (AFTER→BEFORE)** | — | — | ✓ | ✓ | ✓ | — | — |
| **审批恢复** | ✓ | ✗ | ✗ | ✓ | ✓ | ✓ | ✓ |

- **Fresh turn**: 全新 user message，graph 从 START 正常走到 END。
- **续作**: `AfterTurnNode` 的续作门控检测到 `CONTINUATION_REQUEST`，路由回 BEFORE 开始新一轮 turn attempt。Graph 级 hook 不重复触发，turn-attempt 级和 node 级的 END 重新触发。
- **审批恢复**: `actual_turn()` 被重新调用（re-entry），BEFORE_GRAPH/AFTER_GRAPH/FINALLY_GRAPH 重复触发。StartNode 检测到 `resume_target` 后直接路由到 TOOL，跳过 START_NODE_TURN。Graph 拓扑天然路由 TOOL→LLM→AFTER，④AFTER_TURN 会触发——这是正确行为，AFTER_TURN 上的 hook（DeliverRetryHook/TodoContinuationHook）都是有状态检查的，触发安全。

### Hook Inventory

#### Built-in Hooks (13 implemented + 1 reserved)

| # | Hook | ABC(s) | HookPoint(s) | Description |
|---|------|--------|--------------|-------------|
| 1 | `NativeEnvInjectionHook` | `BeforeGraphHook` | ① before_graph | Populates `MODEX_*` env contextvars for native agent subprocess tools (idempotent, safe on resume) |
| 2 | `RuntimeContextHook` | `StartNodeTurnHook` + `BeforeToolExecutionHook` + `AfterToolExecutionHook` | ② start_node_turn + before/after_tool_execution | Clears per-turn RuntimeContext at fresh-turn start; tracks tool calls per session (moved from graph-level to avoid context wipe on resume) |
| 3 | `InboxFlushHook` | `StartNodeTurnHook` + `BeforeIterationHook` | ② start_node_turn + before_iteration | Flushes inbox messages to `ctx.history` at fresh-turn start (moved from graph-level to avoid duplicate flush on resume) |
| 4 | `CurrentTimeInjectionHook` ⭐ | `StartNodeTurnHook` | ② start_node_turn | Injects second-precision current time (IANA timezone name + weekday) as system-reminder at fresh-turn start; replaces the hour-precision `RuntimeProvider` time line |
| 5 | `ModelChoiceBindHook` | `StartNodeTurnHook` | ② start_node_turn | Binds per-turn model selection (contextvar + model_info override); moved from graph-level to avoid re-bind on resume |
| 6 | `TraceCollectorHook` | `BeforeGraphHook` + `BeforeLLMHook` + `AfterLLMResponseHook` + `BeforeToolExecutionHook` + `AfterToolExecutionHook` + `AfterApprovalHook` + `BeforeIterationHook` + `AfterIterationHook` + `FinallyGraphHook` | ① before_graph + ⑦ finally_graph + 6 iteration points | Creates root span at turn start, finalizes at turn end; collects OTel/Langfuse spans at every lifecycle point (9 ABCs — the most multi-inherited hook) |
| 7 | `DeliverRetryHook` | `AfterTurnHook` | ④ after_turn | Requests graph-internal continuation when agent stops without calling `deliver`; sets `CONTINUATION_REQUEST` + injects deliver-reminder system-reminder. Moved from `AfterLLMResponseHook` to `AfterTurnHook` to cover the max-iteration blind spot (old timing missed exits where LLMNode returned before dispatching AFTER_LLM_RESPONSE) |
| 8 | `TodoContinuationHook` ⭐ | `AfterTurnHook` | ④ after_turn | Requests continuation when active todo tasks remain after a turn attempt. Anti-deadlock: caches sha256 hash of active todo `content:status` in `state.custom[LAST_CONTINUATION_TODO_SIG]`; skips if unchanged since last continuation (agent made no progress). Coordinates with DeliverRetryHook: if DeliverRetry already set `CONTINUATION_REQUEST`, TodoContinuation skips |
| 9 | `ExperienceReviewHook` | `AfterGraphHook` | ⑥ after_graph | Spawns background conversation-review agent after graph execution; main agent only |
| 10 | `SubagentAutoSendHook` | `FinallyGraphHook` | ⑦ finally_graph | On subagent turn completion, writes numbered OUTPUT\_\<n\>.md deliverable and notifies parent via bus |
| 11 | `TurnOutcomeNotifyHook` | `FinallyGraphHook` | ⑦ finally_graph | Sends user-facing notification on max_iterations/error turn outcomes |
| 12 | `TrainingDataHook` | `FinallyGraphHook` | ⑦ finally_graph | Records training data at graph teardown |
| 13 | `CassetteFlushHook` | `FinallyGraphHook` | ⑦ finally_graph | Saves cassette recording at graph teardown |
| 14 | `CheckpointHook` | `AfterIterationHook` | after_iteration | Captures per-iteration checkpoint snapshots |
| — | `EndNodeTurnHook` | (reserved) | ⑤ end_node_turn | ABC + dispatch entry exist for future extensibility; no concrete hook inherits it yet (by design) |

⭐ = newly added by hook-architecture-rebuild.

#### Hook Coordination

Hooks at the same HookPoint fire in **registration order** (the order they appear in `shared_hooks` / `pool_wiring`). Two hooks on ④ AFTER_TURN coordinate via `CONTINUATION_REQUEST`:

```
shared_hooks = [
    CurrentTimeInjectionHook(),   # ② START_NODE_TURN — injects time first
    DeliverRetryHook(),            # ④ AFTER_TURN — checks deliver, sets flag + reminder
    TodoContinuationHook(),        # ④ AFTER_TURN — checks flag (skip if DeliverRetry set it)
    *_collect_run_hooks(...),
]
```

If `DeliverRetryHook` sets `CONTINUATION_REQUEST` (agent stopped without deliver), `TodoContinuationHook` sees the flag already set and skips — no double reminder injection.

## HookPoint Dispatch

| HookPoint | Method | Level | When | Common Use |
|-----------|--------|-------|------|------------|
| `BEFORE_GRAPH` | `before_graph` | Graph | `actual_turn()` entry, once per call | Env injection, model binding |
| `AFTER_GRAPH` | `after_graph` | Graph | `actual_turn()` exit (all paths) | Experience review, post-turn logging |
| `FINALLY_GRAPH` | `finally_graph` | Graph | `actual_turn()` teardown (all paths) | Subagent delivery, cassette flush, training data |
| `START_NODE_TURN` | `start_node_turn` | Node | `StartNode` entry (fresh turns only) | Current-time injection, per-turn model binding, inbox flush, runtime context tracking |
| `END_NODE_TURN` | `end_node_turn` | Node | `EndNode` exit (terminal only) | Post-turn observation |
| `BEFORE_TURN` | `before_turn` | TurnAttempt | `BeforeTurnNode` entry, per attempt | Turn-attempt initialization |
| `AFTER_TURN` | `after_turn` | TurnAttempt | `AfterTurnNode` exit, per attempt | Deliver retry, todo continuation |
| `BEFORE_ITERATION` | `before_iteration` | Iteration | Each ReAct loop iteration | Dynamic tool filtering |
| `AFTER_ITERATION` | `after_iteration` | Iteration | After each iteration | Restore state, checkpoint |
| `BEFORE_TOOL_EXECUTION` | `before_tool_execution` | Iteration | Before tool batch | Policy guard, logging |
| `AFTER_TOOL_EXECUTION` | `after_tool_execution` | Iteration | After tool batch | Result transform, logging |
| `AFTER_LLM_RESPONSE` | `after_llm_response` | Iteration | After LLM response | Output guard, loop detection |
| `BEFORE_LLM` | `before_llm` | Iteration | Before LLM provider call | Prompt capture, timing |
| `FINALIZE_CONTENT` | `finalize_content` | Iteration | Before final output (sync) | Content formatting |
| `AFTER_APPROVAL` | `after_approval` | Iteration | After approval decision applied | Approval timing |

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

`before_graph` matches `HookPoint.BEFORE_GRAPH`, `start_node_turn` matches `HookPoint.START_NODE_TURN`, `before_turn` matches `HookPoint.BEFORE_TURN`, `before_iteration` matches `HookPoint.BEFORE_ITERATION`, etc. Graph-level methods end in `_graph`, node-level methods end in `_node_turn`, turn-attempt and iteration-level methods match their HookPoint suffix.

### Rule 3: ReAct clean mode runs without hook services

Hooks are only active in "full" mode (when `AgentRuntimeServices` is wired).

## For AI Agents

- Hooks are for **observation and context injection** — use Interceptors for execution wrapping.
- All hooks run synchronously with a 10-second timeout by default.
- `Hook.name` defaults to the concrete class name (`type(self).__name__`); override for custom diagnostics.
- The veto/result mechanism has been removed. Hooks return `None` — observation only. For execution denial, use Interceptors.
- `HookErrorPolicy` (IGNORE/LOG/ABORT) is retained per-hook via `HookSpec.on_error`.
- `FinalizeContentHook` + `dispatch_finalize` are retained for synchronous content formatting before final output.
- There are 15 hook points across 4 levels (graph / node / turn-attempt / iteration); a hook implementation only needs to define the methods it cares about (all are optional).
- `notification.py` provides utilities for hook lifecycle event notifications.
- **Before writing any hook, re-read Rule 1.** If you find yourself reaching for `self._something[session_id]`, stop — use `ctx.runtime.state.custom` instead.

## Dependencies

- `modex_agent.core.agent` — `AgentContext` (passed as payload to all hooks)
- `modex_agent.runtime.enums` — `TurnCustomKey` (typed keys for `state.custom`)
