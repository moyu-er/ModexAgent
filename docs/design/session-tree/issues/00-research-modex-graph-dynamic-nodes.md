# R01: Can modex_graph support dynamic (runtime-created) nodes?

> Type: `wayfinder:research` (AFK)
> Status: **Resolved** — research complete
> Blocks: T01

## Question

Can modex_graph support runtime-created nodes (not compiled from spec)? What specific changes are needed? Read the modex_graph source to answer:

1. **NodeFactory / NodeRegistry**: Currently `NodeRegistry.create(node_spec)` instantiates from `NodeSpec` at compile time. Can nodes be registered at runtime? What's the minimal change?

2. **CompiledGraph**: Currently holds `nodes: dict[str, Node]` built at compile time. Can nodes be added after compilation? Is `CompiledGraph` mutable?

3. **GraphSpecCompiler**: The `compile(spec)` method creates START/END + spec nodes. Can a "dynamic compile" mode skip the spec and build an empty graph that nodes are added to at runtime?

4. **Schedulers**: 
   - `LinearScheduler._handle_linear_dispatch` records dispatches — does it assume the target node exists in `self.graph.nodes`?
   - `ParallelScheduler._handle_dispatch` calls `validate_dispatch_target(self.graph, ...)` — does this require the target to be in the compiled topology?
   - Can schedulers handle nodes that don't exist in the topology?

5. **NodeStateStore / DeliverStore**: These are per-graph-instance. Can they work for a "virtual" graph instance (SessionTree) without a real GraphSpec?

6. **GraphContext**: Currently created by `GraphOrchestrator.run_instance`. Can a `GraphContext` be created without a `CompiledGraph`? What does SessionTree need from `GraphContext`?

7. **bootstrap**: Recovery scans nodes from `graph.nodes`. If nodes are dynamic, how does bootstrap discover them? (From `NodeStateStore.list_nodes()`?)

### What to investigate

Read these files in `src/modex_graph/`:
- `node_factory.py` — NodeFactory ABC + DefaultNodeFactory
- `graph.py` — Graph.compile(), CompiledGraph
- `spec_compiler.py` — GraphSpecCompiler
- `scheduler/linear.py` — _handle_linear_dispatch, validate_dispatch_target usage
- `scheduler/parallel.py` — _handle_dispatch, validate_dispatch_target usage
- `scheduler/_dispatch_utils.py` — validate_dispatch_target (topology check)
- `scheduler/bootstrap.py` — node discovery
- `persistence/node_state_store.py` — list_nodes() method
- `context.py` — GraphContext construction

### Success criteria

A clear answer: "modex_graph can/cannot support dynamic nodes with these specific changes: [list]." If it can, the changes should be small enough to not break existing static-graph usage. If it cannot, explain why and what the alternative is.
