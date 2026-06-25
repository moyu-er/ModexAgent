<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# graph

Directed graph state machine powering ReActAgent's 4-node execution loop (START → LLM → TOOL → END). Generic over result type `R`.

## Key Files

| File | Description |
|------|-------------|
| `graph.py` | `Graph[R]` — node registry + edge routing; `Edge` (frozen dataclass: source, target, reason) |
| `node.py` | `Node[R]` ABC — `execute(ctx) → NodeTransition(target, reason)`; `NodeTransition` frozen dataclass |
| `engine.py` | `GraphEngine[R]` — iterates nodes via `Graph.next_node()`, propagates `GraphInterrupt`, returns result via the graph's injected `result_extractor` (`build_result` returns `None` when no extractor is set) |
| `interrupt.py` | `GraphInterrupt(Exception)` + `interrupt(value)` — pause graph for approval/resume; carries `value`, `node_name`, `iteration` |
| `constants.py` | `GraphNode(StrEnum)` — `END = "__end__"` sentinel |

## Edge Routing

- `Graph.next_node(source, reason)`: scans edges from `source`; returns first exact `reason` match, then first `reason=None` (unconditional fallback), else `KeyError`.
- `Edge.reason=None` = unconditional fallback edge.

## Design Rules

- `GraphInterrupt` must propagate upward — never catch and swallow it.
- `Node.execute()` receives `AgentContext[R]` and returns `NodeTransition`, not raw values.
- `GraphEngine.build_result()` delegates to the graph's injected `result_extractor` (e.g. `ReActGraph` injects the turn-state read at `TurnCustomKey.GRAPH_RESULT`); `core/graph` stays agnostic to where results are stored.
- Graph is agnostic to ReAct / Hook / Interceptor / Approval concerns.

## Dependencies

- `modex_agent.core.agent.AgentContext` — node execution context

<!-- MANUAL: -->
