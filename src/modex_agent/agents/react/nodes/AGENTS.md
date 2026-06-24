<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# nodes

## Purpose

Graph node implementations for the ReAct agent execution loop. Each node is a single step in the 4-node directed graph: `START → LLM → TOOL → END`. Nodes are invoked by the graph runtime and return `NodeTransition` to determine the next node.

## Key Files

| File | Description |
|------|-------------|
| `start.py` | `StartNode` — entry point; routes to LLM for fresh turns or to the saved `current_node` for resumed (suspended) turns |
| `llm.py` | `LLMNode` — assembles messages, calls the LLM provider (streaming or non-streaming), dispatches hooks/interceptors, emits iteration events |
| `tool.py` | `ToolNode` — classifies tool calls for approval, suspends for approval when `PENDING`, batch-executes approved calls, routes back to LLM or END |
| `end.py` | `EndNode` — assembles `AgentResult` (normal/error/cancelled/max-iterations), emits completion events, marks turn as completed |
| `__init__.py` | Re-exports `StartNode`, `LLMNode`, `ToolNode`, `EndNode` |

## For AI Agents

### Working In This Directory
- Each node extends `Node` and implements `async def execute(ctx: AgentContext) -> NodeTransition`
- Nodes access ReAct-specific state via `get_react_state(ctx)` from `framework/agents/react/state.py`
- Node transitions use `ReActReason` enums (e.g. `NORMAL_START`, `HAS_TOOLS`, `NO_TOOLS`, `LLM_ERROR`, `DONE`)
- `ToolNode` is the most complex node — it handles the full approval lifecycle (classify → suspend → resume → execute)
- Approval suspension uses `interrupt()` from `framework.core.graph.interrupt` which raises a `GraphInterrupt`

### Common Patterns
- Read `state.phase` to detect `SUSPENDED` vs fresh turns
- Use `ctx.emitter.emit(ReActEvent.xxx)` for event-driven observability
- LLMNode drains injection queue + control channel at safe points
- ToolNode normalizes approval decisions via `_normalize_batch_decisions()` before execution

## Dependencies

### Internal
- `framework/agents/react/` — `agent.py` (ReActAgent, ReActEvent), `constants.py` (ReActNode, ReActReason), `state.py` (ReActTurnState)
- `framework/core/graph/` — `node.py` (Node, NodeTransition), `interrupt.py` (interrupt)
- `framework/core/` — `AgentContext`, `LLMResponse`, `ToolCall`, emitter types
- `framework/runtime/` — `TurnPhase`, interceptors, dispatch deadline
- `framework/hook/` — HookPoint, HookPayload, control_drain
- `framework/approval/` — ApprovalDecision, ApprovalTier enums

<!-- MANUAL -->
