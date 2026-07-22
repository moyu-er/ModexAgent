# modex_graph

A generalized typed graph engine with sync/async dual mode, Pydantic state,
channels, and a `GraphBubbleUp` exception family. Framework-agnostic sibling
of `modex_agent`.

## Runtime dependencies

- `pydantic>=2.0.0,<3`
- Python standard library

That's it. No `modex_agent`. No business code. The framework-agnostic
boundary is enforced by an architecture guard test.

## Public surface

See `__init__.py` for the full export list. Key types:

- `Graph`, `Node`, `CompiledGraph`, `GraphEngine`
- `GraphContext`, `GraphRuntime`, `GraphState`
- `NodeResult`, `Command`, `Task`
- `BaseChannel`, `LastValue`, `ReducerChannel`, `Codec`, `register_codec`
- `GraphBubbleUp`, `GraphInterrupt`, `GraphDrained`, `ParentCommand`
- `GraphNode` (START/END sentinels), `RoutingError`, `GraphRecursionError`

See `docs/adr/0033-generalized-graph-engine.md` for the authoritative design.
