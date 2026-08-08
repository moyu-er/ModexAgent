# ADR-0034: Parallel Scheduling Engine

Status: accepted (refined 2026-08-05). This ADR is the Phase c
realization of ADR-0033 D12 (parallel fan-out, instance-level state
isolation). It supplements ADR-0033 — it does not supersede it.
`LinearScheduler` (Phase a behavior) remains the default and is
unchanged.

**Implementation refinement (2026-08-05):** The continuous-scheduling
core (`asyncio.create_task` + `wait(FIRST_COMPLETED)`, ready-set,
reachability-based `ON_ALL_PREDS` readiness) shipped as designed. The
fork/merge + generation-based conflict detection layer
(D7 fork-based state isolation, D8 `WriteConflictDetector`,
D18 `GenerationWriteTracker`, D19 `CheckpointStore`) was removed
after implementation — agent workloads do not need MVCC-style
multi-write guards, and the three-store persistence layer
(`GraphInstanceStore` / `NodeStateStore` / `DeliverStore`) with full
state snapshots handles recovery without a separate scheduler
checkpoint. The current `ParallelScheduler` uses per-task context
shells that **share** `ctx.state` (no fork), an in-memory per-node
serial gate for `ON_RECEIVE` (FIFO queue while a node has an
in-flight instance), synchronous `max_iterations` check+increment
before the first await, and recovery via a deliver scan
(`_restore_from_recovery`). The decision sections below are updated
to reflect the current contract; the original fork/merge-based design
is preserved in the historical context for traceability. The
authoritative description lives in
`docs/design/graph-orchestration/distributed-persistence.md`.

## Context

ADR-0033 shipped Phase a: a sequential scheduler (`GraphEngine.run_async`
with a single `current` node pointer). Phase c (parallel fan-out,
multi-write detection, BSP) was explicitly deferred per D12. The API
surface — `Command(goto=list[Task])`, `Task(node, state)`,
`ReducerChannel`, `ctx.fork()` — was wired in Phase a but executes
sequentially.

Three problems with the Phase a scheduler:

1. **No parallel fan-out.** `Command(goto=list[Task])` executes tasks
   sequentially via `for task: await _execute_task(...)`. Multiple
   static edges from one source only route to the first match. Nodes
   with multiple predecessors cannot join — the engine walks a single
   `current` pointer.

2. **No fan-in / join semantics.** A node with multiple incoming edges
   has no way to wait for all predecessors. Conditional branches that
   skip one arm leave downstream joins deadlocked or prematurely fired.

3. **No dispatch abstraction.** Nodes cannot express "I want to send
   work to node X" outside of `NodeResult.transition` / `Command.goto`.
   Future requirements (modexctl CLI driving task dispatch, agents
   operating via subprocess) need a dispatch interface that is
   framework-controlled and potentially remote-callable.

Reference: langgraph 1.2.9 (installed in `.venv`) uses a Pregel-style
superstep loop with `asyncio.wait(FIRST_COMPLETED)`, channel-version-based
readiness (`_triggers`), and `NamedBarrierValue` for fan-in. This ADR
adapts those patterns to modex_graph's existing channel/reducer/fork
infrastructure without adopting langgraph's complexity (checkpoint
versioning, managed values, delta channels).

## Decision

### D1 — Scheduler ABC with two implementations

`Scheduler` is an ABC; `GraphEngine` delegates to it. Two implementations:

- **`LinearScheduler`** — the Phase a behavior, extracted as-is.
  Single-node pointer, deliver/submit routing (nodes call `deliver()`,
  scheduler reads the first recorded target as next node), no instance
  IDs, no dispatch interface, no state machine.
  Zero behavior change from ADR-0033 Phase a. **Default.** ReAct and all
  existing graph patterns use this with zero code changes.

- **`ParallelScheduler`** — the parallel scheduling engine.
  Ready-set driven, multi-instance, shared state with per-task context
  shells (D7), dispatch abstraction, trigger modes, reachability-based
  readiness. Opt-in via `Graph.compile(scheduler="parallel")`.

