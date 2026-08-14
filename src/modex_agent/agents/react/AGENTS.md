<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-10 -->

# react

## Purpose

Graph-based ReAct agent runtime. Owns the turn loop, LLM calls, tool execution,
approval suspend/resume, and integration points for hooks, interceptors, and control.

## Key Files

| File | Description |
|------|-------------|
| `agent.py` | `ReActAgent(Agent[ReActEvent])` — event enum, turn context setup, constructs `Graph` via `build_react_graph().compile()`, wraps in `GraphEngine`, executes via `engine.run_async(ReActGraphContext(...))`. |
| `graph.py` | `build_react_graph()` — builds `Graph[ReActTurnState]` (from `modex_graph`) with 6 ReAct nodes + 11 edges (8 total with engine sentinels). |
| `context.py` | `ReActGraphContext(GraphContext[ReActTurnState])` — type-safe accessors (`agent_ctx`, `tool_manager`, `context_manager`). |
| `runtime.py` | `ReactGraphRuntime(GraphRuntime)` — AOP bridge mapping ReAct StrEnums to framework enums, bridging `GraphContext.user_data` → `AgentContext`. |
| `state.py` | `ReActTurnState(GraphState)`, `ReActSnapshotPolicy`, and `ReActRuntimeStateCodec`. |
| `builder.py` | `ReActAgentBuilder` -- `build_agent()` + `build_emitter_factory()` from `AgentDescriptor`. |
| `approval.py` | *(removed — migrated to `modex_agent.approval.runtime`)* |
| `constants.py` | `ReActNode`, `ReActHookPoint` (11 values: iteration-level + turn-attempt `BEFORE_TURN`/`AFTER_TURN` + node-level `START_NODE_TURN`/`END_NODE_TURN`), `ReActScope`, `ReActEvent`, `InterruptReason` (B1) StrEnums. |
| `nodes/start.py` | `StartNode` -- routes to BEFORE (fresh) or TOOL (resume from approval). Dispatches `START_NODE_TURN` hook on fresh-turn path only (not on resume). |
| `nodes/before_turn.py` | `BeforeTurnNode` -- increments `turn_attempt`, resets `iteration = 0`, dispatches `BEFORE_TURN` hook, routes to LLM. |
| `nodes/llm.py` | `LLMNode` -- calls LLM, handles streaming, dispatches hooks/interceptors via `ctx.runtime.*`, emits iteration events. |
| `nodes/tool.py` | `ToolNode` -- classify all -> suspend for approval via `ctx.interrupt(tx)` -> batch execute -> route. |
| `nodes/after_turn.py` | `AfterTurnNode` -- constructs `AgentResult`, writes `state.result`, dispatches `AFTER_TURN` hook (with `{"result": result}` payload), then consumes `CONTINUATION_REQUEST` + `CONTINUATION_RENEW_MAX_TURNS` one-shot flags to decide continuation. Routes to BEFORE/END. Watchdog: when RENEW is set and `turn_attempt >= MAX_TURNS`, gate increments `MAX_TURNS` by 1. Default `MAX_TURNS` is 3. No hardcoded deliver-reminder (migrated to `DeliverRetryHook`). |
| `nodes/end.py` | `EndNode` -- reads `state.result` (raises `RuntimeError` if None), emits completion events, dispatches `END_NODE_TURN` hook. |

## Graph Edges

Edges are plain topology — nodes route at runtime via `deliver()`.

```
START → BEFORE
START → TOOL
BEFORE → LLM
LLM   → TOOL
LLM   → AFTER
TOOL  → LLM
TOOL  → AFTER
AFTER → END
AFTER → BEFORE
END   → GraphNode.END
```

## Runtime Modes

- **clean**: plain ReAct graph, no hooks/interceptors/approval/control/state-store.
- **full**: all services wired through `AgentRuntimeServices`. Runtime assembly lives in
  `AgentPipeline` / `TurnContextBuilder` (the old `RuntimeAssembler` was removed as dead
  code); the approval runtime is built by `ioc.factories.approval.build_approval_runtime`
  and injected via `AgentPipeline.runtime_services`.

