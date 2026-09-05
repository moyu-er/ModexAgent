# modex_graph

Standalone typed graph engine — sync/async dual mode, Pydantic state, pluggable scheduler (`LinearScheduler` / `ParallelScheduler`). Framework-agnostic: no `modex_agent` import (enforced by architecture guard test at `tests/architecture/test_modex_graph_isolation.py`).

> See `docs/adr/0033-generalized-graph-engine.md` and `docs/adr/0034-parallel-scheduling-engine.md` for the authoritative design.

## Scheduling Convergence

Normal execution, pause recovery, and crash recovery all rely on the same `node/deliver` mechanism. There is no separate recovery engine or recovery state machine.

### Unified entry: `bootstrap`

`scheduler/bootstrap.py:bootstrap(ctx, graph, *, mode: BootstrapMode) -> list[str]` is the single entry point both schedulers call at the top of `run_async`. The caller passes an explicit keyword-only `mode` (no default — convergence rule 15):

- **`BootstrapMode.FRESH`** — new run or re-invoke (`start_run` / `start_invoke` / subgraph `execute`). Zero scanning; returns `[graph.entry_node]` immediately. No auto-promote, no seed derivation, no instance-status guesswork.
- **`BootstrapMode.RECOVERY`** — crash/pause recovery (`recover_crashed` / classified orphan pickup / `resume` from PAUSED). Uses explicit `graph_run_version` membership. For a non-null run version, a missing or mismatched latest START invocation returns `[entry_node]` with `reached_end=False` before promotion/scanning, even if an older run completed. Otherwise, only matching latest node records participate in completed-deliver promotion and invocation seed derivation. Seeds come from `CRASHED` / orphan-`RUNNING` / `CANCELED` invocations and the unchanged `PENDING` deliver scan, ordered by BFS. Canceled invocations reconsume `CONSUMED_PENDING` inputs; no-input cancellations also re-execute. When no seeds remain, matching invocation history yields `[]`; no matching history (including Null persistence) yields entry. New PENDING input may activate an already-completed node.

**Logical-run membership:** FRESH admission writes its original graph invocation version into `GraphMetadata.attrs[GRAPH_RUN_VERSION_KEY]` (`graph_run_version`); recovery attempts increment graph `version` but retain this attr. `ctx.graph_run_version`, `InvocationContext`, `NodeInvocationRecord`, and `GraphIORecord` carry the nullable membership value. Compare by exact equality: `None` identifies legacy/unscoped records and matches only `None`, not all runs. SQLite node/IO stores add nullable INTEGER columns, leaving old rows NULL; InMemory preserves the same model fields. `load_latest` still selects by node version, and the caller checks membership. Node/IO primary keys, including Snowflakes, have no run-boundary or ordering role. This membership does not add a run filter to deliver stores.

