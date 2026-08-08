# modex_graph

A generalized typed graph engine with sync/async dual mode, Pydantic state,
a `GraphBubbleUp` exception family, and a pluggable scheduler
(`LinearScheduler` default / `ParallelScheduler` opt-in). Framework-agnostic
sibling of `modex_agent`.

## Runtime dependencies

- `pydantic>=2.0.0,<3`
- Python standard library

That's it. No `modex_agent`. No business code. The framework-agnostic
boundary is enforced by an architecture guard test.

## Public surface

See `__init__.py` for the full export list. Key types:

- `Graph`, `Node`, `CompiledGraph`, `GraphEngine`
- `GraphContext`, `GraphRuntime`, `GraphState`
- `Scheduler` (ABC), `LinearScheduler`, `ParallelScheduler`,
  `SchedulerKind`, `NodeTrigger`, `NodeInstanceStatus`, `NodeInstance`
- `GraphPersistenceCoordinator`, `GraphMetadata`
- `GraphBubbleUp`, `GraphInterrupt`, `GraphDrained`, `ParentCommand`
- `GraphNode` (START/END), `RoutingError`, `GraphRecursionError`

See `AGENTS.md` for scheduling convergence design (G8: unified `bootstrap`
entry point, no separate recovery engine).

See `docs/adr/0033-generalized-graph-engine.md` and
`docs/adr/0034-parallel-scheduling-engine.md` for the authoritative design.