## Approval Flow

```
ToolNode._classify_all() -> TieredToolApprovalClassifier
  -> PENDING: ctx.runtime.capture_snapshot() -> ctx.interrupt(tx) -> GraphInterrupt
  -> Pipeline: ApprovalRenderer.detect() -> apply_decision() (mutates ApprovalTransaction.decisions)
  -> replace_approval() persists updated transaction into snapshot
  -> StartNode detects SUSPENDED -> routes to TOOL -> _resume_suspended_batch()
  -> reads ctx.state.approval.decisions -> ALLOWED tools execute, DENIED return error
```

Deny policy: default `TOOL_RESULT_ONLY` (loop continues); override to `CANCEL_TURN` to abort.

## Key Invariants

- `AgentRuntime` / `AgentRuntimeServices` are assembled by `AgentPipeline` /
  `TurnContextBuilder` (per turn). The approval runtime is constructed by
  `ioc.factories.approval.build_approval_runtime` and wired onto the main pipeline via
  `AgentPipeline.runtime_services` (a mirror property) — see ADR-0008.
- ReAct runs on the `modex_graph` engine (ADR-0033). `ReActAgent.run()` constructs
  `Graph` + `GraphEngine` + `ReActGraphContext` per turn and calls `engine.run_async()`.
  Hooks follow a 4-level hierarchy: graph-level (`BEFORE_GRAPH`/`AFTER_GRAPH`/`FINALLY_GRAPH`)
  and `around_turn` interceptor remain in `ReActAgent.run()`'s `actual_turn()`; node-level
  (`START_NODE_TURN`/`END_NODE_TURN`) dispatched in `StartNode`/`EndNode`; turn-attempt
  (`BEFORE_TURN`/`AFTER_TURN`) dispatched in `BeforeTurnNode`/`AfterTurnNode`; iteration-level
  dispatched in `LLMNode`/`ToolNode`. `BeforeTurnNode` dispatches `BEFORE_TURN` hook;
  `AfterTurnNode` dispatches `AFTER_TURN` hook and no longer injects deliver-reminder
  (migrated to `DeliverRetryHook`).
- **turnId / trace_id future consideration**: `turn_uuid` is generated in the pipeline layer
  (`TurnRunner`), and `trace_id` is generated in `TraceCollectorHook.before_graph()`. Both
  fire once per `actual_turn()` call, so approval resume (which re-enters `actual_turn()`)
  generates a new trace root. A future improvement could move `trace_id` generation to
  `START_NODE_TURN` so approval-resume continues the same trace. This is deferred because it
  involves `TraceCollectorHook` span lifecycle redesign.
- Node-level AOP (hooks, interceptors, governance, control drain, snapshot, emit) is
  routed through `ReactGraphRuntime` via `ctx.runtime.*`. Node-level, turn-attempt, and
  iteration-level hooks are all dispatched explicitly by nodes via
  `ctx.runtime.dispatch_hook(ReActHookPoint.X, ctx)`, NOT engine-auto-invoked
  (preserves hook timing exactly). Graph-level hooks bypass `HOOK_POINT_MAP` — they
  are dispatched in `actual_turn()` via `hook_runner.dispatch(HookPoint.X, agent_ctx)`
  directly.
- Per-turn state lives in `ctx.state` (`ReActTurnState`, a mutable `GraphState`).
  `ctx.state.result` holds the final
  `AgentResult` (replaces the old `custom[GRAPH_RESULT]` pattern).
- Approval does NOT go through interceptors; it is handled at the `ToolNode`/pipeline
  layer via `ctx.interrupt(tx)` → `GraphInterrupt` → `TurnSnapshot`.
- `ApprovalTransaction` / `ToolBatchState` / `ToolCallState` are mutable `BaseModel`
  (NOT frozen) — the approval state machine mutates `decisions` dict and `status`/
  `decision` fields at runtime (ADR-0033 D14).
- Control: `ctx.runtime.drain_control(ctx)` is called at safe points (LLMNode, ToolNode)
  to check for `CANCEL_TURN`.