`bootstrap` does NOT restore `ctx.state` (the caller initializes it), does NOT create instances, and does NOT mark anything READY. The scheduler receives the seed list and decides how to use it (`LinearScheduler` takes `seeds[0]`; `ParallelScheduler` creates instances for re-execute and fresh-start seeds, while PENDING-deliver seeds are discovered by `_recheck_pending`'s store scan).

Bootstrap restores `ctx.reached_end` when END is a seed or no work remains after a matching-run END completed. The orchestrator reuses output only from matching-run latest I/O when that completed END is unchanged; it does not replay END just to rebuild `state.result`. Recovery I/O placeholders retain the same run's prior output, including across interrupted recovery attempts. Parallel re-execution seeds reserve their pending deliver IDs so the store scan does not schedule a duplicate invocation for newly supplied resume input.

### Immediate Pause And Stop

`GraphRunControl` is a per-execution, irreversible control handle. Both schedulers use `wait_for_tasks` to observe pause/stop while nodes are blocked, and `cancel_and_drain` to cancel their owned tasks once and await asynchronous cleanup. `wait_for_settlement` retains and shields cleanup through repeated caller cancellation, then propagates faults before delayed cancellation; the orchestrator uses this same boundary for finalization. Admission checks prevent ready tasks from entering a node after a request. `GraphDrained` reaches the caller only after owned work is drained; node/cleanup faults remain faults, not pauses. Cooperative asyncio cancellation cannot preempt synchronous blocking code or roll back external side effects.

The orchestrator owns graph status persistence and emission: persist `PAUSING` / `STOPPING` when requesting control, drain node cleanup/output, then publish `PAUSED` / `STOPPED`. The owner stays reserved through final status output; resume requires its exit. Resume uses the same graph instance, node identities, stores, and logical `graph_run_version`, but a new attempt/version and `GraphRunControl`, with `BootstrapMode.RECOVERY`. `GraphOutputKind.STATUS_CHANGED` (`graph_status_changed`) carries typed `GraphOutput.status: GraphInstanceStatus | None`; the coordinator's `emit_output(status=...)` supports that event. SQLite upgrades the status CHECK constraint while preserving all graph invocation versions and metadata. Abandoned `PAUSING` / `STOPPING` invocations finalize as `CRASHED`, like abandoned `RUNNING` invocations.

Resumed unfinished work, including agent/provider/tool work, is at-least-once: re-execution can repeat external effects. Nodes own idempotency; graph pause is neither a mid-call checkpoint nor a rollback. For ownership, restart/I/O semantics, or validation scope, read `docs/design/graph-orchestration/external-control.md`; live-provider and hard-kill validation are not claimed there.

### Scheduler: no fresh/recovery distinction

`LinearScheduler.run_async` and `ParallelScheduler.run_async` both call `bootstrap` at the top, then run the normal scheduling loop. The scheduler does not know or care whether the seeds came from a fresh start or a recovery — it just executes them.

**D8 disposition (accepted + documented):** `LinearScheduler` does not support external deliver admission (`deliver_to_node` / external `route_deliver`). Linear is the ReAct internal-flow scheduler (sequential, single-pointer); external deliver injection is a Parallel/bot-graph scenario. This is by design, not a gap — Linear's `_handle_linear_dispatch` records the first dispatch target as the next node and has no multi-source admission path. (Crash-window D8: accepted + documented.)

### Node.run: local recovery via version chain

`Node.run()` lifecycle: `begin_invocation → integrate → execute → complete_invocation → promote_staged_by_source → dispatch → promote_delivers`.

- `begin_invocation(node_id, graph_run_version=ctx.graph_run_version)` — create a record with node `version = max(existing) + 1` and explicit logical-run membership. Node versions keep increasing across fresh runs and retries; `graph_run_version` is neither the node version nor a retry flag. Orphan cleanup marks a prior `RUNNING` record `CRASHED` (there is no `suspended` state).
- `integrate` — `collect_consumable_delivers` (returns `PENDING` + `CONSUMED_PENDING`) + `mark_consumed`. Idempotent (prevents duplicate consumption).
- `execute` — `await self.execute(ctx, integrated_input)` (async void).
- `complete_invocation(invocation)` — mark `COMPLETED` (STRICT CAS; no state argument — `node_states` carries lifecycle + version-chain facts only, no `state_json`).
- `promote_staged_by_source` — make this node's `STAGED` outputs `PENDING` in their target deliver stores; returns affected target node IDs.
- `dispatch` — fire each affected target as a scheduling wakeup (`ctx.dispatch(target, state_update={})`); content flows through the store, not the dispatch payload.
- `promote_delivers` — advance this node's `CONSUMED_PENDING` inputs to `CONSUMED_COMPLETED`.

Exception handling: `GraphInterrupt` / other `GraphBubbleUp` / `asyncio.CancelledError` → `cancel_invocation` (terminal `CANCELED`) + re-raise; other `Exception` → `crash_invocation` + re-raise; `finally` → `finalize_invocation` (orphan `RUNNING` → `CRASHED` safety net). There is no node-level `suspend_invocation`; recovery is a fresh invocation that reconsumes consumable delivers.

### Deliver admission: two paths, unified store rescan

The deliver store is a four-state machine across stateful implementations (`STAGED → PENDING → CONSUMED_PENDING → CONSUMED_COMPLETED`; only `PENDING` and `CONSUMED_PENDING` are consumable; `NullDeliverStore` is a simple queue with no visibility gate). Admission uses two coexisting paths — this is by design (ticket 10 disposition), not a duplication to converge away:

- `_handle_dispatch` — live in-memory fast path (control plane): when a node delivers during `execute`, content is persisted as `STAGED` in the target's deliver store, then `promote_staged_by_source` makes it `PENDING` after the source completes. The dispatch handler fires the target as a scheduling wakeup (`state_update={}`) — it carries no data, only a readiness signal.
- `_recheck_pending` — persisted-store rescan (data plane): each scheduler loop iteration, scan all nodes' deliver stores for `PENDING` delivers and fire instances based on trigger (ON_RECEIVE / ON_ALL_PREDS). This covers both recovery seeds and any live delivers not caught by the fast path.

Dispatch = pure wakeup (control plane); store scan = sole data plane. The two coexist by design — converging them into one path would remove the live fast path. (Ticket 10: scan-merge / shared-snapshot / incremental-invalidation layer explicitly NOT done — bot-scale graphs are ~5 nodes, in-process SQLite index queries are microsecond-scale; revisit only at ~100 nodes or when the store backend is networked.)

### Persistence strategy tradeoff

| Strategy | Stores | Persists | Recovery |
|----------|--------|----------|----------|
| Null | `NullNodeStateStore`, `NullDeliverStore`, `NullGraphInstanceStore` | No | Not supported (by design — for ReAct and tests) |
| InMemory | `InMemoryNodeStateStore`, `InMemoryDeliverStore`, `InMemoryGraphInstanceStore` | No (process-local) | Not supported (data lost on restart) |
| SQLite | `SqliteNodeStateStore`, `SqliteDeliverStore`, `SqliteGraphInstanceStore` | Yes | Supported — `load_latest` + `collect_consumable_delivers` idempotent restoration |

All three implement the same `NodeStateStore` / `DeliverStore` / `GraphInstanceStore` ABCs. `bootstrap` with `mode=FRESH` returns `[entry_node]` regardless of store. InMemory supports pause/resume while its stores remain alive; Null stores have no invocation history and recovery falls back to `[entry_node]`.

## Trigger Model

**Stable trigger:** `NodeTrigger.ON_ALL_PREDS` (the default). A node waits
until every activated predecessor has dispatched at least once AND no active
instance can reach it via outgoing edges. One instance is then created
consuming all currently pending dispatches from the activated sources
(batch semantics: IntegratedInput may contain multiple payloads per source).
This is the recommended trigger for all production graphs.

**Deprecated / experimental trigger:** `NodeTrigger.ON_RECEIVE`. Each
dispatch creates a new instance immediately; reachability is NOT checked;
the per-node FIFO serial gate is in-memory only and not persisted across
crashes. `Graph.compile()` emits a `DeprecationWarning` when ON_RECEIVE is
used; `GraphSpec` (declarative API) rejects it entirely. Do not use
ON_RECEIVE in new production graphs.

## State Ownership

`ctx.state` is the per-run lifecycle workspace. Runtime isolation is the
contract: cross-node data flows through `deliver` → `DeliverStore` →
`IntegratedInput` → `execute()` only. Nodes do not read each other's
working state. Framework fields (`resume_target`, `result` on
`DefaultGraphState`) are written by framework nodes (START/END) and the
entry-node routing path, not by arbitrary business nodes.

`ctx.state.node_scratch: dict[str, Any]` is a **per-invocation working
region, never persisted**. Each node writes only to
`node_scratch[self.node_id]` — key separation provides natural isolation
without context copying. The `ctx.scratch` property provides a scoped
accessor: `ctx.scratch["key"] = value` resolves to
`ctx.state.node_scratch[current_node_id]["key"]` automatically. Reading
other nodes' scratch is PROHIBITED. Tests may inspect the backing
`node_scratch` dict to verify outcomes. Because scratch is never
persisted, a crash discards it — recovery reconsumes upstream delivers
and re-derives working state, it does not restore a checkpoint.

State is NOT restored from the store on recovery. The caller initializes
`ctx.state`; crash recovery is derived from invocation status plus the
four-state deliver admission path, not from a persisted business-state
snapshot (`node_states` carries lifecycle + version-chain facts only —
no `state_json`, no `suspended`; phase 07 retirement). The
`copy(ctx)` pattern was removed — `ParallelScheduler` passes `ctx`
directly.

The per-node serial gate guarantees the same Node object never executes
concurrently under ANY trigger mode — execution-specific mutable
attributes (`_pending_delivers`, `_submit_result`, `_graph_ref`) on Node
are safe under this invariant. `ON_ALL_PREDS` enforces this via
`_is_node_running(target)` in `_try_fire_on_all_preds`; `ON_RECEIVE`
enforces it via `_is_node_running(target)` in `_handle_dispatch`.

## Subgraph Composition

`CompiledGraph` is a `Node[S]` subclass — the "subgraph composition =
node implementation freedom" pattern (ADR-0033 D8). Any node can
internally build and run its own `GraphEngine` loop without the outer
graph knowing or caring. `CompiledGraph.execute(ctx, integrated_input)`
runs its own engine with `mode=FRESH`, sharing the parent context's
`state` / `runtime` / `user_data`; the `integrated_input` is ignored
(the subgraph manages its own input integration). The dispatch handler
is saved and restored so the inner scheduler does not clobber the outer
scheduler's routing; invocation identity is handled by the
ContextVar-based execution context (token reset in `Node.run()`).

The inner engine does not participate in the outer lifecycle. A
`GraphInterrupt` raised inside the subgraph `cancel_invocation`s the
current (innermost) node and propagates through the subgraph engine to
the parent — the engine never swallows `GraphBubbleUp` (interrupt
contract per ticket 07). There is no `suspend_invocation`; recovery is
a fresh re-invocation that reconsumes consumable delivers.

## Process Ownership (Phase 09)

`GraphMetadata.attrs: dict[str, int | str | None]` is a frozen-model
extension document with an isolated `default_factory=dict` — a typed
exception to the strict-field rule, following the `node_id_map`
precedent. It is the ownership/audit seam: business layers write
per-instance metadata without schema changes to `GraphMetadata`.
`GraphInstanceStore.update_attrs` is implemented by all three strategies
(Null no-op; InMemory and SQLite merge supplied keys into the latest
version only). Prior versions remain frozen as an audit trail, and
`begin_invocation` copies the latest attrs into the new version.

The stale-execution sweeper uses this seam. `ProcessIdentity` +
`ProcessRegistry` live in `modex_agent.runtime` (the graph engine stays
framework-agnostic — it only provides the `attrs` write surface). The
orchestrator writes `executor_process_id` into `attrs` on
`begin_invocation`; `StaleInstanceSweeper` (business layer,
`examples/bot_project/bot/service/stale_instance_sweeper.py`) loads
`RUNNING` / `PAUSING` / `STOPPING` instances, compares their executor against the alive-process
set from `ProcessRegistry.alive_process_ids()`, and marks stale ones
(absent/dead executor, or explicit `None`) `CRASHED` via
`update_status`. It marks `CRASHED` only — it does NOT trigger recovery
(recovery is explicit via `GraphRecoveryService`). Terminal-state attrs
are preserved as audit trail; the sweeper never touches terminal
instances.

## Key Files

| File | Description |
|------|-------------|
| `node.py` | `Node[S]` ABC — `run()` lifecycle, `deliver()`, `_resolve_default_target()` |
| `graph.py` | `Graph[S]` — `compile()` produces `CompiledGraph[S]`; START/END as registered Node instances |
| `engine.py` | `GraphEngine[S]` — thin delegator, selects `LinearScheduler` or `ParallelScheduler` |
| `scheduler/linear.py` | `LinearScheduler` — sequential execution, deliver-only routing |
| `scheduler/parallel.py` | `ParallelScheduler` — multi-instance, ON_RECEIVE / ON_ALL_PREDS triggers, ctx passed directly (no copy) |
| `scheduler/bootstrap.py` | `bootstrap(ctx, graph, *, mode: BootstrapMode)` + `BootstrapMode` (FRESH/RECOVERY) — unified entry point |
| `scheduler/base.py` | `Scheduler[S]` ABC |
| `run_control.py` | `GraphRunControl` - shared control-aware task wait and cancel/drain ownership |
| `scheduler/instance.py` | `NodeInstance` — in-memory instance state (DORMANT/READY/RUNNING/COMPLETED/CRASHED) |
| `scheduler/_dispatch_utils.py` | Shared dispatch-handler helpers — topology validation + deliver routing (converged from both schedulers) |
| `node_factory.py` | `NodeFactory` ABC + `DefaultNodeFactory` registry |
| `nodes/function_node.py` | `FunctionNode` — wraps sync/async function as a Node |
| `nodes/delay_node.py` | `DelayNode` — async delay / rate-limiting node |
| `nodes/human_input_node.py` | `HumanInputNode` — suspends for human input via `GraphInterrupt` (interrupts only when `IntegratedInput.payloads` is empty) |
| `compiled_graph.py` | `CompiledGraph[S]` — `Node[S]` subclass (subgraph composition = node implementation freedom); `execute` runs its own `GraphEngine` loop with `mode=FRESH`, sharing parent `ctx` |
| `context.py` | `GraphContext[S]` — runtime, coordinator, dispatch handler, user_input; state is framework-managed with per-node node_scratch; invocation identity via ContextVar (`execution_context.py`) |
| `persistence/node_state_store.py` | `NodeStateStore` ABC + Null/InMemory/Sqlite implementations, version chain + CAS; lifecycle facts only (no `state_json`/`suspended`) |
| `persistence/deliver_store.py` | `DeliverStore` ABC + Null/InMemory/Sqlite, four-state machine (`STAGED→PENDING→CONSUMED_PENDING→CONSUMED_COMPLETED`): `accumulate`/`promote_staged_by_source`/`query_consumable`/`mark_consumed`/`promote_consumed` |
| `persistence/persistence_coordinator.py` | `GraphPersistenceCoordinator` — `route_deliver`, `collect_consumable_delivers`, `load_for_recovery`; single emission seam for node-level `GraphOutput` events (`emit_output` / `set_output_adapter` / `drain_output_events`) |
| `persistence/graph_metadata.py` | `GraphMetadata` (identity + status + `version` + `node_id_map` + `attrs` extension seam), `NodeInvocationRecord` (lifecycle facts only), `InvocationContext` |
| `persistence/instance_store.py` | `GraphInstanceStore` ABC + Null/InMemory/Sqlite, `node_id_map` JSON column, `update_attrs` (phase 09 ownership seam) |
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