`Graph.compile(scheduler: str = "linear")` selects the scheduler. The
`CompiledGraph` carries the choice; `GraphEngine` reads it and delegates.

### D2 — Continuous scheduling execution (ParallelScheduler)

The scheduler uses **continuous (event-driven) scheduling** — no batch
barrier. Instances start the moment their dependencies are satisfied,
independent of other running instances. This eliminates head-of-line
blocking: a short task (10ms) completing no longer waits for a long task
(10s) in the same batch.

Reference: langgraph 1.2.9 uses BSP supersteps with an `after_tick()`
barrier. This ADR replaces the barrier with continuous scheduling +
shared state (D7). See "Rejected alternatives" for the BSP comparison.

Execution loop:

1. Launch all READY instances as independent `asyncio.Task`s (no
   `asyncio.gather` barrier). Each gets a per-task context shell that
   shares `ctx.state` (D7).
2. `await asyncio.wait(running, return_when=FIRST_COMPLETED)` — wait for
   **any** instance to complete, not all.
3. For each completed instance:
   a. `Node.run` has already called `complete_invocation` (persists
      full state snapshot, synchronous — no `await`). asyncio's
      single-thread model guarantees no interleaving.
   b. `submit` has already dispatched accumulated delivers (events 1/2).
   c. Drain the per-node `ON_RECEIVE` serial-gate queue (D3).
   d. Re-check pending `ON_ALL_PREDS` nodes (D4).
4. Newly-READY instances (from step 3b/3c/3d) are launched immediately in
   the next loop iteration — they do not wait for other running instances.
5. Repeat until no READY and no RUNNING instances remain (D10).

`max_iterations` is checked + incremented synchronously at the top of
`_execute_instance`, before the first `await` (D9).

### D3 — Trigger modes

Each node declares a trigger mode (default `ON_ALL_PREDS`; graph-level
default configurable):

- **`ON_ALL_PREDS`** — the node fires once after all *activated*
  predecessors have dispatched to it. "Activated" = a predecessor that
  actually executed and dispatched to this node (not merely declared via
  a static edge). If a conditional branch skips one arm, that arm's
  downstream is not activated and does not count.

- **`ON_RECEIVE`** — the node fires once *per* dispatch received. N
  predecessor dispatches → up to N executions (N independent instances).
  **Serial gate (added 2026-08-05):** if the target node already has an
  in-flight instance (DORMANT / READY / RUNNING), the dispatch queues
  in a per-node FIFO and fires when the in-flight instance completes.
  N dispatches to an `ON_RECEIVE` node → N serial executions, not N
  parallel ones. The queue is in-memory only and not persisted across
  crashes — see `distributed-persistence.md` §11 for the caution.

`ON_ALL_PREDS` groups dispatches by source: each source contributes one
dispatch per group. A group is complete when every activated source has
at least one dispatch in it. A complete group triggers one instance.

### D4 — Readiness judgment (reachability-based, ON_ALL_PREDS only)

Reachability BFS applies **only to `ON_ALL_PREDS` nodes**. `ON_RECEIVE`
nodes never check reachability — they execute immediately on dispatch
(per D3's "fire once per dispatch" semantics), subject only to the
per-node serial gate.

A `ON_ALL_PREDS` node N is ready when **both** are true:

1. Every activated predecessor has dispatched to N.
2. No active instance (PENDING ∪ READY ∪ RUNNING) can reach N via
   outgoing edges (BFS over static edges).

Condition 2 is conservative: if any active instance *might* eventually
dispatch to N (even indirectly), N waits. This prevents premature
firing when a long chain (A→E→F→D) is still running while D's direct
predecessor (B) has already completed.

**Re-check trigger:** When any instance completes, the scheduler
re-checks **all pending `ON_ALL_PREDS` nodes** (not just direct
successors). This is necessary because an instance completing may clear
a reachability path to a node it has no direct edge to — e.g., a
predecessor chose a different branch, making a previously-blocked join
node reachable-clear.

The re-check scope is bounded: it scans only the `ON_ALL_PREDS` pending
queue (typically 1-3 nodes), not all instances. The BFS itself is O(V+E)
over static graph edges (typically < 20 nodes).

### D5 — Dispatch interface

`ctx.dispatch(target: str, state_update: dict | None = None)` is the
framework-level dispatch primitive, called by `Node.run`'s `submit`
step. It:

- Records the target + payload for the scheduler's dispatch handler.
- Routes the deliver to the target node's `DeliverStore` via
  `coordinator.route_deliver(target, content, source_node, source_invocation_id)`.
- Updates the target's readiness state (ParallelScheduler: trigger
  mode + serial gate; LinearScheduler: records next target).

