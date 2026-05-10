<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-02 -->

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
| `state.py` | Runtime state types, including `TurnResumeState` and runtime store aliases. |
| `strategy.py` | Suspend/resume strategy used by approval flows. |
| `constants.py` | Metadata keys and runtime constants shared by nodes. |
| `nodes/start.py` | Start/resume node. Restores state and routes to the stored resume target. |
| `nodes/llm.py` | Model node. Handles prompt/model execution and streaming integration. |
| `nodes/tool.py` | Tool node. Handles tool execution, approval suspend/resume, and cancellation metadata. |
| `nodes/end.py` | End node. Builds `AgentResult`, including `turn_cancelled` results. |

## Runtime Modes

- `clean`: should execute as a plain ReAct graph. Hooks, approval, interceptors,
  control services, suspend/resume strategy, runtime state store, and injection
  queues should be stripped at turn entry, with one concise log line explaining
  the sanitization. Do not add repeated clean-mode conditionals in every node.
- `full`: wires hook, interceptor, control, approval, and runtime state services
  through `AgentContext` extensions.

## Hook / Interceptor / Control Rules

- Hooks observe or transform lifecycle payloads. Store per-turn hook state in
  `ctx.metadata`, never in shared hook instance attributes.
- Interceptors wrap execution boundaries such as turn, iteration, LLM stream, and
  tool call. Tool call wrapping is active; turn/iteration wrapping should only be
  enabled when `ReActAgent` owns those scopes explicitly.
- Control is the runtime command plane. Current handling is safe-boundary based;
  future live intervention should target operation IDs for LLM streams and tool
  calls.

## Resume And Cancellation

`TurnResumeState` stores both `resume_node` and `resume_reason`. Approval resume
currently returns to `ToolNode`, but new suspend points should set their own
target instead of relying on approval-specific defaults.

Tool cancellation paths should set `ReActMetaKey.END_REASON` and
`ReActMetaKey.CANCEL_REASON`. `EndNode` maps those values to an
`AgentResult(stop_reason="turn_cancelled")`.

## Testing Requirements

- Unit tests live under `tests/unit/agents/react/`.
- Mock `LLMProvider`, tools, and emitters directly; avoid broad integration
  setup for node-level behavior.
- Cover resume target/reason, approval deny checkpoint signatures, cancellation
  result mapping, and clean/full mode boundaries when those paths change.


