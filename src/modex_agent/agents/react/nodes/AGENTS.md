<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# nodes

## Purpose

Graph node implementations for the ReAct agent execution loop. Each node is a single step in the 6-node directed graph: `START → BEFORE → LLM ↔ TOOL → AFTER → END`. Nodes implement `modex_graph.Node[S]` and call `deliver(content, target, ctx)` to route to the next node.

## Key Files

| File | Description |
|------|-------------|
| `start.py` | `StartNode` — entry point; routes to BEFORE for fresh turns or to TOOL for resumed (approval-suspended) turns |
| `before_turn.py` | `BeforeTurnNode` — mechanical only: increments `turn_attempt`, resets `iteration = 0`, routes to LLM. NO hook dispatch (BEFORE_TURN stays in `actual_turn()`) |
| `llm.py` | `LLMNode` — assembles messages, calls the LLM provider (streaming or non-streaming), dispatches hooks/interceptors via `ctx.runtime.*`, emits iteration events |
| `tool.py` | `ToolNode` — classifies tool calls for approval, suspends for approval when `PENDING` via `ctx.interrupt(tx)`, batch-executes approved calls, routes back to LLM or AFTER |
| `after_turn.py` | `AfterTurnNode` — mechanical only: constructs `AgentResult`, writes `state.result`, checks `CONTINUATION_REQUEST` flag, routes to BEFORE (continuation) or END. NO hook dispatch (AFTER_TURN stays in `actual_turn()`) |
| `end.py` | `EndNode` — reads `state.result` (raises `RuntimeError` if None), emits completion events, delivers to `GraphNode.END` |
| `__init__.py` | Re-exports `StartNode`, `LLMNode`, `ToolNode`, `EndNode` (`BeforeTurnNode`/`AfterTurnNode` are imported directly from their modules by `graph.py`) |

## For AI Agents

### Working In This Directory
- Each node extends `modex_graph.Node[S]` and implements `async def execute(ctx: GraphContext[ReActTurnState], integrated_input: IntegratedInput) -> None`
- Nodes access ReAct-specific state via `ctx.state` (a `ReActTurnState` — no longer via `get_react_state(ctx)` helper)
- Node routing uses `deliver(content, target, ctx)` to select the next node
- AOP calls go through `ctx.runtime.*` (`ReactGraphRuntime`): `dispatch_hook`, `around`, `apply_governance`, `drain_control`, `capture_snapshot`, `emit`
- `ToolNode` is the most complex node — it handles the full approval lifecycle (classify → suspend → resume → execute)
- Approval suspension uses `ctx.interrupt(tx)` which raises a `GraphInterrupt` from `modex_graph.exceptions`

### Common Patterns
- Read `ctx.state.phase` to detect `SUSPENDED` vs fresh turns
- Use `ctx.runtime.emit(ReActEvent.xxx, data, ctx)` for event-driven observability
- LLMNode drains control channel at safe points via `ctx.runtime.drain_control(ctx)`
- ToolNode normalizes approval decisions via `_normalize_batch_decisions()` before execution

## Dependencies

### Internal
- `modex_agent/agents/react/` — `agent.py` (ReActAgent, ReActEvent), `constants.py` (ReActNode, ReActHookPoint, ReActScope, ReActEvent), `state.py` (ReActTurnState), `runtime.py` (ReactGraphRuntime), `context.py` (ReActGraphContext)
- `modex_graph` — `Node[S]`, `GraphContext[S]`, `IntegratedInput`, `GraphInterrupt` (ADR-0033)
- `modex_agent/core/` — `AgentContext`, `LLMResponse`, `ToolCall`, emitter types
- `modex_agent/runtime/` — `TurnPhase`, interceptors, dispatch deadline
- `modex_agent/hook/` — HookPoint, HookPayload
- `modex_agent/approval/` — ApprovalDecision, ApprovalTier enums

<!-- MANUAL -->