`target == GraphNode.END` skips `route_deliver` (END has no
`DeliverStore`). `target` with no registered store raises
`RoutingError`.

Nodes may also handle dispatch externally (e.g., via modexctl CLI)
through `GraphControlService`, which calls `coordinator.route_deliver`
directly with `source_node="__external__"`. Remote and local
dispatches are recorded identically in the per-node `DeliverStore`.

### D6 — Dispatch payload and state visibility

**Current contract (deliver-payload model):** The dispatch
`state_update` dict carries the deliver payload (plus `_source_node` /
`_source_inv_id` metadata), NOT a state delta. The downstream node
consumes the deliver via `coordinator.collect_consumable_delivers` +
`InputIntegrator`, which integrates the payload into `IntegratedInput`
passed to `execute()`. The node reads `ctx.state` uniformly for
shared graph state — there is no separate payload-to-state merge.

Shared state mutations happen via imperative `ctx.state.x = y` inside
`execute()`. The full snapshot is persisted on `complete_invocation`
(`ctx.state.checkpoint()` → `node_states.state_json`). There is no
channel-level merge, no `LastValue` / `ReducerChannel` fold, no
`InvalidUpdateError`.

**Historical context (preserved for traceability):** The original
Phase-c design folded `NodeResult.state_update` payloads into
`ctx.state` via channel semantics — `ON_ALL_PREDS` folded all
sources' payloads through `channel.update([v1, v2, ...])` with
`LastValue` multi-write guards and `ReducerChannel` folding;
`ON_RECEIVE` merged the triggering source's payload before each
execution. This was removed along with channels and `NodeResult`.
Downstream data now flows exclusively through the `DeliverStore`
consumption state machine, not through state deltas.

### D7 — Multi-instance model with shared state (per-task context shells)

**Current contract (2026-08-05 refinement):** Every node execution
creates an independent **instance** identified by
`{node_name}#{global_seq}`. Instances are immutable lifecycle objects:
DORMANT → READY → RUNNING → COMPLETED. No state resetting — loops
produce new instances (`body#0`, `body#1`, `body#2`, ...).

**No fork.** Each instance task uses its own **context shell**
(`exec_ctx = copy(ctx)`) but **shares `ctx.state`** — the shell does
not deep-copy the `GraphState`. All instances read and mutate the same
`GraphState` instance. Imperative mutations (`exec_ctx.state.x = y`)
are visible to all concurrent instances immediately. There is no
fork-based isolation, no merge-back step, no `NodeResult.state_update`
delta to fold.

This is a deliberate simplification from the original fork-based
design (preserved in the historical context below). Agent workloads
are typically serial or weakly concurrent; the fork/merge complexity
was not justified. The single-node fast path (D2) is now the only
path — there is no fork to skip.

