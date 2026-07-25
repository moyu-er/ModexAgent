# ADR-0034: Parallel Scheduling Engine

Status: proposed. This ADR is the Phase c realization of ADR-0033 D12
(parallel fan-out, multi-write detection, instance-level state isolation).
It supplements ADR-0033 — it does not supersede it. ADR-0033 Phase a
(sequential scheduling, `LinearScheduler`) remains the default and is
unchanged.

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

- **`LinearScheduler`** — the current Phase a behavior, extracted as-is.
  Single-node pointer, `transition` + static edges + `Command.goto` routing,
  no fork, no instance IDs, no dispatch interface, no state machine.
  Zero behavior change from ADR-0033 Phase a. **Default.** ReAct and all
  existing graph patterns use this with zero code changes.

- **`ParallelScheduler`** — the new parallel scheduling engine.
  Ready-set driven, multi-instance, fork-based state isolation, dispatch
  abstraction, trigger modes, reachability-based readiness. Opt-in via
  `Graph.compile(scheduler="parallel")`.

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
fork-based isolation (D7) + generation-based conflict detection (D8/D18).
See "Rejected alternatives" for the BSP comparison.

Execution loop:

1. Launch all READY instances as independent `asyncio.Task`s (no
   `asyncio.gather` barrier). Each gets a forked state snapshot (D7).
2. `await asyncio.wait(running, return_when=FIRST_COMPLETED)` — wait for
   **any** instance to complete, not all.
3. For each completed instance:
   a. Atomically merge `state_update` to main state + conflict detection
      (D8). This merge is a synchronous code segment with no `await` —
      asyncio's single-thread model guarantees no interleaving.
   b. Compile `NodeResult.transition` / `Command.goto` into dispatch
      events (D5).
   c. Re-check pending `ON_ALL_PREDS` nodes (D4).
4. Newly-READY instances (from step 3b/3c) are launched immediately in
   the next loop iteration — they do not wait for other running instances.
5. Repeat until no READY and no RUNNING instances remain (D10).

Fast path: when only one instance is READY and none are RUNNING, skip
fork and execute directly on main state (zero overhead, equivalent to
LinearScheduler behavior).

`max_iterations` counts every node instance execution (D9).

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

`ON_ALL_PREDS` groups dispatches by source: each source contributes one
dispatch per group. A group is complete when every activated source has
at least one dispatch in it. A complete group triggers one instance.

### D4 — Readiness judgment (reachability-based, ON_ALL_PREDS only)

Reachability BFS applies **only to `ON_ALL_PREDS` nodes**. `ON_RECEIVE`
nodes never check reachability — they execute immediately on dispatch
(per D3's "fire once per dispatch" semantics).

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
framework-level dispatch primitive. It:

- Validates `target` is reachable via the current node's outgoing edges
  (edge whitelist — cannot dispatch to nodes without a declared edge).
- Records a dispatch event `(source_instance, target, payload)`.
- Updates the target's readiness state.
- Default implementation persists the event (for modexctl / crash recovery).

`NodeResult.transition` and `Command.goto` are declarative syntax sugar:
after `execute` returns, the scheduler compiles them into `ctx.dispatch`
calls. Both styles can coexist in the same `execute` call.

Nodes may also handle dispatch externally (e.g., via modexctl CLI) without
calling `ctx.dispatch` directly — the framework provides an IPC-equivalent
interface so remote dispatches are recorded identically.

### D6 — Dispatch payload and state visibility

The dispatch payload is the upstream's `NodeResult.state_update` (from
either `ctx.dispatch(state_update=...)` or `NodeResult.state_update`).
Payload may be `None` (empty) — in which case the downstream naturally
falls back to reading the shared `ctx.state` (B-degrades-to-A).

Payloads are merged into `ctx.state` via channel semantics:
- `ON_ALL_PREDS`: all activated sources' payloads are folded through
  `channel.update([v1, v2, ...])` — `LastValue` multi-write raises
  `InvalidUpdateError`; `ReducerChannel` folds.
- `ON_RECEIVE`: the triggering source's payload is merged into state
  before each execution.

The node reads `ctx.state` uniformly — no separate payload access API.

