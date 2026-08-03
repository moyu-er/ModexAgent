"""``ParallelScheduler[S]`` — continuous multi-instance execution strategy.

Implements the continuous scheduling model (ADR-0034 D2): instances start as
independent ``asyncio.create_task`` coroutines the moment their dependencies
are satisfied — there is no batch barrier. Features generation-based conflict
detection, coordinator-driven recovery, and trigger-mode routing.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ..channel import LastValue
from ..conflict_detector import GenerationWriteTracker, WriteConflictDetector
from ..constants import GraphNode, InvocationStatus, NodeInstanceStatus, NodeTrigger, SchedulerKind
from ..dispatch_store import DispatchStore, InMemoryDispatchStore
from ..exceptions import GraphRecursionError, RoutingError
from ..id_generator import default_id_generator
from ..integration import IntegratedPayload
from ..result import DispatchEvent
from .base import Scheduler
from .instance import NodeInstance

if TYPE_CHECKING:
    from ..compiled_graph import CompiledGraph
    from ..context import GraphContext
    from ..graph_metadata import RecoveryContext
    from ..state import GraphState


class ParallelScheduler[S: "GraphState"](Scheduler[S]):
    """Continuous multi-instance scheduler with conflict detection + coordinator recovery.

    Implements the continuous scheduling model (ADR-0034 D2): instances
    start as independent `asyncio.create_task` coroutines the moment their
    dependencies are satisfied — there is no batch barrier. The scheduler
    waits for any task to complete via `asyncio.wait(FIRST_COMPLETED)`,
    processes the merge, then launches any newly-READY instances.

    **Fast path** (single READY + no RUNNING): executes directly on
    `main_state` — no fork, no conflict detection. `instance.forked_state`
    is `None`, `instance.fork_version` is `0`. `ctx.state` points to
    `main_state`. Imperative mutations (`ctx.state.x = y`) are directly
    effective. `NodeResult.state_update` is applied immediately via
    `apply_state_update`.

    **Fork path** (multiple READY or has RUNNING): each READY instance
    forks `main_state` via `model_copy(deep=True)` before execution.
    `instance.forked_state` is the snapshot; `instance.fork_version`
    captures the `main_state` version at fork time. `ctx.state` (via a
    forked `sub_ctx`) points to `forked_state`. Imperative mutations stay
    on the fork and do NOT propagate to `main_state`. After `node.execute`
    returns, `NodeResult.state_update` is merged to `main_state` via the
    atomic segment: `commit + apply_state_update + advance + complete`
    (no `await` between them — asyncio's single-thread model guarantees
    no interleaving). The conflict detector raises `InvalidUpdateError`
    if two instances in the same generation write the same `LastValue`
    field.

    **Recovery**: at the top of ``run_async``,
    ``ctx.coordinator.load_for_recovery()`` is called. If prior state
    exists (any node has an invocation record), the scheduler rebuilds
    its in-memory state from the ``RecoveryContext`` — counters,
    activated_sources, pending_dispatches, and main_state are restored.
    Nodes with non-terminal invocation status (CRASHED, SUPERSEDED with
    no successor, suspended RUNNING) are re-dispatched. COMPLETED
    nodes with PENDING delivers in the deliver_store are also
    re-dispatched.

    **Other features:**

    - `ctx.dispatch(target, state_update)` routing: validates target against
      the source node's outgoing edges, creates a `DispatchEvent`, and
      creates/queues the target instance. Dispatches happen inside
      `Node._submit` (called by `run()`), before the merge.
    - `GraphNode.END` dispatch: terminal signal, does NOT create an instance.
    - `max_iterations`: global per-instance-execution counter; raises
      `GraphRecursionError` on overflow.
    - Trigger modes: `ON_ALL_PREDS` (gated by reachability BFS) and
      `ON_RECEIVE` (immediate, no gating).

    `GraphBubbleUp` exceptions propagate verbatim — the scheduler NEVER
    catches and swallows them (D7). On exception from any instance, all
    remaining running tasks are cancelled (D13) before re-raising.
    """

    def __init__(
        self,
        graph: CompiledGraph[S],
        *,
        dispatch_store: DispatchStore | None = None,
        conflict_detector: WriteConflictDetector | None = None,
    ) -> None:
        self.graph = graph
        # Reset at the top of each `run_async` call — stateless across calls.
        self._main_state: S | None = None
        self._instances: dict[str, NodeInstance[S]] = {}
        self._instance_seq: int = 0
        # Dispatch persistence: store survives across runs; each
        # run gets a fresh run_id. The _dispatch_log property reads from the
        # store for backward compat with direct-access callers.
        self._dispatch_store: DispatchStore = (
            dispatch_store if dispatch_store is not None else InMemoryDispatchStore()
        )
        self._run_id: str | None = None
        # graph_instance_id: Snowflake ID — the single persistence
        # key replacing uuid run_id (rule 15: converge). Set at the top of
        # run_async from ctx.graph_instance_id or generated fresh.
        self._graph_instance_id: int = 0
        self._active: set[str] = set()
        self._ready: set[str] = set()
        self._iteration_count: int = 0
        # Stored ctx for dispatch handler access to coordinator.
        self._ctx: GraphContext[S] | None = None
        # Conflict detection (ADR-0034 D18): generation-based write tracking.
        # `_current_version` mirrors the detector's version via advance()'s
        # return value (the ABC doesn't expose current_version as a property).
        self._conflict_detector: WriteConflictDetector = (
            conflict_detector if conflict_detector is not None else GenerationWriteTracker()
        )
        self._current_version: int = 0
        # ── Trigger mode state ──────────────────────────────────
        # Per-target activated sources: which source NODE NAMES have dispatched
        # to this target. A source is "activated" on first dispatch; it stays
        # activated for the rest of the run (used by ON_ALL_PREDS grouping).
        self._activated_sources: dict[str, set[str]] = {}
        # Per-target pending dispatch queues: target -> source -> [payloads].
        # ON_ALL_PREDS consumes one payload per source when firing a group.
        # ON_RECEIVE does not use this (instances are created immediately).
        self._pending_dispatches: dict[str, dict[str, list[dict[str, Any] | None]]] = {}
        self._wakeup: asyncio.Event | None = None

    @property
    def _dispatch_log(self) -> list[DispatchEvent]:
        """Backward-compat accessor: events for the current run from the store."""
        if self._run_id is None:
            return []
        return self._dispatch_store.query_all(self._run_id)

    def query_dispatches_by_target(self, target: str) -> list[DispatchEvent]:
        """Recovery query: all dispatches to ``target`` in the current run.

        Wraps ``DispatchStore.query_by_target`` with the scheduler's current
        ``run_id``. Returns an empty list if no run is active. Useful for
        external callers and debugging recovery state.
        """
        if self._run_id is None:
            return []
        return self._dispatch_store.query_by_target(target, self._run_id)

    # ── Scheduler ABC implementation ───────────────────────────────────

    async def run_async(self, ctx: GraphContext[S]) -> S:
        """Run the graph under the continuous multi-instance model.

        Wires `ctx.dispatch` to this scheduler's `_handle_dispatch`, creates
        the entry instance, then loops: launch all READY instances as
        independent `asyncio.create_task` coroutines → wait for any to
        complete via `asyncio.wait(FIRST_COMPLETED)` → process the
        completed instance (merge + routing + recheck, all handled inside
        `_execute_instance`) → repeat until `ready` is empty and no tasks
        are running.

        Fast path (single READY + no RUNNING): executes directly on
        `main_state`, bypasses the conflict detector.

        Fork path (multiple READY or has RUNNING): forks `main_state` per
        instance, registers with the conflict detector, and merges via the
        atomic `commit + apply_state_update + advance + complete` segment
        after `node.execute` returns.

        Recovery: at the top, `ctx.coordinator.load_for_recovery()`
        is called. If prior state exists (any node has an invocation record),
        state is rebuilt from the `RecoveryContext` — completed instances are
        NOT re-executed, non-terminal nodes are re-dispatched, COMPLETED
        nodes with PENDING delivers are re-dispatched. If no prior state,
        fresh start (`_init_fresh_state`).

        Error handling (D13): if any instance raises, all remaining running
        tasks are cancelled and the exception propagates to the caller.

        Returns `ctx.state` (the shared `main_state`).
        """
        # graph_instance_id from ctx or generate fresh (backward
        # compat). Snowflake ID — the single persistence key replacing
        # uuid run_id (rule 15: converge on one key).
        self._graph_instance_id = ctx.graph_instance_id or default_id_generator().generate()
        self._run_id = str(self._graph_instance_id)
        self._ctx = ctx

        # Recovery: load from coordinator. If prior state exists, rebuild
        # scheduler state from RecoveryContext. Otherwise, fresh start.
        recovery = ctx.coordinator.load_for_recovery()
        has_prior_state = any(v is not None for v in recovery.node_states.values())
        if has_prior_state:
            self._restore_from_recovery(ctx, recovery)
        else:
            self._init_fresh_state(ctx)

        running: dict[asyncio.Task[None], str] = {}

        while self._ready or running:
            while self._ready:
                iid = sorted(self._ready)[0]
                self._ready.discard(iid)

                need_fork = bool(running) or len(self._ready) > 0

                if need_fork:
                    instance = self._instances[iid]
                    instance.fork_version = self._current_version
                    self._conflict_detector.register(instance.fork_version)
                    if instance.forked_state is None:
                        assert self._main_state is not None
                        instance.forked_state = self._main_state.model_copy(deep=True)

                task = asyncio.create_task(
                    self._execute_instance(iid, ctx, fork=need_fork)
                )
                running[task] = iid

            if not running:
                break

            assert self._wakeup is not None
            wakeup_wait = asyncio.ensure_future(self._wait_for_wakeup())
            try:
                done, _ = await asyncio.wait(
                    {*running.keys(), wakeup_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not wakeup_wait.done():
                    wakeup_wait.cancel()
            self._wakeup.clear()

            for task in done:
                if task is wakeup_wait:
                    continue
                iid = running.pop(task)
                try:
                    task.result()
                except Exception:
                    for t in running:
                        t.cancel()
                    await asyncio.gather(*running, return_exceptions=True)
                    raise

        ctx.set_current_instance(None)
        return ctx.state

    async def _wait_for_wakeup(self) -> None:
        if self._wakeup is not None:
            await self._wakeup.wait()

    # ── State initialization: fresh vs recovery ────────────────

    def _init_fresh_state(self, ctx: GraphContext[S]) -> None:
        """Initialize scheduler state for a fresh run (no prior invocations)."""
        self._main_state = ctx.state
        self._instances = {}
        self._instance_seq = 0
        self._active = set()
        self._ready = set()
        self._iteration_count = 0
        self._activated_sources = {}
        self._pending_dispatches = {}
        self._conflict_detector.reset()
        self._current_version = 0
        self._wakeup = asyncio.Event()

        ctx.scheduler_kind = SchedulerKind.PARALLEL
        ctx.set_dispatch_handler(self._handle_dispatch)

        entry_id = self._create_instance(self.graph.entry_node)
        self._mark_ready(entry_id)

    def _restore_from_recovery(
        self, ctx: GraphContext[S], recovery: RecoveryContext
    ) -> None:
        """Rebuild scheduler state from a ``RecoveryContext``.

        - ``main_state`` is restored via ``GraphState.from_checkpoint`` from
          ``recovery.rebuilt_main_state`` (coordinator pre-builds it).
        - Counters (``_iteration_count``, ``_instance_seq``) are restored
          from ``recovery.metadata``.
        - ``_activated_sources`` and ``_pending_dispatches`` are restored
          from ``recovery.metadata``.
        - Completed instances are NOT re-added to ``_instances``.
        - ``_recheck_pending`` is called to re-dispatch any pending
          ON_ALL_PREDS targets whose reachability gate is now clear.
        - Nodes with SUPERSEDED/CRASHED/orphan status are re-dispatched.
        - COMPLETED nodes with PENDING delivers are re-dispatched.
        - The conflict detector is reset (ephemeral per-generation, not
          persisted — D19).
        """
        if recovery.rebuilt_main_state:
            state_class = type(ctx.state)
            restored = state_class.from_checkpoint(recovery.rebuilt_main_state)
            ctx.state = restored
            self._main_state = restored
        else:
            self._main_state = ctx.state

        self._iteration_count = recovery.metadata.iteration_count
        self._instance_seq = recovery.metadata.instance_seq

        self._activated_sources = {
            target: set(sources)
            for target, sources in recovery.metadata.activated_sources.items()
        }
        self._pending_dispatches = dict(recovery.metadata.pending_dispatches)

        self._instances = {}
        self._active = set()
        self._ready = set()

        ctx.scheduler_kind = SchedulerKind.PARALLEL
        ctx.set_dispatch_handler(self._handle_dispatch)

        self._conflict_detector.reset()
        self._current_version = 0
        self._wakeup = asyncio.Event()

        self._redispatch_from_recovery(recovery)
        self._recheck_pending()

    def _redispatch_from_recovery(self, recovery: RecoveryContext) -> None:
        """Re-dispatch nodes based on their latest invocation status.

        SUPERSEDED with no successor (it is the latest invocation) →
        re-dispatch. CRASHED / orphan PENDING / orphan RUNNING (suspended=False)
        → re-dispatch. suspended=True RUNNING → re-dispatch (resume path).
        CANCELED → skip (deliberate cancel, requires explicit resume).
        COMPLETED → check deliver_store for PENDING delivers; if any,
        re-dispatch to process them.
        """
        for node_name, record in recovery.node_states.items():
            if record is None:
                continue
            if record.status == InvocationStatus.COMPLETED:
                # Check deliver_store for PENDING delivers targeting
                # this COMPLETED node. If delivers exist, re-dispatch.
                if self._ctx is not None:
                    delivers = self._ctx.coordinator.collect_consumable_delivers(
                        node_name, 0
                    )
                    if delivers:
                        instance_id = self._create_instance(node_name)
                        self._mark_ready(instance_id)
                continue
            if record.status == InvocationStatus.CANCELED:
                continue
            # SUPERSEDED (no successor), CRASHED, orphan PENDING/RUNNING,
            # suspended RUNNING — all need re-dispatch.
            instance_id = self._create_instance(node_name)
            self._mark_ready(instance_id)

    # ── Instance lifecycle ────────────────────────────────────────────

    def _create_instance(
        self,
        node_name: str,
        initial_state: S | None = None,
        upstream_payloads: list[IntegratedPayload] | None = None,
    ) -> str:
        """Create a new `NodeInstance` for `node_name` in DORMANT status.

        Assigns the next global seq number and registers the instance in
        `_instances` and `_active`. Returns the `instance_id`.

        If ``initial_state`` is provided, it is stored on the instance as
        ``forked_state`` and used as the execution state instead of forking
        ``main_state``.

        If ``upstream_payloads`` is provided, it is stored on the instance
        as scheduler internal bookkeeping (no longer passed to
        ``node.run()`` — flows through the coordinator). ``None`` means the
        entry node (no upstream).

        Does NOT mark the instance READY — the caller does that via
        `_mark_ready` once any gating is satisfied.
        """
        seq = self._instance_seq
        self._instance_seq += 1
        instance_id = f"{node_name}#{seq}"
        instance = NodeInstance[S](
            instance_id=instance_id,
            node_name=node_name,
            seq=seq,
            status=NodeInstanceStatus.DORMANT,
            forked_state=initial_state,
            upstream_payloads=upstream_payloads,
        )
        self._instances[instance_id] = instance
        self._active.add(instance_id)
        return instance_id

    def _mark_ready(self, instance_id: str) -> None:
        instance = self._instances[instance_id]
        instance.status = NodeInstanceStatus.READY
        self._ready.add(instance_id)
        if self._wakeup is not None:
            self._wakeup.set()

    # ── Execution ─────────────────────────────────────────────────────

    async def _execute_instance(
        self,
        instance_id: str,
        ctx: GraphContext[S],
        *,
        fork: bool = False,
    ) -> None:
        """Execute a single instance: READY → RUNNING → run node → COMPLETED.

        Fast path (``fork=False`` and ``instance.forked_state is None``):
        executes directly on ``main_state`` (= ``ctx.state``). The conflict
        detector is bypassed (no concurrency → no conflict possible).
        ``NodeResult.state_update`` is applied directly to ``ctx.state``
        via ``apply_state_update``.

        Fork path (``fork=True`` or ``instance.forked_state is not None``):
        forks ``main_state`` via ``model_copy(deep=True)`` before execution
        (if not already forked). The instance executes on a forked
        ``sub_ctx`` whose ``state`` is the snapshot. Imperative mutations
        stay on the fork and do NOT propagate to ``main_state``. After
        ``node.execute`` returns, ``NodeResult.state_update`` is merged to
        ``main_state`` via the atomic segment:
        ``commit + apply_state_update + advance + complete`` (no ``await``
        between them — asyncio's single-thread model guarantees no
        interleaving). The conflict detector raises ``InvalidUpdateError``
        if a same-generation instance already wrote any of the same fields.

        The ``after_node`` hook is called AFTER the merge (D8), so the
        hook observes the merged state. After ``after_node``, routing is
        compiled into ``ctx.dispatch`` calls, and pending instances are
        re-checked.

        ``max_iterations`` checked before execution; overflow raises
        ``GraphRecursionError``. ``GraphBubbleUp`` propagates (not caught).
        """
        instance = self._instances[instance_id]

        # Engine-level safety net (per-instance-execution counting).
        if self._iteration_count >= self.graph.max_iterations:
            raise GraphRecursionError(
                f"Graph exceeded max_iterations={self.graph.max_iterations} "
                f"(last instance: {instance_id!r}). This is an abnormal exit — "
                f"the business-level max iteration count should route to "
                f"END via ctx.dispatch(GraphNode.END) before this safety net fires."
            )

        instance.status = NodeInstanceStatus.RUNNING

        node = self.graph.nodes[instance.node_name]

        if fork:
            assert instance.forked_state is not None
            exec_ctx = ctx.fork(state=instance.forked_state)
            exec_ctx.set_current_instance(instance_id)
        elif instance.forked_state is not None:
            exec_ctx = ctx.fork(state=instance.forked_state)
            exec_ctx.set_current_instance(instance_id)
        else:
            instance.forked_state = None
            ctx.set_current_instance(instance_id)
            exec_ctx = ctx

        # Engine-auto-invoked lifecycle hook (D5: before_node).
        await exec_ctx.runtime.before_node(exec_ctx, instance.node_name)

        # Execute via run() — pass graph topology. _submit dispatches (via
        # ctx.dispatch) happen inside run(), before the merge below.
        # _handle_dispatch only creates DORMANT instances and marks READY;
        # forking happens at next-loop iteration, post-merge.
        # GraphBubbleUp exceptions propagate — NOT caught here.
        # Upstream payloads flow through coordinator.collect_consumable_delivers.
        # The dispatch handler calls coordinator.route_deliver to route
        # delivers to the target node's deliver_store.
        result = await node.run(
            exec_ctx,
            graph=self.graph,
        )

        # Atomic merge segment (ADR-0034 D8): commit + apply_state_update +
        # advance + complete as one synchronous segment (no await between
        # them). asyncio's single-thread model guarantees no interleaving.
        #
        # Fast path (fork=False, forked_state is None): bypass the conflict
        # detector entirely — no concurrency, no conflict possible. Apply
        # state_update directly to ctx.state (= main_state).
        #
        # Fork path (fork=True or forked_state is not None): use the conflict
        # detector to catch same-generation LastValue collisions, then apply
        # state_update to main_state (NOT exec_ctx.state — the forked state
        # is discarded after merge; only state_update propagates).
        if fork or instance.forked_state is not None:
            if result.state_update is not None:
                assert self._main_state is not None
                last_value_fields = {
                    name
                    for name, ch in self._main_state._channels.items()
                    if isinstance(ch, LastValue)
                }
                conflict_fields = [
                    f for f in result.state_update if f in last_value_fields
                ]
                self._conflict_detector.commit(instance.fork_version, conflict_fields)
                self._main_state.apply_state_update(result.state_update)
                if exec_ctx.state is not self._main_state:
                    exec_ctx.state.apply_state_update(result.state_update)
            else:
                self._conflict_detector.commit(instance.fork_version, [])
            self._current_version = self._conflict_detector.advance()
            self._conflict_detector.complete(instance.fork_version)
        else:
            if result.state_update is not None:
                exec_ctx.state.apply_state_update(result.state_update)

        # Engine-auto-invoked lifecycle hook (D5: after_node). Called AFTER
        # the merge so the hook observes the merged state.
        await exec_ctx.runtime.after_node(exec_ctx, instance.node_name, result)

        instance.status = NodeInstanceStatus.COMPLETED
        self._active.discard(instance_id)
        self._iteration_count += 1

        self._recheck_pending()


    # ── Dispatch handling (trigger modes) ────────────────────

    def _handle_dispatch(
        self,
        source_instance: str,
        target: str,
        payload: dict[str, Any] | None,
        initial_state: S | None = None,
    ) -> None:
        """Process a `ctx.dispatch(target, state_update)` call.

        Called synchronously from `GraphContext.dispatch` under
        `SchedulerKind.PARALLEL`. Takes effect immediately:

        1. Validate `target` is in the source node's outgoing edges
           (raises `RoutingError` if not).
        2. Record a `DispatchEvent` in `dispatch_log`.
        3. If `target == GraphNode.END`: terminal signal, do NOT create an
           instance (the dispatch is already recorded in step 2).
        4. Otherwise: resolve the target's trigger mode and apply
           trigger-mode logic:

           - `ON_RECEIVE` (ADR-0034 D4): create a new instance and mark it
             READY immediately. Reachability is NOT checked — the instance
             will be picked up by the run_async loop's inner ``while
             self._ready:`` on the next iteration.
           - `ON_ALL_PREDS`: record the dispatch in the per-target pending
             queue. When every activated source has at least one dispatch
             AND reachability is clear, consume one dispatch per source
             and create one instance (READY). Otherwise leave queued.

        The reachability BFS (`_can_reach_active`) is the safety gate for
        ON_ALL_PREDS: a node never becomes READY while any
        PENDING/READY/RUNNING instance (excluding the candidate itself)
        can reach it along declared outgoing edges.
        """
        source_instance_obj = self._instances.get(source_instance)
        if source_instance_obj is None:
            raise RoutingError(f"Dispatch from unknown instance {source_instance!r}.")
        source_node_name = source_instance_obj.node_name

        valid_targets = self._outgoing_targets(source_node_name)
        if target not in valid_targets:
            raise RoutingError(
                f"Dispatch target {target!r} is not in the outgoing edges of "
                f"node {source_node_name!r}. Valid targets: "
                f"{sorted(valid_targets)}."
            )

        event = DispatchEvent(
            source_instance=source_instance,
            target=target,
            payload=payload,
        )
        assert self._run_id is not None  # set at the top of run_async
        self._dispatch_store.record(event, self._run_id)

        # Route deliver to target node's deliver_store via coordinator.
        content = payload.get("delivered") if payload is not None else None
        source_node = payload.get("_source_node", source_node_name) if payload else source_node_name
        source_inv_id = payload.get("_source_inv_id", 0) if payload else 0
        if self._ctx is not None:
            self._ctx.coordinator.route_deliver(target, content, source_node, source_inv_id)

        if target == GraphNode.END:
            return

        trigger = self._resolve_trigger(target)

        if trigger == NodeTrigger.ON_RECEIVE:
            # ON_RECEIVE (D4): create instance and mark READY immediately.
            # No reachability check, no PENDING state — the run_async loop
            # picks it up directly. Store the dispatch payload as an
            # IntegratedPayload for the downstream node's input integration.
            content = payload.get("delivered") if payload is not None else None
            upstream = [IntegratedPayload(source_node=source_node_name, content=content)]
            target_id = self._create_instance(
                target, initial_state=initial_state, upstream_payloads=upstream
            )
            self._mark_ready(target_id)
        else:
            self._activated_sources.setdefault(target, set()).add(source_node_name)
            self._pending_dispatches.setdefault(target, {}).setdefault(
                source_node_name, []
            ).append(payload)

    # ── Trigger mode helpers ────────────────────────────────

    def _resolve_trigger(self, node_name: str) -> NodeTrigger:
        node = self.graph.nodes.get(node_name)
        if node is not None and node.trigger is not None:
            return node.trigger
        return self.graph.default_trigger

    def _can_reach_active(self, target: str, *, exclude: str | None = None) -> bool:
        """BFS from all PENDING/READY/RUNNING instances (excluding `exclude`)
        and all pending-dispatch targets (ON_ALL_PREDS nodes with queued
        dispatches that will become future instances) along declared outgoing
        edges. Returns True if any can reach `target`.

        The BFS checks ALL declared outgoing edges (not just routed ones) —
        we don't know which edges a running node will take until it completes.
        Path length is ≥ 1 (one edge): an instance sitting AT the target node
        does not count as "reaching" the target unless there is an edge path
        back to it.

        Pending-dispatch targets are included because they represent nodes
        that WILL become active instances once their group completes — if
        such a node can reach `target`, `target` must wait. The `target`
        itself is excluded from the pending-dispatch start set to avoid
        self-blocking.
        """
        start_nodes: set[str] = set()
        for iid in self._active:
            if iid == exclude:
                continue
            status = self._instances[iid].status
            if status in (
                NodeInstanceStatus.READY,
                NodeInstanceStatus.RUNNING,
            ):
                start_nodes.add(self._instances[iid].node_name)

        for tgt, queues in self._pending_dispatches.items():
            if tgt == target:
                continue
            if any(queues.get(src) for src in queues):
                start_nodes.add(tgt)

        if not start_nodes:
            return False

        visited: set[str] = set()
        queue: list[str] = []
        for node in start_nodes:
            for edge in self.graph.edges_from(node):
                if edge.target not in visited:
                    queue.append(edge.target)

        while queue:
            current = queue.pop(0)
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            for edge in self.graph.edges_from(current):
                if edge.target not in visited:
                    queue.append(edge.target)

        return False

    def _try_fire_on_all_preds(self, target: str) -> None:
        """Attempt to fire one ON_ALL_PREDS instance for `target`.

        Fires when every activated source has at least one pending
        dispatch AND reachability is clear. Consumes ALL pending
        dispatches for this target (not just one per source) — the
        node fires once per "all sources have dispatched" event,
        not once per dispatch.
        """
        activated = self._activated_sources.get(target)
        if not activated:
            return
        pending = self._pending_dispatches.get(target, {})
        for source in activated:
            if not pending.get(source):
                return
        if self._can_reach_active(target):
            return
        # Collect upstream payloads from all sources for the downstream
        # node's input integration. One IntegratedPayload per dispatch.
        upstream_payloads: list[IntegratedPayload] = []
        for source in list(pending.keys()):
            for state_update in pending[source]:
                content = (
                    state_update.get("delivered") if state_update else None
                )
                upstream_payloads.append(
                    IntegratedPayload(source_node=source, content=content)
                )
            pending[source].clear()
        self._pending_dispatches.pop(target, None)
        self._activated_sources.pop(target, None)
        target_id = self._create_instance(target, upstream_payloads=upstream_payloads)
        self._mark_ready(target_id)

    def _recheck_pending(self) -> None:
        """Re-check ON_ALL_PREDS queues after a state change (an instance
        completed, clearing reachability).
        """
        for tgt in list(self._pending_dispatches.keys()):
            self._try_fire_on_all_preds(tgt)

    def _outgoing_targets(self, node_name: str) -> set[str]:
        """Return the set of valid outgoing-edge targets from `node_name`.

        Includes `GraphNode.END` if there is an edge to END. Used by
        `_handle_dispatch` for whitelist validation.
        """
        return {e.target for e in self.graph.edges_from(node_name)}


__all__ = ["ParallelScheduler"]
