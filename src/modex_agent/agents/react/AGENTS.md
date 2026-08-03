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
| `graph.py` | `build_react_graph()` — builds `Graph[ReActTurnState]` (from `modex_graph`) with 4 nodes + 8 edges. |
| `context.py` | `ReActGraphContext(GraphContext[ReActTurnState])` — type-safe accessors (`agent_ctx`, `tool_manager`, `context_manager`). |
| `runtime.py` | `ReactGraphRuntime(GraphRuntime)` — AOP bridge mapping ReAct StrEnums to framework enums, bridging `GraphContext.user_data` → `AgentContext`. |
| `state.py` | `ReActTurnState(GraphState)` with `Annotated[T, LastValue]` fields, `ReActSnapshotPolicy` (simplified via `state.checkpoint()` per-channel path, ADR-0033 D14), `ReActRuntimeStateCodec`. |
| `builder.py` | `ReActAgentBuilder` -- `build_agent()` + `build_emitter_factory()` from `AgentDescriptor`. |
| `approval.py` | *(removed — migrated to `modex_agent.approval.runtime`)* |
| `constants.py` | `ReActNode`, `ReActHookPoint`, `ReActScope`, `ReActEvent`, `InterruptReason` (B1) StrEnums. |
| `nodes/start.py` | `StartNode` -- routes to LLM (fresh) or stored `current_node` (resume from suspended). |
| `nodes/llm.py` | `LLMNode` -- calls LLM, handles streaming, dispatches hooks/interceptors via `ctx.runtime.*`, emits iteration events. |
| `nodes/tool.py` | `ToolNode` -- classify all -> suspend for approval via `ctx.interrupt(tx)` -> batch execute -> route. |
| `nodes/end.py` | `EndNode` -- assembles `AgentResult` (normal/error/cancelled), writes `ctx.state.result`. |

## Graph Edges

Edges are plain topology — nodes route at runtime via `deliver()`.

```
START → LLM
LLM   → TOOL
LLM   → END
TOOL  → LLM
TOOL  → END
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
  interceptor remain in `ReActAgent.run()`, NOT in the graph runtime.
- Node-level AOP (hooks, interceptors, governance, control drain, snapshot, emit) is
  routed through `ReactGraphRuntime` via `ctx.runtime.*`. Iteration-level hooks
  (`BEFORE_ITERATION`/`AFTER_ITERATION`) are dispatched explicitly by nodes, NOT
  engine-auto-invoked (preserves hook timing exactly).
- Per-turn state lives in `ctx.state` (`ReActTurnState`, a `GraphState(BaseModel)` with
  `Annotated[T, LastValue]` per-field channels). `ctx.state.result` holds the final
  `AgentResult` (replaces the old `custom[GRAPH_RESULT]` pattern).
- Approval does NOT go through interceptors; it is handled at the `ToolNode`/pipeline
  layer via `ctx.interrupt(tx)` → `GraphInterrupt` → `TurnSnapshot`.
- `ApprovalTransaction` / `ToolBatchState` / `ToolCallState` are mutable `BaseModel`
  (NOT frozen) — the approval state machine mutates `decisions` dict and `status`/
  `decision` fields at runtime (ADR-0033 D14).
- Control: `ctx.runtime.drain_control(ctx)` is called at safe points (LLMNode, ToolNode)
  to check for `CANCEL_TURN`.
