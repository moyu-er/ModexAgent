# modex_graph

Standalone typed graph engine — sync/async dual mode, Pydantic state, pluggable scheduler (`LinearScheduler` / `ParallelScheduler`). Framework-agnostic: no `modex_agent` import (enforced by architecture guard test at `tests/architecture/test_modex_graph_isolation.py`).

> See `docs/adr/0033-generalized-graph-engine.md` and `docs/adr/0034-parallel-scheduling-engine.md` for the authoritative design.

## Scheduling Convergence

Normal execution, pause recovery, and crash recovery all rely on the same `node/deliver` mechanism. There is no separate recovery engine or recovery state machine.

### Unified entry: `bootstrap`

`scheduler/bootstrap.py:bootstrap(ctx, graph) -> list[str]` is the single entry point both schedulers call at the top of `run_async`. It queries the persistence store and derives seed node names:

- **Fresh start** (no prior invocations): returns `[graph.entry_node]`.
- **Recovery** (prior invocations exist): returns nodes needing re-execution — CRASHED / orphan RUNNING invocations + nodes with PENDING delivers — ordered topologically (BFS from `entry_node`).

Side effects: restores `ctx.state` from the newest snapshot (via `coordinator.rebuild_main_state`), auto-promotes CONSUMED_PENDING delivers whose consuming invocation is COMPLETED (crash between `mark_consumed` and `promote_delivers`).