**Implementation note (payload fold deferral):** Compiled-dispatch payloads
(`NodeResult.state_update` routed via `transition`/`Command.goto`) are
effectively delivered — they merge into `main_state` at the source's
completion, and downstream instances fork from `main_state` which includes
the merged update. However, **manual `ctx.dispatch(target, state_update=X)`
payloads are recorded in the `DispatchEvent` audit log but NOT folded into
the target instance's state** — the payload reaches downstream only if the
same dict is also the source's `NodeResult.state_update`. Full D6 payload
folding (channel-level merge of multiple sources' payloads at group-fire
time) is deferred to a future phase. Nodes that need to pass data to
downstream should write it to `ctx.state` (via `state_update` or imperative
mutation) rather than relying on dispatch payloads.

### D7 — Multi-instance model with fork-based state isolation

Every node execution creates an independent **instance** identified by
`{node_name}#{global_seq}`. Instances are immutable lifecycle objects:
DORMANT → PENDING → READY → RUNNING → COMPLETED. No state resetting —
loops produce new instances (`body#0`, `body#1`, `body#2`, ...).

**Fork timing:** Each instance receives a **fork** (deep copy) of the
graph-level main state at the moment it transitions PENDING → READY
(D2). This captures the latest merged state — including all updates
from previously-completed instances. Instances that become READY
simultaneously (before any merge occurs) fork the same snapshot and
are *concurrent* (D8 conflict detection applies).

The instance reads/writes its fork freely. On completion, only
`NodeResult.state_update` merges back to the main state via channel
semantics. Imperative mutations (`ctx.state.x = y`) on the fork do
**not** propagate.

Single-node fast path: when only one instance is READY and none are
RUNNING, the fork is skipped — the instance operates directly on the
main state (zero overhead, equivalent to LinearScheduler behavior).

### D8 — State merge semantics (continuous merge + generation-based conflict detection)

The graph has one **main state** (the `GraphState` instance passed to
`run_async`). All completed instances' `state_update` values merge into
it. New instances fork from the current main state.

Under continuous scheduling (D2), there is no batch barrier. Instances
complete and merge one at a time. Conflict detection uses a
**generation** concept:

- A **generation** = all instances that forked the same main_state
  snapshot. When multiple instances become READY simultaneously (before
  any merge occurs), they share the same `fork_version` and belong to
  the same generation.
- When any instance completes and merges, `main_state_version`
  increments. Subsequent instances fork a new generation.
- **Same-generation writes** (two instances with the same `fork_version`
  writing the same `LastValue` field) = **conflict** →
  `InvalidUpdateError`.
- **Cross-generation writes** (a later instance writes a field that an
  earlier instance already wrote) = **sequential overwrite** — the
  later instance saw the earlier one's merged result. Not a conflict.