**Per-task shell resets:** `exec_ctx.current_invocation = None` (each
task sets its own invocation in `Node.run`'s `begin_invocation`).
Everything else (state, runtime, user_data, coordinator) is shared
from the parent `ctx`.

**Historical context (preserved for traceability):** The original
Phase-c design specified a fork (deep copy) of `main_state` at
PENDING → READY, with `NodeResult.state_update` merging back on
completion via channel semantics and imperative mutations not
propagating. This was removed: fork was dropped, `NodeResult` was
removed (execute is async void), channels were removed (state is a
plain `BaseModel`), and the merge step was replaced by direct shared
mutation + full-snapshot persistence on `complete_invocation`.

### D8 — State merge semantics (removed; shared state + full snapshots)

**Current contract (2026-08-05 refinement):** There is no merge step.
The graph has one **main state** (`ctx.state`), shared across all
instances. Instances mutate it directly. Persistence is via full
snapshots: `complete_invocation` writes
`ctx.state.checkpoint()` (= `model_dump(mode="json")`) to
`node_states.state_json`; `suspend_invocation` does the same.
Recovery rebuilds via `model_validate(rebuilt_main_state)`.

There is no `WriteConflictDetector`, no `GenerationWriteTracker`, no
`InvalidUpdateError`. Concurrent writes to the same field are
serialized by asyncio's single-thread model — the synchronous
segments (lifecycle calls in `Node.run`, dispatch handling) do not
interleave. If business logic requires per-instance isolation, the
node author is responsible for managing it (e.g. per-instance
working fields keyed by invocation_id).

**Historical context (preserved for traceability):** The original
Phase-c design specified generation-based conflict detection —
`LastValue` raised `InvalidUpdateError` on same-generation
multi-write, `ReducerChannel` folded, a `GenerationWriteTracker`
tracked `concurrent_versions` sets across generations. This entire
layer was removed along with channels and fork/merge. See
`distributed-persistence.md` §15 for the full removed-concepts list.

The `commit + apply_state_update + advance + complete` atomic
synchronous segment (no `await` between steps) is no longer relevant
— there is no commit, no version advance, no conflict check. The
synchronous segment that remains is `Node.run`'s lifecycle calls
(`complete_invocation` writes the snapshot atomically; asyncio
guarantees no interleaving).

### D9 — Iteration counting

`max_iterations` counts every node instance execution. Parallel execution
of N instances consumes N iterations. Exceeding the limit raises
`GraphRecursionError`. This preserves the Phase a safety-net semantics.

**Synchronous reservation (current contract):** The check + increment
runs as a synchronous code segment at the top of `_execute_instance`,
before the first `await` (`before_node`):

```python
if self._iteration_count >= self.graph.max_iterations:
    raise GraphRecursionError(...)
self._iteration_count += 1
```

asyncio's single-thread model guarantees that even if N instances are
READY simultaneously, their `_execute_instance` calls enter the check
serially — `iteration_count` is accurate and the limit is enforced
without races. No lock is needed.

**Recovery derivation:** On recovery, `_iteration_count` is derived as
`len(node_state_store.query_all({InvocationStatus.COMPLETED}))` — the
count of COMPLETED invocations in the version chain. It is not
persisted as a bookkeeping field.

### D10 — Termination

The graph terminates when `ready` is empty AND `active` is empty — no
pending, ready, or running instances remain.

`GraphNode.END` is a sentinel (not a real node — no `execute`). The
scheduler maintains a set of "dispatch-to-END source instances." When
all activated END sources have completed, the graph terminates. END has
implicit `ON_ALL_PREDS` semantics — it waits for all branches.

Nodes with outgoing edges that choose not to dispatch are legal (silent
skip). Their downstream remains DORMANT and does not block termination.

### D11 — Compile-time validation

`Graph.compile(scheduler="parallel")` adds two validations beyond Phase a:

1. **START reachability**: every node must be reachable from START.
2. **END reachability**: every node must have a path to END.

These ensure the graph is closed — no dangling nodes that could confuse
the reachability-based readiness judgment.

### D12 — Routing model (deliver/submit, shared by both schedulers)

**Current contract (2026-08-05 refinement):** Routing is deliver-only
(per ADR-0033 D6). Nodes call `deliver(content, next_node, ctx)`
during `execute()`; `submit` dispatches each deliver group via
`ctx.dispatch(target, state_update={"delivered": payload, ...})`.
The scheduler's dispatch handler records the target and routes the
deliver to the target node's `DeliverStore` via
`coordinator.route_deliver(...)`.

- `LinearScheduler` reads the first recorded target as the next node
  (sequential).
- `ParallelScheduler` resolves the target's trigger mode
  (`ON_ALL_PREDS` → pending queue + reachability check;
  `ON_RECEIVE` → fire immediately, subject to the per-node serial
  gate per D3).

`next_node=None` resolves via graph topology (default edge / single
downstream / END). `next_node=GraphNode.END` skips `route_deliver`.
A node that produces no delivers and has no default downstream edge
raises `RoutingError`.

**Historical context (preserved for traceability):** The original
Phase-c design specified a two-layer routing model —
`Command.goto` (explicit override) + `transition: str` (static-edge
lookup) — and removed `route_fn` / `add_conditional_edges`. The
further refinement replaced `Command` / `transition` / `NodeResult`
entirely with the deliver/submit model. `Command.goto=list[str]` and
`Command.goto=list[Task]` fan-out forms are gone; fan-out is now
expressed by calling `deliver()` multiple times with different
`next_node` targets during a single `execute()`. The `pending` queue
concept is gone — pending dispatches live in the per-target
`DeliverStore` and the scheduler's `_pending_dispatches` map,
rebuilt from PENDING delivers on recovery.

### D13 — Error handling under continuous scheduling

- **Node exception (non-`GraphBubbleUp`)**: the failing instance's
  `asyncio.Task` raises. The scheduler cancels all remaining running
  tasks and propagates the exception. Unlike the previous batch model
  (where `asyncio.gather` handled cancellation automatically), the
  scheduler explicitly cancels running tasks before propagating.
- **`GraphInterrupt`**: the first interrupt propagates immediately; all
  running tasks are cancelled. Same as Phase a — the engine never
  swallows `GraphBubbleUp`.
- **`InvocationStateError` (CAS failure)**: raised by strict lifecycle
  methods (`complete_invocation` / `suspend_invocation` /
  `cancel_invocation`) when the `node_states` row is already terminal
  or suspended. Propagates as above. Since lifecycle calls are
  synchronous (no `await`), the error is raised atomically. See
  `distributed-persistence.md` §4.5 for CAS semantics.

### D14 — Concurrency safety for hooks and emit

`before_node` / `after_node` / `emit` may be called concurrently from
parallel instances. Implementations (e.g., `ReactGraphRuntime`) must be
concurrency-safe. If audit reveals unsafe implementations, they should
be fixed or serialized with a lock — but this is an implementation concern,
not an architectural decision.

### D15 — Dispatch and execute lifecycle decoupling

Dispatch takes effect immediately when `ctx.dispatch` is called — the
target's state machine updates right away, even if the source's
`execute` has not returned yet. This supports the modexctl pattern: an
agent inside `execute` dispatches via CLI, the downstream begins
executing, and `execute` eventually returns.

`execute` returning marks the instance COMPLETED (via
`Node.run`'s `complete_invocation` call, which persists the full
state snapshot). The `submit` step inside `Node.run` dispatches any
accumulated delivers (from `deliver()` calls during `execute`).
After COMPLETED, the instance accepts no further dispatches (unless
it is re-activated by a loop, which creates a new instance).

### D16 — modexctl remote dispatch

The dispatch interface is designed to be callable over IPC. The current
`ctx.dispatch` is a Python method; a future IPC adapter (HTTP / Unix
socket / stdin) exposes the same `(target, state_update)` contract to
the modexctl CLI. The scheduler treats remote and local dispatches
identically — both produce dispatch events recorded in the same store.

This ADR records the interface contract (`dispatch(target, state_update)`)
as stable. The specific IPC transport is an implementation detail.

### D17 — Scheduling event model

The continuous scheduler is driven by three event types. All scheduling
decisions are centralized in a `_schedule()` method — nodes only dispatch,
the framework decides everything else.

**Event 1 — dispatch to ON_RECEIVE node X:**
- If X has no in-flight instance: create instance → per-task context
  shell (shares `ctx.state`, D7) → `create_task` immediately. No
  reachability check. The node's business logic decides how to handle
  the payload.
- If X has an in-flight instance: queue in per-node FIFO (serial gate,
  D3). Fire on completion of the in-flight instance.

**Event 2 — dispatch to ON_ALL_PREDS node X:**
- Record pending dispatch in the per-target queue.
- `_try_fire(X)`: (a) all activated sources dispatched? (b) reachability
  clear (D4)?
  - Yes → `create_task` (per-task context shell shares `ctx.state`, D7).
  - No → stays in pending queue.

**Event 3 — instance completes:**
- `Node.run`'s `complete_invocation` persists the full state snapshot
  to `node_states.state_json` (synchronous, no `await`).
- `submit` has already dispatched accumulated delivers (events 1 or 2)
  inside `Node.run`, before `complete_invocation`.
- Drain the per-node `ON_RECEIVE` serial-gate queue (D3) — fire the
  next queued dispatch if any.
- Re-check all pending ON_ALL_PREDS nodes (D4): an instance completing
  may clear a reachability path to a node it has no direct edge to.

This event model replaces the previous batch loop (`gather` → merge →
recheck). The `_schedule()` method is the single entry point for all
scheduling decisions.

### D18 — Conflict detection (removed)

**Current contract (2026-08-05 refinement):** There is no
`WriteConflictDetector` ABC, no `GenerationWriteTracker`, no
`InvalidUpdateError`. Concurrent writes to the same `ctx.state` field
are serialized by asyncio's single-thread model — the synchronous
segments (lifecycle calls in `Node.run`, dispatch handling) do not
interleave. The original conflict-detection layer was removed along
with fork/merge and channels. See D7 (shared state) and D8 (removed
merge semantics).

**Historical context (preserved for traceability):** The original
Phase-c design abstracted conflict detection behind an ABC to allow
future strategies (optimistic retry, custom resolution, distributed
detection). The default `GenerationWriteTracker` tracked a
`dict[int, _Generation]` where `_Generation` held `written_fields:
set[str]` and `pending_count: int`, one generation per `fork_version`.
All methods were synchronous (no `await`); the caller invoked
`commit + apply_state_update + advance + complete` as one atomic
synchronous segment. This entire layer was removed when fork/merge
was dropped — without forked state, there are no concurrent writers
to the same field to detect.

### D19 — Recovery (three-store, deliver scan)

**Current contract (2026-08-05 refinement):** There is no
`CheckpointStore` ABC, no `CheckpointData`, no async checkpoint after
each merge. Recovery is via the three-store persistence layer
(`GraphInstanceStore` / `NodeStateStore` / `DeliverStore`) with full
state snapshots, orchestrated by `GraphPersistenceCoordinator.load_for_recovery()`
(see `distributed-persistence.md` §6.5 and §10).

`ParallelScheduler._restore_from_recovery(ctx, recovery)` runs at the
top of `run_async`:

1. `recovery = ctx.coordinator.load_for_recovery()` — returns
   `RecoveryContext` with `metadata`, `node_states` (per-node latest
   invocation), and `rebuilt_main_state` (single newest snapshot per
   node, merged in Snowflake-time order).
2. Restore state: `ctx.state = type(ctx.state).model_validate(recovery.rebuilt_main_state)`
   (if prior state exists).
3. Derive `iteration_count` from `node_state_store.query_all({COMPLETED})`
   — the count of COMPLETED invocations.
4. Reset `instance_seq = 0` (in-memory temporary).
5. `_redispatch_from_recovery(recovery)` — status-based re-dispatch
   (CRASHED nodes re-dispatched, suspended RUNNING resumed). No
   "COMPLETED + delivers" shortcut.
6. `_rebuild_pending_from_delivers(ctx, recovery)` — scan ALL nodes'
   deliver stores for PENDING delivers (unconditional). For each
   target, resolve trigger mode: `ON_ALL_PREDS` → pending queue;
   `ON_RECEIVE` → fire if no in-flight instance, else the running
   instance consumes via `collect_consumable_delivers`.
7. `_recheck_pending()` — fire any ready `ON_ALL_PREDS` nodes.
8. Fresh start: if nothing was recovered and no prior invocations
   exist, create the entry instance.

`LinearScheduler` recovery is 4 lines: `load_for_recovery()` →
`model_validate(rebuilt_main_state)` → sequential loop from
`entry_node`. Resume routing is the graph author's concern
(`state.resume_target`).

**What was removed:** The original `CheckpointStore` ABC +
`CheckpointData` (frozen snapshot of scheduler + state) + async
checkpoint after each merge + `load_latest` resume path. The
"crash recovery is not yet wired" deferral note is resolved —
recovery is fully wired via the deliver scan. The
`ConcurrentWriteTracker` state was never persisted (it was a runtime
safety mechanism); with the tracker removed, this is moot.

**Bootstrap convergence (post-ADR):** Both `LinearScheduler` and
`ParallelScheduler` now share a unified `bootstrap(ctx, graph)` entry
point (`scheduler/bootstrap.py`) that wraps the recovery steps above.
The named methods (`_restore_from_recovery`, `_redispatch_from_recovery`,
`_rebuild_pending_from_delivers`) were consolidated into `bootstrap` +
`_recheck_pending`. See `src/modex_graph/AGENTS.md` "Scheduling
Convergence" section.

### D20 — Extensibility seams

The continuous scheduler retains ABC-first seams where they still
apply:

| Seam | Status | Notes |
|------|--------|-------|
| Trigger modes | `NodeTrigger` enum | `ON_ALL_PREDS` / `ON_RECEIVE` with serial gate |
| Reachability | (concrete class) | BFS over static edges; abstracted when a second policy emerges (rule 6) |
| Persistence | `CoordinatorFactory` ABC | `NullCoordinatorFactory` default; business layer substitutes SQLite factory |
| State stores | `GraphInstanceStore` / `NodeStateStore` / `DeliverStore` ABCs | Null / InMemory / Sqlite each |

**Removed seams:** `BaseChannel` / `LastValue` / `ReducerChannel`
(channel layer deleted), `WriteConflictDetector` /
`GenerationWriteTracker` (conflict detection deleted),
`CheckpointStore` / `CheckpointData` (replaced by three-store
recovery), `DispatchStore` (replaced by `DeliverStore` consumption
state machine). These were speculative seams without a second real
use case; per ADR-0007 they were removed rather than retained as
dead API surface.

Reachability is intentionally a concrete method, not an ABC — per
rule 6 ("one adapter is hypothetical; two make a real seam"). It
will be abstracted when a second policy is needed.

## Consequences

### Positive

- **No head-of-line blocking.** A short task (10ms) completing no
  longer waits for a long task (10s) in the same batch. Instances
  start the moment their dependencies are satisfied. This is the
  primary advantage over langgraph's BSP model.
- **Parallel fan-out works via deliver.** Nodes call `deliver()` with
  multiple targets during a single `execute()`; the framework
  dispatches each group concurrently.
- **Simple graphs pay no tax.** `LinearScheduler` is unchanged; ReAct
  and existing patterns are zero-impact.
- **Fan-in is correct.** Reachability-based readiness (`ON_ALL_PREDS`
  only) prevents premature joins and deadlocks from skipped branches.
- **Dispatch is a first-class abstraction.** Enables modexctl-driven
  workflows and future remote dispatch.
- **Multi-instance model** eliminates state-reset bugs in loops and
  `ON_RECEIVE` multi-trigger scenarios.
- **Crash recovery via deliver scan.** No separate checkpoint store —
  the three-store layer (`node_states` + `deliver_states`) carries
  all recovery state. `load_for_recovery` rebuilds state from the
  single newest snapshot per node and re-dispatches from PENDING
  delivers.
- **Synchronous `max_iterations` reservation.** Check + increment
  before the first `await` — race-free under asyncio single-thread.

### Negative

- **`ParallelScheduler` complexity.** State machine, reachability
  BFS, instance tracking, per-node serial gate — significant
  implementation surface. Mitigated by being opt-in and sharing
  `LinearScheduler`'s test coverage for simple graphs.
- **`ON_RECEIVE` serial gate is in-memory only.** The per-node FIFO
  queue is not persisted across crashes. Queued (unfired) dispatches
  are lost on crash; their delivers are PENDING and re-scanned on
  recovery, but queue order is not preserved. `ON_RECEIVE` is marked
  "use cautiously" — most nodes should use `ON_ALL_PREDS` (default).
- **Shared state means no per-instance isolation.** Concurrent
  instances mutate the same `ctx.state`. If business logic needs
  per-instance working state, the node author must manage it
  (e.g. per-invocation fields). The original fork-based isolation
  was removed as over-engineering for agent workloads.
- **`route_fn` / `add_conditional_edges` removal is a breaking
  change.** Any external code using it breaks. No internal code uses
  it (verified).
- **`NodeResult` / `Command` / `Task` removal is a breaking change.**
  Any code returning `NodeResult(transition=...)` or
  `Command(goto=...)` must change to `deliver()` / `submit()`. No
  internal code uses the old forms (verified).

### Rejected alternatives

- **BSP supersteps (langgraph model).** langgraph 1.2.9 uses BSP
  with an `after_tick()` barrier — channel updates from step N are
  only visible in step N+1. The `asyncio.wait(FIRST_COMPLETED)` in
  its runner is for streaming/error-cancel, not continuous
  scheduling. BSP is simpler (step immutability guarantees
  consistency) but imposes the head-of-line blocking tax. This ADR
  chooses continuous scheduling to eliminate that tax.
- **Single scheduler with fast-path optimization.** Considered (one
  `ParallelScheduler` with internal "if single node, skip instance"
  branches). Rejected — scatters complexity across the hot path;
  two clean implementations are simpler than one with conditional
  branches.
- **NamedBarrierValue channel for fan-in (langgraph pattern).**
  Considered. Rejected — modex_graph's reachability-based readiness
  judgment achieves the same join semantics without a new channel
  type per join node. (The channel layer itself was subsequently
  removed.)
- **Push-based dispatch only (no `ctx.dispatch`).** Considered
  (nodes always return a result, framework always computes routes).
  Rejected — cannot support modexctl remote dispatch or complex
  business-driven routing.
- **Pure pull-based dispatch (nodes read from a mailbox).**
  Considered. Rejected — the framework still needs to know dispatch
  happened (for readiness). Push with optional pull (node reads
  `ctx.state` which already contains shared mutations) is simpler.
- **Fork/merge + generation-based conflict detection (original
  Phase-c design).** Specified and partially implemented, then
  removed. Fork deep-copied `GraphState` per instance; merge folded
  `NodeResult.state_update` back via channel semantics;
  `GenerationWriteTracker` detected same-generation multi-writes.
  Rejected as over-engineering — agent workloads are serial or
  weakly concurrent, and the fork/merge complexity was not
  justified. Replaced by shared state + per-task context shells +
  full-snapshot persistence.
- **MVCC with version tree.** Considered during design (per-snapshot
  version chain, per-field version tracking). Rejected as
  over-engineering — the generation-based `set[str]` approach
  achieved the same safety guarantee with far less complexity. A
  full version tree is only needed if future requirements demand
  stale-read detection or multi-version reads, which the current
  DAG model does not. (The generation-based approach itself was
  subsequently removed.)

## Relationship to ADR-0033

This ADR realizes ADR-0033 D12 Phase c items:

| D12 row | ADR-0034 decision |
|---|---|
| MapReduce (fan-out → fan-in), parallel | D2, D3, D4, D5 |
| Parallel fan-out with multi-write detection | D7 (shared state), D8 (removed) — multi-write detection removed; concurrent writes serialized by asyncio single-thread |
| Cooperative shutdown (`GraphDrained`) | Deferred (exception class exists; wiring is future work) |
| Subroutine / Graph-of-graphs | Deferred (Graph-is-a-Node wiring exists; exercising is future work) |

ADR-0033 D1 Phase c item 6 ("BSP vs keep sequential + opt-in
parallel") is resolved here: **keep sequential as default
(`LinearScheduler`), parallel as opt-in (`ParallelScheduler`)**, with
a continuous scheduling model (not BSP) for the parallel path.
