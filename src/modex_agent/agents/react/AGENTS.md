<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# react

## Purpose

Graph-based ReAct agent runtime. Owns the turn loop, LLM calls, tool execution,
approval suspend/resume, and integration points for hooks, interceptors, and control.

## Key Files

| File | Description |
|------|-------------|
| `agent.py` | `ReActAgent(Agent[ReActEvent])` -- event enum, turn context setup, delegates to graph. |
| `graph.py` | `ReActGraph` -- 4-node graph: START -> LLM -> TOOL -> END with reason-based edges. |
| `state.py` | `ReActTurnState`, snapshot payload keys, `ReActRuntimeStateCodec`. |
| `builder.py` | `ReActAgentBuilder` -- `build_agent()` + `build_emitter_factory()` from `AgentDescriptor`. |
| `approval.py` | `ApprovalRuntime` + `TieredToolApprovalClassifier` (NORMAL/DANGEROUS path-based). |
| `constants.py` | `ReActNode`, `ReActReason` enums. |
| `nodes/start.py` | `StartNode` -- routes to LLM (fresh) or stored `current_node` (resume from suspended). |
| `nodes/llm.py` | `LLMNode` -- calls LLM, handles streaming, emits iteration events. |
| `nodes/tool.py` | `ToolNode` -- classify all -> suspend for approval -> batch execute -> route. |
| `nodes/end.py` | `EndNode` -- assembles `AgentResult` (normal/error/cancelled). |

## Graph Edges

```
START --NORMAL_START--> LLM
START --RESUME_TOOLS--> TOOL
LLM   --HAS_TOOLS--> TOOL
LLM   --NO_TOOLS--> END
LLM   --MAX_ITERATIONS--> END
LLM   --LLM_ERROR--> END
TOOL  --TOOLS_DONE--> LLM
TOOL  --TURN_CANCELLED--> END
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
  -> PENDING: _suspend_for_approval() -> TurnSnapshot -> GraphInterrupt
  -> Pipeline: ApprovalRenderer.detect() -> apply_decision()
  -> StartNode detects SUSPENDED -> routes to TOOL -> _resume_suspended_batch()
  -> PRE_APPROVED_TOOL_IDS set on ALLOWED tools, denied tools return error
```

Deny policy: default `TOOL_RESULT_ONLY` (loop continues); override to `CANCEL_TURN` to abort.

## Key Invariants

- `AgentRuntime` / `AgentRuntimeServices` are assembled by `AgentPipeline` /
  `TurnContextBuilder` (per turn). The approval runtime is constructed by
  `ioc.factories.approval.build_approval_runtime` and wired onto the main pipeline via
  `AgentPipeline.runtime_services` (a mirror property) — see ADR-0008.
- Hook state goes in `ctx.runtime.state`, never on shared instance attributes.
- Approval does NOT go through interceptors; it is handled at the `ToolNode`/pipeline layer via `TurnSnapshot`.
- Control: `drain_control_channel()` is called at safe points (LLMNode, ToolNode,
  the iteration loop) to check for `CANCEL_TURN`, but the channel is currently not
  fed in the default runtime (real cancellation is `asyncio.Task.cancel()` in the
  pipeline). These drain calls are effectively no-ops unless a producer is added —
  see `modex_agent/control/AGENTS.md`.
