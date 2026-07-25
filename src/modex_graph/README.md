# modex_graph

A generalized typed graph engine with sync/async dual mode, Pydantic state,
channels, a `GraphBubbleUp` exception family, and a pluggable scheduler
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
- `NodeResult`, `Command`, `Task`, `DispatchEvent`
- `BaseChannel`, `LastValue`, `ReducerChannel`, `Codec`, `register_codec`
- `Scheduler` (ABC), `LinearScheduler`, `ParallelScheduler`,
  `SchedulerKind`, `NodeTrigger`, `NodeInstanceStatus`, `NodeInstance`
- `DispatchStore`, `InMemoryDispatchStore`, `SqliteDispatchStore`
- `WriteConflictDetector`, `GenerationWriteTracker`
- `CheckpointStore`, `CheckpointData`, `MemoryCheckpointStore`, `SqliteCheckpointStore`
- `GraphBubbleUp`, `GraphInterrupt`, `GraphDrained`, `ParentCommand`,
  `InvalidUpdateError`
- `GraphNode` (START/END sentinels), `RoutingError`, `GraphRecursionError`

See `docs/adr/0033-generalized-graph-engine.md` (Phase a) and
`docs/adr/0034-parallel-scheduling-engine.md` (Phase c parallel scheduling)
for the authoritative design.
