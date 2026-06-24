<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# graph

Directed graph state machine powering ReActAgent's 4-node execution loop (START → LLM → TOOL → END). Generic over result type `R`.

## Key Files

| File | Description |
|------|-------------|
| `graph.py` | `Graph[R]` — node registry + edge routing; `Edge` (frozen dataclass: source, target, reason) |
| `node.py` | `Node[R]` ABC — `execute(ctx) → NodeTransition(target, reason)`; `NodeTransition` frozen dataclass |
| `engine.py` | `GraphEngine[R]` — iterates nodes via `Graph.next_node()`, propagates `GraphInterrupt`, returns result from `ctx.runtime.state.custom[TurnCustomKey.GRAPH_RESULT]` |
| `interrupt.py` | `GraphInterrupt(Exception)` + `interrupt(value)` — pause graph for approval/resume; carries `value`, `node_name`, `iteration` |
| `constants.py` | `GraphNode(StrEnum)` — `END = "__end__"` sentinel |

## Edge Routing

- `Graph.next_node(source, reason)`: scans edges from `source`; returns first exact `reason` match, then first `reason=None` (unconditional fallback), else `KeyError`.
- `Edge.reason=None` = unconditional fallback edge.

## Design Rules

- `GraphInterrupt` must propagate upward — never catch and swallow it.
- `Node.execute()` receives `AgentContext[R]` and returns `NodeTransition`, not raw values.
- `GraphEngine.build_result()` reads from turn state (`TurnCustomKey.GRAPH_RESULT`), not return values.
- Graph is agnostic to ReAct / Hook / Interceptor / Approval concerns.

## Dependencies

- `framework.core.agent.AgentContext` — node execution context
- `framework.runtime.enums.TurnCustomKey` — graph result storage key

<!-- MANUAL: -->