`bootstrap` does NOT create instances or mark anything READY. The scheduler receives the seed list and decides how to use it (`LinearScheduler` takes `seeds[0]`; `ParallelScheduler` creates instances for re-execute and fresh-start seeds, while PENDING-deliver seeds are discovered by `_recheck_pending`'s store scan).

### Scheduler: no fresh/recovery distinction

`LinearScheduler.run_async` and `ParallelScheduler.run_async` both call `bootstrap` at the top, then run the normal scheduling loop. The scheduler does not know or care whether the seeds came from a fresh start or a recovery — it just executes them.

### Node.run: local recovery via version chain

`Node.run()` lifecycle: `load_latest → begin_invocation → integrate → execute → submit → complete`.

- `load_latest(node_id)` — read the latest invocation record from the store. Idempotent (read-only).
- `begin_invocation(node_id)` — create a new invocation record with `version = max(existing) + 1`. Continuous increment, no reset, no recovery marker. A crashed v=3 becomes v=4 on retry, identical to a normal v=4.
- `integrate` — `collect_consumable_delivers` + `mark_consumed`. Idempotent (prevents duplicate consumption).
- `complete_invocation` — mark COMPLETED + `promote_delivers`.

Orphan cleanup: `begin_invocation` marks a prior non-suspended RUNNING as CRASHED (the node was interrupted without graceful shutdown).

### Deliver admission: two paths, unified store rescan

- `_handle_dispatch` — live in-memory fast path: when a node delivers during `execute`, the dispatch handler updates in-memory queues and creates instances. This is the normal-execution path.
- `_recheck_pending` — persisted-store rescan: each scheduler loop iteration, scan all nodes' deliver stores for PENDING delivers and fire instances based on trigger (ON_RECEIVE / ON_ALL_PREDS). This covers both recovery seeds and any live delivers not caught by the fast path.

Both paths coexist; `_recheck_pending` is the unified rescan that makes recovery work without a separate admission path.

### Persistence strategy tradeoff

| Strategy | Stores | Persists | Recovery |
|----------|--------|----------|----------|
| Null | `NullNodeStateStore`, `NullDeliverStore`, `NullGraphInstanceStore` | No | Not supported (by design — for ReAct and tests) |
| InMemory | `InMemoryNodeStateStore`, `InMemoryDeliverStore`, `InMemoryGraphInstanceStore` | No (process-local) | Not supported (data lost on restart) |
| SQLite | `SqliteNodeStateStore`, `SqliteDeliverStore`, `SqliteGraphInstanceStore` | Yes | Supported — `load_latest` + `collect_consumable_delivers` idempotent restoration |

All three implement the same `NodeStateStore` / `DeliverStore` / `GraphInstanceStore` ABCs. `bootstrap` on Null/InMemory stores naturally returns `[entry_node]` (no prior invocations → fresh start).

## State Ownership

`ctx.state` is framework-managed. Nodes read it but do not write to framework fields (`resume_target`, `result` on `DefaultGraphState`). Framework code (checkpoint, recovery, bootstrap) writes these fields.

`ctx.state.node_scratch: dict[str, Any]` provides per-node working state. Each node writes only to `node_scratch[self.node_id]` — key separation provides natural isolation without fork/clone. Reading other nodes' scratch is allowed but discouraged; prefer receiving data via deliver/IntegratedInput.

Cross-node data flows through deliver → DeliverStore → IntegratedInput → execute(). The `copy(ctx)` pattern was removed — ParallelScheduler passes ctx directly.

The ON_RECEIVE serial gate guarantees the same Node object never executes concurrently — execution-specific mutable attributes (`_pending_delivers`, `_submit_result`, `_graph_ref`) on Node are safe under this invariant.

## Key Files

| File | Description |
|------|-------------|
| `node.py` | `Node[S]` ABC — `run()` lifecycle, `deliver()`, `_resolve_default_target()` |
| `graph.py` | `Graph[S]` — `compile()` produces `CompiledGraph[S]`; START/END as registered Node instances |
| `engine.py` | `GraphEngine[S]` — thin delegator, selects `LinearScheduler` or `ParallelScheduler` |
| `scheduler/linear.py` | `LinearScheduler` — sequential execution, deliver-only routing |
| `scheduler/parallel.py` | `ParallelScheduler` — multi-instance, ON_RECEIVE / ON_ALL_PREDS triggers, ctx passed directly (no copy) |
| `scheduler/bootstrap.py` | `bootstrap(ctx, graph)` — unified entry point (fresh + recovery) |
| `scheduler/base.py` | `Scheduler[S]` ABC |
| `scheduler/instance.py` | `NodeInstance` — in-memory instance state (DORMANT/READY/RUNNING/COMPLETED/CRASHED) |
| `scheduler/_dispatch_utils.py` | Shared dispatch-handler helpers — topology validation + deliver routing (converged from both schedulers) |
| `node_factory.py` | `NodeFactory` ABC + `DefaultNodeFactory` registry |
| `nodes/function_node.py` | `FunctionNode` — wraps sync/async function as a Node |
| `nodes/delay_node.py` | `DelayNode` — async delay / rate-limiting node |
| `nodes/human_input_node.py` | `HumanInputNode` — suspends for human input via `GraphInterrupt` |
| `nodes/graph_as_node.py` | `GraphAsNode` — embeds a sub-graph as a Node |
| `compiled_graph.py` | `CompiledGraph[S]` — compiled graph with nodes, edges, entry_node |
| `context.py` | `GraphContext[S]` — runtime, coordinator, dispatch handler, user_input, current_invocation (invocation-local); state is framework-managed with per-node node_scratch |
| `persistence/node_state_store.py` | `NodeStateStore` ABC + Null/InMemory/Sqlite implementations, version chain + CAS |
| `persistence/deliver_store.py` | `DeliverStore` ABC + Null/InMemory/Sqlite, accumulate/query_consumable/mark_consumed/promote |
| `persistence/persistence_coordinator.py` | `GraphPersistenceCoordinator` — route_deliver, collect_consumable_delivers, rebuild_main_state; single emission seam for node-level `GraphOutput` events (`emit_output` / `set_output_adapter` / `drain_output_events`) |
| `persistence/graph_metadata.py` | `GraphMetadata`, `NodeInvocationRecord`, `InvocationContext` |
| `persistence/instance_store.py` | `GraphInstanceStore` ABC + Null/InMemory/Sqlite, `node_id_map` JSON column |
| `integration.py` | `GraphPayload` (static-graph deliver content), `IntegratedPayload`, `IntegratedInput` |
| `nodes/start_node.py` | `StartNode` — reads `ctx.user_input`, delivers to downstream |
| `nodes/end_node.py` | `EndNode` — aggregates delivers to `ctx.state.result` (ON_ALL_PREDS trigger) |
| `state/state.py` | `GraphState` ABC (frozen=False, mutable, node_scratch for per-node working state) |
| `state/default_state.py` | `DefaultGraphState` — `result: list[GraphPayload]` for static graphs |
| `output_adapter.py` | `GraphOutputAdapter` ABC, `GraphOutput`, `GraphOutputKind` (terminal + node-level events) |
| `constants.py` | `GraphNode` (START/END), `InvocationStatus`, `NodeTrigger`, `SchedulerKind`, `GraphInstanceStatus` |
| `spec.py` | `GraphSpec`, `NodeSpec`, `EdgeSpec` |
| `spec_compiler.py` | `GraphSpecCompiler` — `compile()` always creates START/END, `validate()` runs TopologyValidator |
| `topology_validator.py` | `TopologyValidator` — cycle detection, reachability |
| `spec_store.py` | `GraphSpecStore` ABC + Null/InMemory/Sqlite (upsert, `list_records`, `get_by_id`) |
| `spec_record.py` | `GraphSpecRecord` — REST-friendly metadata view |
| `utils/id.py` | `generate_id(prefix)` — opencode-style sortable short ID |

## Architecture Boundary

`modex_graph` must NOT import `modex_agent`. Enforced by `tests/architecture/test_modex_graph_isolation.py`. The graph engine is framework-agnostic; business wiring lives in `modex_agent` and `examples/bot_project/`.
