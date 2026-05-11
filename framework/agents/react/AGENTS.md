<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-11 -->

# react

## Purpose

Graph-based ReAct agent runtime. This package owns the turn loop, model calls,
tool execution, approval suspend/resume, clean/full modes, runtime state, and
the integration points for hooks, interceptors, and control.

## Key Files

| File | Description |
|------|-------------|
| `agent.py` | `ReActAgent` entry point. Builds turn context and delegates execution to the graph. |
| `graph.py` | `ReActGraph` composition for `start -> llm -> tool -> end`. |
| `state.py` | `ReActTurnState`, `ReActSnapshotPolicy`, `ReActRuntimeStateCodec`. |
| `constants.py` | ReAct node and transition reason enums. |
| `approval.py` | `ApprovalRuntime` (classifier + deny_policy service) + `TieredToolApprovalClassifier`. |
| `assembler.py` | `RuntimeAssembler` — single entry point for `AgentRuntime` construction. |
| `nodes/start.py` | Start/resume node. Routes to stored `current_node` when `phase=SUSPENDED`. |
| `nodes/llm.py` | Model node. Prompt/model execution and streaming integration. |
| `nodes/tool.py` | Tool node. Classify → suspend for approval → batch execute. |
| `nodes/end.py` | End node. Builds `AgentResult`, including `turn_cancelled` results. |

## Runtime Modes

- `clean`: executes as a plain ReAct graph. Hooks, approval, interceptors,
  control services, runtime state store, and injection queues should be absent
  from the turn runtime.
- `full`: wires hook, interceptor, control, approval, and runtime state services
  through `AgentRuntimeServices`.

## Approval Flow (THE one and only path)

```
LLM generates tool_calls
  → ToolNode._classify_all() → ApprovalRuntime.classifier.classify()
  → If PENDING: _suspend_for_approval() → ApprovalTransaction → TurnSnapshot → interrupt
  → Pipeline: ApprovalRenderer.detect() → _handle_snapshot_approval()
  → If every_tool_decided: _execute_turn()
  → StartNode → TOOL → _resume_suspended_batch()
  → Sets PRE_APPROVED_TOOL_IDS on ALLOWED tools
  → _execute_batch(): ALLOWED=tool executes, DENIED/PREEMPTED=error result
  → Continue to LLM (default TOOL_RESULT_ONLY) or cancel turn (configurable CANCEL_TURN)
```

## Deny Policy

- **Default**: `TOOL_RESULT_ONLY` — denied tools return errors with `deny_reason`, ReAct loop continues.
- **Cancel override**: set `ApprovalRuntime.default_deny_policy=CANCEL_TURN` to terminate the turn on any denial.
- **EXTENSION POINT**: `_execute_batch` has a comment block explaining how to configure per-agent/per-batch deny policy.

## Resume And Cancellation

- Suspended turns: `TurnSnapshot` + `ReActTurnState.current_node`. Resume enters through `StartNode` → routes to stored node.
- `ToolNode._resume_suspended_batch` sets `PRE_APPROVED_TOOL_IDS` in `state.custom` to prevent re-approval.
- `deny_reason` is read from `state.approval.deny_reason` (NOT from `ctx.metadata`).
- `EndNode` maps cancelled turns to `AgentResult(stop_reason="turn_cancelled")`.

## Hook / Interceptor / Control Rules

- Hooks: store per-turn state in `ctx.runtime.state`, never in shared instance attributes.
- Interceptors: wrap execution boundaries. Approval does NOT go through interceptors.
- Control: runtime command plane. `ControlDrainInterceptor` drains cancel/inject/config only (no APPROVAL_RESPONSE).

## Testing Requirements

- Unit tests under `tests/unit/agents/react/`.
- Mock `LLMProvider`, tools, and emitters directly.
- Cover START-based resume routing, approval transactions, PRE_APPROVED_TOOL_IDS, deny policy defaults.
- When testing CANCEL_TURN behavior, explicitly set `default_deny_policy=ApprovalDenyPolicy.CANCEL_TURN`.