**Implementation note (cross-generation concurrency):** Under continuous
scheduling, a new instance can fork while an older-generation instance is
still RUNNING. These two instances are truly concurrent (the new instance
did NOT see the older one's merged result), even though they have different
`fork_version` values. To catch conflicts between them, `GenerationWriteTracker`
tracks a bidirectional `concurrent_versions` set per generation: when a new
generation registers while any older generation has `pending_count > 0`,
both generations record each other as concurrent. `commit` checks
`written_fields` of the committing generation AND all its
`concurrent_versions`. Generations are NOT deleted when `pending_count`
drops to zero — their `written_fields` must remain available for later
commits from concurrent instances. All generations are cleared at `reset()`.

Channel merge semantics per field:

- `LastValue` channel: single-writer per generation. Multi-write within
  the same generation raises `InvalidUpdateError`. Cross-generation
  last-completed-wins.
- `ReducerChannel`: all writes fold via the reducer. Reducers are not
  required to be commutative; order-dependent results are documented as
  the user's responsibility. Within a generation, fold order is
  completion order (deterministic given asyncio single-thread). Across
  generations, fold order is merge order (temporal).

The conflict detection mechanism is abstracted behind the
`WriteConflictDetector` ABC (D18). The default implementation is
`GenerationWriteTracker`.

**Concurrency safety:** The merge operation (conflict check + state
update + version advance) is a synchronous code segment with no `await`
between the steps. asyncio's single-thread model guarantees that even if
multiple instances complete simultaneously, their merge segments execute
serially — no data race.

### D9 — Iteration counting

`max_iterations` counts every node instance execution. Parallel execution
of N instances consumes N iterations. Exceeding the limit raises
`GraphRecursionError`. This preserves the Phase a safety-net semantics.

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

### D12 — Routing model (shared by both schedulers)

Two-layer routing, replacing the Phase a five-level priority chain:

1. `Command.goto` — explicit override (highest priority).
2. `transition` matches static edges by `reason` — all matching edges
   fire (multi-target fan-out). `transition=None` falls back to default
   edges (`reason=None`). No match and no default → `RoutingError`.

The `route_fn` conditional edge mechanism (`add_conditional_edges`) is
**removed**. All conditional routing is expressed via `transition` +
static edges or `Command.goto`. (No existing code uses `route_fn` —
verified by grepping the codebase.)

`Command.goto` type changes: `str | list[Task] | None`. The `list[str]`
form is removed (it was a Phase a sequential-queue artifact). Use
`Task(node="B", state=None)` for shared-state fan-out; `Task(node="B",
state=<independent>)` for isolated-state fan-out. The `pending` queue is
deleted.

### D13 — Error handling under continuous scheduling

- **Node exception (non-`GraphBubbleUp`)**: the failing instance's
  `asyncio.Task` raises. The scheduler cancels all remaining running
  tasks and propagates the exception. Unlike the previous batch model
  (where `asyncio.gather` handled cancellation automatically), the
  scheduler explicitly cancels running tasks before propagating.
- **`GraphInterrupt`**: the first interrupt propagates immediately; all
  running tasks are cancelled. Same as Phase a — the engine never
  swallows `GraphBubbleUp`.
- **`InvalidUpdateError` (LastValue multi-write)**: raised during the
  synchronous merge segment (D8) when the `WriteConflictDetector` (D18)
  detects a same-generation conflict. Propagates as above — all running
  tasks are cancelled. Since the merge segment has no `await`, the
  error is raised atomically before any other instance can observe a
  partially-merged state.

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
executing, and `execute` eventually returns (possibly with an empty
`NodeResult`).

`execute` returning marks the instance COMPLETED. The scheduler then
compiles any `transition` / `Command.goto` into additional dispatches.
After COMPLETED, the instance accepts no further dispatches (unless it
is re-activated by a loop, which creates a new instance).

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
- Create instance → fork main_state (D7) → `create_task` immediately.
- No reachability check. No PENDING state. The node's business logic
  decides how to handle the payload — the framework guarantees
  concurrency safety (conflict detection at merge time), not business
  correctness.

**Event 2 — dispatch to ON_ALL_PREDS node X:**
- Record pending dispatch in the per-target queue.
- `_try_fire(X)`: (a) all activated sources dispatched? (b) reachability
  clear (D4)?
  - Yes → fork main_state → `create_task`.
  - No → stays in pending queue.

**Event 3 — instance completes:**
- Atomic merge: conflict check (D18) + `apply_state_update` + version
  advance (D8). No `await` in this segment.
- Compile routing: `NodeResult.transition` / `Command.goto` → dispatch
  events (events 1 or 2).
- Re-check all pending ON_ALL_PREDS nodes (D4): an instance completing
  may clear a reachability path to a node it has no direct edge to.
- Schedule async checkpoint (D19).

This event model replaces the previous batch loop (`gather` → merge →
recheck). The `_schedule()` method is the single entry point for all
scheduling decisions.

### D18 — WriteConflictDetector ABC

Conflict detection is abstracted behind an ABC to allow future strategies
(optimistic retry, custom resolution, distributed detection).

```python
class WriteConflictDetector(ABC):
    """Detects concurrent write conflicts for LastValue fields."""

    @abstractmethod
    def register(self, fork_version: int) -> None:
        """Called when an instance forks. Tracks a new concurrent writer
        in the given generation."""

    @abstractmethod
    def commit(self, fork_version: int, fields: Collection[str]) -> None:
        """Called before merge. Raises InvalidUpdateError if a
        same-generation instance already wrote any of `fields`."""

    @abstractmethod
    def complete(self, fork_version: int) -> None:
        """Called after merge. Decrements the generation's writer count.
        Cleans up when all writers in a generation are done."""

    @abstractmethod
    def advance(self) -> int:
        """Called after merge. Increments and returns the new
        main_state version. Subsequent forks enter a new generation."""

    @abstractmethod
    def reset(self) -> None:
        """Called at the start of each run_async. Clears all state."""
```

Default implementation: `GenerationWriteTracker` — tracks a
`dict[int, _Generation]` where `_Generation` holds `written_fields:
set[str]` and `pending_count: int`. One generation per fork_version.

**Concurrency safety:** All methods are synchronous (no `await`). The
caller (ParallelScheduler) must invoke `commit + apply_state_update +
advance + complete` as one atomic synchronous segment — no `await`
between them. asyncio's single-thread model guarantees no interleaving.

### D19 — CheckpointStore ABC

State persistence is abstracted behind an ABC. The default
implementation is `SqliteCheckpointStore`. A `MemoryCheckpointStore` is
available for testing.

```python
class CheckpointData(BaseModel):
    """Frozen snapshot of scheduler + state at a point in time."""
    main_state: dict[str, Any]          # GraphState.checkpoint()
    main_state_version: int             # current generation
    pending_on_all_preds: dict[str, dict[str, list[dict | None]]]
    completed_instances: list[InstanceRecord]
    dispatch_events: list[DispatchEvent]


class CheckpointStore(ABC):
    """Persists scheduler state for crash recovery."""

    @abstractmethod
    async def save(self, run_id: str, checkpoint: CheckpointData) -> None: ...

    @abstractmethod
    async def load(self, run_id: str) -> CheckpointData | None: ...

    @abstractmethod
    async def load_latest(self, run_id: str) -> CheckpointData | None: ...
```

**Checkpoint timing:** After each instance merge (event 3 in D17), the
scheduler schedules an async checkpoint. This does NOT block the
scheduling loop — the merge is already committed to memory, and the
checkpoint write runs as a background `asyncio.Task`. On crash, recovery
starts from the last successfully-saved checkpoint; instances after that
point are re-dispatched.

**`ConcurrentWriteTracker` state is NOT persisted** — it tracks only
in-flight concurrent instances. On restart, no instances are running,
so the tracker starts fresh. Conflict detection is a runtime safety
mechanism, not a recovery mechanism.

**Implementation note (recovery deferral):** `save` is implemented and
called after each instance merge (checkpoint data is snapshotted
synchronously in the merge segment, then persisted asynchronously). The
checkpoint `asyncio.Task` is tracked in a set with done-callbacks to
prevent GC, and failures are logged rather than silently swallowed.
However, **crash recovery is not yet wired** — `load_latest` is not
called by the scheduler (no resume path exists). `CheckpointData` does
not include `main_state_version` (deviation from the sketched interface
above). Full D19 recovery (resume from last checkpoint, re-dispatch
instances after that point) is deferred to a future phase.

### D20 — Extensibility seams

The continuous scheduler is designed with ABC-first seams for future
extension:

| Seam | ABC | Default impl | Extension examples |
|------|-----|-------------|-------------------|
| Channel semantics | `BaseChannel` (existing) | `LastValue`, `ReducerChannel` | `Topic`, `NamedBarrierValue` |
| Conflict detection | `WriteConflictDetector` (D18) | `GenerationWriteTracker` | Optimistic retry, custom resolution |
| State persistence | `CheckpointStore` (D19) | `SqliteCheckpointStore` | Remote store, Redis |
| Dispatch persistence | `DispatchStore` (existing) | `InMemoryDispatchStore`, `SqliteDispatchStore` | Remote dispatch |
| Reachability | (concrete class, not yet ABC) | BFS over static edges | Custom policies when second use case emerges |

Reachability is intentionally a concrete method, not an ABC — per rule 6
("one adapter is hypothetical; two make a real seam"). It will be
abstracted when a second policy is needed.

## Consequences

### Positive

- **No head-of-line blocking.** A short task (10ms) completing no longer
  waits for a long task (10s) in the same batch. Instances start the
  moment their dependencies are satisfied. This is the primary advantage
  over langgraph's BSP model.
- **Parallel fan-out works automatically.** `Command(goto=[Task(...)])`
  code from Phase a runs in parallel without node changes.
- **Simple graphs pay no tax.** `LinearScheduler` is unchanged; ReAct
  and existing patterns are zero-impact.
- **Fan-in is correct.** Reachability-based readiness (ON_ALL_PREDS
  only) prevents premature joins and deadlocks from skipped branches.
- **Dispatch is a first-class abstraction.** Enables modexctl-driven
  workflows and future remote dispatch.
- **Multi-instance model** eliminates state-reset bugs in loops and
  `ON_RECEIVE` multi-trigger scenarios.
- **Extensible by design.** ABC-first seams (D18, D19, D20) allow
  future conflict-detection strategies, checkpoint backends, and channel
  types without core changes.
- **Crash recovery.** Async checkpoint after each merge (D19) means
  restart loses at most one in-flight instance's work.

### Negative

- **`ParallelScheduler` complexity.** State machine, reachability BFS,
  instance tracking, fork/merge, generation tracking — significant
  implementation surface. Mitigated by being opt-in and sharing
  `LinearScheduler`'s test coverage for simple graphs.
- **Fork cost.** Deep-copying `GraphState` per instance has cost
  proportional to state size. Mitigated by single-node fast path (no fork
  when no concurrency). Future optimization: copy-on-write.
- **Generation tracking overhead.** Each merge checks a `set[str]` of
  written fields — O(fields_per_update). Negligible for typical state
  sizes (< 20 fields).
- **Async checkpoint lag.** The checkpoint write is async — on crash,
  the last checkpoint may be stale by one instance. Acceptable for agent
  workloads (not financial transactions).
- **`route_fn` removal is a breaking change.** Any external code using
  `add_conditional_edges` breaks. No internal code uses it (verified).
- **`Command.goto=list[str]` removal is a breaking change.** Any code
  returning `Command(goto=["A", "B"])` must change to
  `Command(goto=[Task(node="A", state=None), Task(node="B", state=None)])`.
  No internal code uses `list[str]` form (verified).

### Rejected alternatives

- **BSP supersteps (langgraph model).** langgraph 1.2.9 uses BSP with
  an `after_tick()` barrier — channel updates from step N are only
  visible in step N+1. The `asyncio.wait(FIRST_COMPLETED)` in its runner
  is for streaming/error-cancel, not continuous scheduling. BSP is
  simpler (no fork/MVCC needed — step immutability guarantees
  consistency) but imposes the head-of-line blocking tax. This ADR
  chooses continuous scheduling + fork + generation-based conflict
  detection to eliminate that tax. The trade-off is implementation
  complexity (fork, WriteConflictDetector) vs. latency.
- **Single scheduler with fast-path optimization.** Considered (one
  `ParallelScheduler` with internal "if single node, skip fork/instance"
  branches). Rejected — scatters complexity across the hot path; two
  clean implementations are simpler than one with conditional branches.
- **NamedBarrierValue channel for fan-in (langgraph pattern).** Considered.
  Rejected — modex_graph's reachability-based readiness judgment achieves
  the same join semantics without a new channel type per join node.
- **Push-based dispatch only (no `ctx.dispatch`).** Considered (nodes
  always return `NodeResult`, framework always computes routes). Rejected
  — cannot support modexctl remote dispatch or complex business-driven
  routing.
- **Pure pull-based dispatch (nodes read from a mailbox).** Considered.
  Rejected — the framework still needs to know dispatch happened (for
  readiness). Push with optional pull (node reads `ctx.state` which
  already contains merged payloads) is simpler.
- **MVCC with version tree.** Considered during design (per-snapshot
  version chain, per-field version tracking). Rejected as over-engineering
  — the generation-based `set[str]` approach (D18) achieves the same
  safety guarantee with far less complexity. A full version tree is only
  needed if future requirements demand stale-read detection or
  multi-version reads, which the current DAG model does not.

## Relationship to ADR-0033

This ADR realizes ADR-0033 D12 Phase c items:

| D12 row | ADR-0034 decision |
|---|---|
| MapReduce (fan-out → fan-in), parallel | D2, D3, D4, D5 |
| Parallel fan-out with multi-write detection | D7, D8, D13, D18 |
| Cooperative shutdown (`GraphDrained`) | Deferred (exception class exists; wiring is future work) |
| Subroutine / Graph-of-graphs | Deferred (Graph-is-a-Node wiring exists; exercising is future work) |

ADR-0033 D1 Phase c item 6 ("BSP vs keep sequential + opt-in parallel")
is resolved here: **keep sequential as default (`LinearScheduler`),
parallel as opt-in (`ParallelScheduler`)**, with a continuous scheduling
model (not BSP) for the parallel path.
