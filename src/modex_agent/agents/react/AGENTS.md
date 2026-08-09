<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

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
| `constants.py` | `ReActNode`, `ReActHookPoint`, `ReActScope`, `ReActEvent`, `InterruptReason` (B1) StrEnums. |
| `nodes/start.py` | `StartNode` -- routes to BEFORE (fresh) or TOOL (resume from approval). |
| `nodes/before_turn.py` | `BeforeTurnNode` -- mechanical only: `turn_attempt += 1`, `iteration = 0`, route to LLM. NO hook dispatch (BEFORE_TURN stays in `actual_turn()`). |
| `nodes/llm.py` | `LLMNode` -- calls LLM, handles streaming, dispatches hooks/interceptors via `ctx.runtime.*`, emits iteration events. |
| `nodes/tool.py` | `ToolNode` -- classify all -> suspend for approval via `ctx.interrupt(tx)` -> batch execute -> route. |
| `nodes/after_turn.py` | `AfterTurnNode` -- mechanical only: construct `AgentResult`, write `state.result`, check `CONTINUATION_REQUEST`, route to BEFORE/END. NO hook dispatch (AFTER_TURN stays in `actual_turn()`). |
| `nodes/end.py` | `EndNode` -- reads `state.result` (raises `RuntimeError` if None), emits completion events. |

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
  Turn-level hooks (`BEFORE_TURN`/`AFTER_TURN`/`FINALLY_TURN`) and `around_turn`
  interceptor remain in `ReActAgent.run()`'s `actual_turn()`, NOT in the graph runtime.
  `BeforeTurnNode` and `AfterTurnNode` are mechanical only — they handle turn-attempt
  counting, result construction, and continuation routing; they do NOT dispatch hooks.
- Node-level AOP (hooks, interceptors, governance, control drain, snapshot, emit) is
  routed through `ReactGraphRuntime` via `ctx.runtime.*`. Iteration-level hooks
  (`BEFORE_ITERATION`/`AFTER_ITERATION`) are dispatched explicitly by nodes, NOT
  engine-auto-invoked (preserves hook timing exactly).
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
