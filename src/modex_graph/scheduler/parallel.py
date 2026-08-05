"""``ParallelScheduler[S]`` — continuous multi-instance execution strategy.

Implements the continuous scheduling model (ADR-0034 D2): instances start as
independent ``asyncio.create_task`` coroutines the moment their dependencies
are satisfied — there is no batch barrier. Features coordinator-driven
recovery and trigger-mode routing.
"""

from __future__ import annotations

import asyncio
from collections import deque
from copy import copy
from typing import TYPE_CHECKING, Any

from ..constants import (
    DeliverConsumptionStatus,
    GraphNode,
    InvocationStatus,
    NodeInstanceStatus,
    NodeTrigger,
    SchedulerKind,
)
from ..exceptions import GraphRecursionError, RoutingError
from ..integration import IntegratedPayload
from .base import Scheduler
from .instance import NodeInstance

if TYPE_CHECKING:
    from ..compiled_graph import CompiledGraph
    from ..context import GraphContext
    from ..persistence import RecoveryContext
    from ..state import GraphState


class ParallelScheduler[S: "GraphState"](Scheduler[S]):
    """Continuous multi-instance scheduler with coordinator recovery.

    Implements the continuous scheduling model (ADR-0034 D2): instances
    start as independent `asyncio.create_task` coroutines the moment their
    dependencies are satisfied — there is no batch barrier. The scheduler
    waits for any task to complete via `asyncio.wait(FIRST_COMPLETED)`, then
    launches any newly-READY instances. Every instance shares `ctx.state`, so
    imperative state mutations are visible directly across concurrent tasks.

    **Recovery**: at the top of ``run_async``,
    ``ctx.coordinator.load_for_recovery()`` is called. The scheduler
    rebuilds its in-memory state from the ``RecoveryContext`` —
    ``ctx.state`` is restored from ``rebuilt_main_state``,
    ``iteration_count`` is derived as the count of COMPLETED invocations
    across all nodes, ``instance_seq`` is reset to 0 (in-memory
    temporary), and the pending dispatch queue is rebuilt from a scan of
    PENDING delivers. Nodes with non-terminal invocation status (CRASHED,
    suspended RUNNING) are re-dispatched. COMPLETED nodes are NOT
    re-dispatched.

    **Other features:**

    - `ctx.dispatch(target, state_update)` routing: validates target against
      the source node's outgoing edges and creates/queues the target instance.
      Dispatches happen inside `Node._submit` (called by `run()`).
    - `GraphNode.END` dispatch: terminal signal, does NOT create an instance.
    - `max_iterations`: global per-instance-execution counter; raises
      `GraphRecursionError` on overflow.
    - Trigger modes: `ON_ALL_PREDS` (gated by reachability BFS) and
      `ON_RECEIVE` (immediate, no reachability gating; serialized per-node
      — concurrent ON_RECEIVE dispatches to a node with an in-flight
      instance queue FIFO and fire serially on completion).

    `GraphBubbleUp` exceptions propagate verbatim — the scheduler NEVER
    catches and swallows them (D7). On exception from any instance, all
    remaining running tasks are cancelled (D13) before re-raising.
    """

    def __init__(self, graph: CompiledGraph[S]) -> None:
        self.graph = graph
        # Reset at the top of each `run_async` call — stateless across calls.
        self._instances: dict[str, NodeInstance[S]] = {}
        self._instance_seq: int = 0
        self._active: set[str] = set()
        self._ready: set[str] = set()
        self._iteration_count: int = 0
        # Stored ctx for dispatch handler access to coordinator.
        self._ctx: GraphContext[S] | None = None
        # ── Trigger mode state ──────────────────────────────────
        # Per-target activated sources: which source NODE NAMES have dispatched
        # to this target. A source is "activated" on first dispatch; it stays
        # activated for the rest of the run (used by ON_ALL_PREDS grouping).
        self._activated_sources: dict[str, set[str]] = {}
        # Per-target pending dispatch queues: target -> source -> [payloads].
        # ON_ALL_PREDS consumes one payload per source when firing a group.
        # ON_RECEIVE does not use this (instances are created immediately).
        self._pending_dispatches: dict[str, dict[str, list[dict[str, Any] | None]]] = {}
        # Per-node FIFO of queued ON_RECEIVE dispatches waiting for the
        # node's current in-flight instance to complete. Each entry is a
        # (source_instance, target, payload) tuple preserving the original
        # dispatch arguments. In-memory only — NOT persisted across crashes.
        self._on_receive_queue: dict[str, deque[tuple[str, str, dict[str, Any] | None]]] = {}
        # Store scans must not reschedule delivers already handled by in-memory dispatch.
        self._scheduled_deliver_ids: set[int] = set()
        self._wakeup: asyncio.Event | None = None

    # ── Scheduler ABC implementation ───────────────────────────────────

    async def run_async(self, ctx: GraphContext[S]) -> S:
        """Run the graph under the continuous multi-instance model.

        Wires `ctx.dispatch` to this scheduler's `_handle_dispatch`, creates
        the entry instance, then loops: launch all READY instances as
        independent `asyncio.create_task` coroutines → wait for any to
        complete via `asyncio.wait(FIRST_COMPLETED)` → process the
        completed instance (routing + recheck are handled inside
        `_execute_instance`) → repeat until `ready` is empty and no tasks are
        running.

        Recovery: at the top, `ctx.coordinator.load_for_recovery()`
        is called. The scheduler rebuilds state from the `RecoveryContext`
        — completed instances are NOT re-executed, non-terminal nodes are
        re-dispatched, and the pending dispatch queue is rebuilt from
        PENDING delivers. If no prior invocations exist, the entry
        instance is created (fresh start).

        Error handling (D13): if any instance raises, all remaining running
        tasks are cancelled and the exception propagates to the caller.

        Returns the shared `ctx.state`.
        """
        self._ctx = ctx

        # Recovery: load from coordinator and rebuild scheduler state.
        # The scan runs unconditionally — even with no prior state, it
        # finds no PENDING delivers and no-ops. The entry instance is
        # created only when no prior invocations exist (fresh start).
        recovery = ctx.coordinator.load_for_recovery()
        self._restore_from_recovery(ctx, recovery)
        ctx.control.set_wakeup(self._wakeup)

        running: dict[asyncio.Task[None], str] = {}

        try:
            while self._ready or running:
                ctx.control.check()
                while self._ready:
                    iid = sorted(self._ready)[0]
                    self._ready.discard(iid)

                    task = asyncio.create_task(self._execute_instance(iid, ctx))
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

                self._rebuild_pending_from_delivers(ctx, recovery)
                self._recheck_pending()

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
        finally:
            # Cancel any remaining in-flight tasks on ALL exit paths
            # (GraphDrained, owner-task CancelledError, unhandled Exception,
            #  normal completion). On normal exit `running` is empty → no-op.
            # On the inner `except Exception` path, tasks are already
            # cancelled+gathered but still in the dict → cancel is no-op,
            # gather returns immediately.
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)

        ctx.set_current_instance(None)
        return ctx.state

    async def _wait_for_wakeup(self) -> None:
        if self._wakeup is not None:
            await self._wakeup.wait()

    # ── State initialization: recovery (unconditional) ─────────────

    def _restore_from_recovery(self, ctx: GraphContext[S], recovery: RecoveryContext) -> None:
        """Rebuild scheduler state from a ``RecoveryContext``.

        Runs unconditionally — the deliver scan and node re-dispatch
        always execute, even when no prior state exists (they no-op).

        - ``ctx.state`` is restored via ``GraphState.from_checkpoint`` from
          ``recovery.rebuilt_main_state`` (coordinator pre-builds it).
        - ``_iteration_count`` is derived as the count of COMPLETED
          invocations across all nodes (via
          ``node_state_store.query_all({COMPLETED})``).
        - ``_instance_seq`` is reset to 0 (in-memory temporary).
        - ``_activated_sources`` and ``_pending_dispatches`` are rebuilt
          from a scan of PENDING delivers across all nodes' deliver stores.
        - Nodes with CRASHED/orphan RUNNING status are re-dispatched
          (via ``_redispatch_from_recovery``). COMPLETED nodes are NOT
          re-dispatched.
        - ``_recheck_pending`` is called to fire any ON_ALL_PREDS
          targets whose reachability gate is now clear.
        - If no instances were created and no prior invocations exist,
          the entry instance is created (fresh start).
        """
        if recovery.rebuilt_main_state:
            state_class = type(ctx.state)
            restored = state_class.model_validate(recovery.rebuilt_main_state)
            ctx.state = restored

        # Derive iteration_count from COMPLETED invocations in the store.
        completed = ctx.node_state_store.query_all({InvocationStatus.COMPLETED})
        self._iteration_count = len(completed)

        # instance_seq is an in-memory temporary — reset to 0.
        self._instance_seq = 0

        self._activated_sources = {}
        self._pending_dispatches = {}
        self._on_receive_queue = {}
        self._scheduled_deliver_ids = set()

        self._instances = {}
        self._active = set()
        self._ready = set()

        ctx.scheduler_kind = SchedulerKind.PARALLEL
        ctx.set_dispatch_handler(self._handle_dispatch)

        self._wakeup = asyncio.Event()

        # Re-dispatch crashed/orphaned nodes first so the serial gate
        # is meaningful when the deliver scan fires ON_RECEIVE targets.
        self._redispatch_from_recovery(recovery)
        self._rebuild_pending_from_delivers(ctx, recovery)
        self._recheck_pending()

        # Fresh start: if no instances were recovered and no prior
        # invocations exist, create the entry instance.
        has_any_invocation = any(v is not None for v in recovery.node_states.values())
        if not self._active and not has_any_invocation:
            entry_id = self._create_instance(self.graph.entry_node)
            self._mark_ready(entry_id)

    def _rebuild_pending_from_delivers(
        self, ctx: GraphContext[S], recovery: RecoveryContext
    ) -> None:
        """Rebuild the pending dispatch queue from PENDING delivers.

        Scans ALL nodes' deliver stores for PENDING records. For each
        target node, resolves the trigger mode:

        - ``ON_ALL_PREDS``: the deliver enters the pending dispatch queue
          (``_activated_sources`` + ``_pending_dispatches``).
          ``_recheck_pending`` fires the target when the group is
          complete and reachability is clear.
        - ``ON_RECEIVE``: if the target node has no in-flight instance,
          a new instance is created to process the delivers. If the
          target is in-flight (re-dispatched), the running instance
          will consume the delivers via ``collect_consumable_delivers``.
        """
        coordinator = ctx.coordinator
        for node_name in recovery.node_states:
            delivers = coordinator.collect_consumable_delivers(node_name, 0)
            pending = [
                d
                for d in delivers
                if d.status == DeliverConsumptionStatus.PENDING
                and d.deliver_id not in self._scheduled_deliver_ids
            ]
            if not pending:
                continue
            trigger = self._resolve_trigger(node_name)
            if trigger == NodeTrigger.ON_RECEIVE:
                # Respect the serial gate — if the target is in-flight
                # (re-dispatched), the running instance consumes the
                # delivers. Otherwise, create one instance to process
                # all pending delivers for this node.
                if not self._is_node_running(node_name):
                    upstream = [
                        IntegratedPayload(
                            source_node=d.source_node, content=d.content
                        )
                        for d in pending
                    ]
                    target_id = self._create_instance(node_name, upstream_payloads=upstream)
                    self._mark_ready(target_id)
                    self._scheduled_deliver_ids.update(d.deliver_id for d in pending)
            else:
                for deliver in pending:
                    payload: dict[str, Any] | None = {
                        "delivered": deliver.content,
                        "_source_node": deliver.source_node,
                    }
                    self._activated_sources.setdefault(node_name, set()).add(
                        deliver.source_node
                    )
                    self._pending_dispatches.setdefault(node_name, {}).setdefault(
                        deliver.source_node, []
                    ).append(payload)
                    self._scheduled_deliver_ids.add(deliver.deliver_id)

    def _redispatch_from_recovery(self, recovery: RecoveryContext) -> None:
        """Re-dispatch nodes based on their latest invocation status.

        CRASHED / orphan RUNNING (suspended=False)
        → re-dispatch. suspended=True RUNNING → re-dispatch (resume path).
        CANCELED → skip (deliberate cancel, requires explicit resume).
        COMPLETED → skip (work is done; PENDING delivers are handled by
        the deliver scan in ``_rebuild_pending_from_delivers``).
        """
        for node_name, record in recovery.node_states.items():
            if record is None:
                continue
            if record.status == InvocationStatus.COMPLETED:
                continue
            if record.status == InvocationStatus.CANCELED:
                continue
            # CRASHED, orphan RUNNING, suspended RUNNING — all need re-dispatch.
            instance_id = self._create_instance(node_name)
            self._mark_ready(instance_id)

    # ── Instance lifecycle ────────────────────────────────────────────

    def _create_instance(
        self,
        node_name: str,
        upstream_payloads: list[IntegratedPayload] | None = None,
    ) -> str:
        """Create a new `NodeInstance` for `node_name` in DORMANT status.

        Assigns the next global seq number and registers the instance in
        `_instances` and `_active`. Returns the `instance_id`.

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

    async def _execute_instance(self, instance_id: str, ctx: GraphContext[S]) -> None:
        """Execute a single instance: READY → RUNNING → run node → COMPLETED.

        Each task uses its own context shell while sharing ``ctx.state``. After
        ``after_node``, pending instances are re-checked.

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
        self._iteration_count += 1

        instance.status = NodeInstanceStatus.RUNNING

        node = self.graph.nodes[instance.node_name]

        exec_ctx = copy(ctx)
        exec_ctx.set_current_instance(instance_id)
        exec_ctx.current_invocation = None

        # Engine-auto-invoked lifecycle hook (D5: before_node).
        await exec_ctx.runtime.before_node(exec_ctx, instance.node_name)

        # Execute via run() — pass graph topology. _submit dispatches (via
        # ctx.dispatch) happen inside run().
        # _handle_dispatch only creates DORMANT instances and marks READY;
        # GraphBubbleUp exceptions propagate — NOT caught here.
        # Upstream payloads flow through coordinator.collect_consumable_delivers.
        # The dispatch handler calls coordinator.route_deliver to route
        # delivers to the target node's deliver_store.
        await node.run(
            exec_ctx,
            graph=self.graph,
        )

        # Engine-auto-invoked lifecycle hook (D5: after_node).
        await exec_ctx.runtime.after_node(exec_ctx, instance.node_name)

        instance.status = NodeInstanceStatus.COMPLETED
        self._active.discard(instance_id)

        self._recheck_pending()
        self._drain_on_receive_queue(instance.node_name)

    # ── Dispatch handling (trigger modes) ────────────────────

    def _handle_dispatch(
        self,
        source_instance: str,
        target: str,
        payload: dict[str, Any] | None,
    ) -> None:
        """Process a `ctx.dispatch(target, state_update)` call.

        Called synchronously from `GraphContext.dispatch` under
        `SchedulerKind.PARALLEL`. Takes effect immediately:

        1. Validate `target` is in the source node's outgoing edges
           (raises `RoutingError` if not).
        2. If `target == GraphNode.END`: terminal signal, do NOT create an
           instance.
        3. Otherwise: resolve the target's trigger mode and apply
           trigger-mode logic:

           - `ON_RECEIVE` (ADR-0034 D4): if the target node has no
             in-flight instance, create a new instance and mark it READY
             immediately. If the target node already has an in-flight
             instance, the dispatch queues in a per-node FIFO and fires
             when the in-flight instance completes. Reachability is NOT
             checked.
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

        # Route deliver to target node's deliver_store via coordinator.
        content = payload.get("delivered") if payload is not None else None
        source_node = payload.get("_source_node", source_node_name) if payload else source_node_name
        source_inv_id = payload.get("_source_inv_id", 0) if payload else 0
        if self._ctx is not None:
            deliver_id = self._ctx.coordinator.route_deliver(
                target, content, source_node, source_inv_id
            )
            if deliver_id is not None:
                self._scheduled_deliver_ids.add(deliver_id)

        if target == GraphNode.END:
            return

        trigger = self._resolve_trigger(target)

        if trigger == NodeTrigger.ON_RECEIVE:
            # ON_RECEIVE dispatches are serialized per-node — use cautiously.
            # Queued dispatches are not persisted across crashes.
            # If the target node already has an in-flight instance (DORMANT,
            # READY, or RUNNING), the dispatch queues in a per-node FIFO
            # instead of firing immediately. When the in-flight instance
            # completes, the next queued dispatch fires.
            if self._is_node_running(target):
                self._on_receive_queue.setdefault(target, deque()).append(
                    (source_instance, target, payload)
                )
            else:
                self._fire_on_receive(source_instance, target, payload)
        else:
            self._activated_sources.setdefault(target, set()).add(source_node_name)
            self._pending_dispatches.setdefault(target, {}).setdefault(source_node_name, []).append(
                payload
            )

    # ── Trigger mode helpers ────────────────────────────────────

    def _is_node_running(self, node_name: str) -> bool:
        """Return True if any instance of ``node_name`` is in-flight.

        An instance is in-flight from creation (DORMANT) through RUNNING —
        it leaves ``_active`` only after COMPLETED. This is the per-node
        serial gate for ON_RECEIVE: while any instance of the target node
        is in-flight, new ON_RECEIVE dispatches queue instead of firing.
        """
        return any(self._instances[iid].node_name == node_name for iid in self._active)

    def _fire_on_receive(
        self,
        source_instance: str,
        target: str,
        payload: dict[str, Any] | None,
    ) -> None:
        """Create a READY instance for an ON_RECEIVE dispatch.

        Resolves the source node name from ``source_instance``, extracts
        the delivered content from ``payload``, and creates a new instance
        with one ``IntegratedPayload`` for the downstream node's input
        integration. Called both for immediate fires (no in-flight
        instance) and for queued-dispatch drains.
        """
        source_instance_obj = self._instances[source_instance]
        source_node_name = source_instance_obj.node_name
        content = payload.get("delivered") if payload is not None else None
        upstream = [IntegratedPayload(source_node=source_node_name, content=content)]
        target_id = self._create_instance(target, upstream_payloads=upstream)
        self._mark_ready(target_id)

    def _drain_on_receive_queue(self, node_name: str) -> None:
        """After a node's instance completes, fire the next queued
        ON_RECEIVE dispatch (FIFO) for that node, if any.

        Dequeues one (source_instance, target, payload) tuple and fires
        it via ``_fire_on_receive``. The newly created instance becomes
        in-flight, so subsequent drains are deferred until it completes —
        this preserves the per-node serial execution invariant.
        """
        queue = self._on_receive_queue.get(node_name)
        if not queue:
            return
        source_instance, target, payload = queue.popleft()
        if not queue:
            self._on_receive_queue.pop(node_name, None)
        self._fire_on_receive(source_instance, target, payload)

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
                content = state_update.get("delivered") if state_update else None
                upstream_payloads.append(IntegratedPayload(source_node=source, content=content))
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
