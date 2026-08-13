"""``ParallelScheduler[S]`` — continuous multi-instance execution strategy.

Implements the continuous scheduling model (ADR-0034 D2): instances start as
independent ``asyncio.create_task`` coroutines the moment their dependencies
are satisfied — there is no batch barrier. Features coordinator-driven
recovery and trigger-mode routing.
"""

from __future__ import annotations

import asyncio
from collections import deque
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
from ..execution_context import NodeExecution, reset_execution, set_execution
from ._dispatch_utils import route_deliver_from_dispatch, validate_dispatch_target
from .base import Scheduler
from .bootstrap import bootstrap
from .instance import NodeInstance

if TYPE_CHECKING:
    from ..compiled_graph import CompiledGraph
    from ..context import GraphContext
    from ..state import GraphState


class ParallelScheduler[S: "GraphState"](Scheduler[S]):
    """Continuous multi-instance scheduler with coordinator recovery.

    Implements the continuous scheduling model (ADR-0034 D2): instances
    start as independent `asyncio.create_task` coroutines the moment their
    dependencies are satisfied — there is no batch barrier. The scheduler
    waits for any task to complete via `asyncio.wait(FIRST_COMPLETED)`, then
    launches any newly-READY instances. Every instance shares `ctx.state`, so
    imperative state mutations are visible directly across concurrent tasks.

    **Recovery**: at the top of ``run_async``, ``bootstrap(ctx, graph)``
    restores ``ctx.state`` from the newest full snapshot, auto-promotes
    CONSUMED_PENDING delivers, and returns seed node names (CRASHED /
    RUNNING nodes + nodes with PENDING delivers, BFS-ordered). Re-execute
    seeds are marked READY immediately; PENDING-deliver seeds are
    discovered by ``_recheck_pending``'s store scan.

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
        # Per-node FIFO of queued ON_RECEIVE targets waiting for the node's
        # current in-flight instance to complete. In-memory only.
        self._on_receive_queue: dict[str, deque[str]] = {}
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

        Recovery: at the top, `bootstrap(ctx, graph)` produces seed node
        names. Re-execute seeds (CRASHED/RUNNING) are marked READY; fresh
        start creates the entry instance. PENDING-deliver seeds are left
        for `_recheck_pending`'s store scan to discover and create
        instances for.

        Error handling (D13): if any instance raises, all remaining running
        tasks are cancelled and the exception propagates to the caller.

        Returns the shared `ctx.state`.
        """
        self._ctx = ctx
        ctx.scheduler_kind = SchedulerKind.PARALLEL
        ctx.set_dispatch_handler(self._handle_dispatch)
        self._wakeup = asyncio.Event()

        # Reset in-memory scheduler state.
        self._instances = {}
        self._active = set()
        self._ready = set()
        self._activated_sources = {}
        self._pending_dispatches = {}
        self._on_receive_queue = {}
        self._scheduled_deliver_ids = set()
        self._iteration_count = 0
        self._instance_seq = 0

        # Unified bootstrap: query store -> produce seed node names.
        # Restores ctx.state and auto-promotes CONSUMED_PENDING delivers.
        seeds = bootstrap(ctx, self.graph)

        # Mark re-execute seeds (CRASHED/RUNNING) and entry_node as READY.
        # entry_node is always READY when present in seeds (bootstrap puts
        # it there for fresh starts and re-invocations). Other seeds are
        # READY only if their latest invocation is CRASHED or RUNNING.
        # PENDING-deliver seeds are left for _recheck_pending's store scan.
        for seed_name in seeds:
            node = self.graph.nodes[seed_name]
            record = ctx.coordinator.node_state_store.load_latest(node.node_id)
            if seed_name == self.graph.entry_node or (
                record is not None
                and record.status in (
                    InvocationStatus.CRASHED,
                    InvocationStatus.RUNNING,
                )
            ):
                iid = self._create_instance(seed_name)
                self._mark_ready(iid)

        # Discover PENDING delivers from the store and create/queue instances.
        # Handles PENDING-deliver seeds not marked READY above, including the
        # case where only PENDING-deliver seeds exist (no re-execute seeds).
        self._recheck_pending()

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

        return ctx.state

    async def _wait_for_wakeup(self) -> None:
        if self._wakeup is not None:
            await self._wakeup.wait()

    # ── Instance lifecycle ────────────────────────────────────────────

    def _create_instance(self, node_name: str) -> str:
        """Create a new `NodeInstance` for `node_name` in DORMANT status.

        Assigns the next global seq number and registers the instance in
        `_instances` and `_active`. Returns the `instance_id`.

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

        Uses the shared ctx directly — state isolation is via per-node
        scratchpad keys (node_scratch), not context copying. Invocation-local
        identity is set via the ContextVar-based execution context
        (``set_execution``). After ``after_node``, pending instances are
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
        self._iteration_count += 1

        instance.status = NodeInstanceStatus.RUNNING

        node = self.graph.nodes[instance.node_name]

        node_exec = NodeExecution(instance_id=instance_id)
        exec_token = set_execution(node_exec)

        try:
            await ctx.runtime.before_node(ctx, instance.node_name)
            await node.run(ctx, graph=self.graph)
            await ctx.runtime.after_node(ctx, instance.node_name)
        finally:
            reset_execution(exec_token)

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
              AND reachability is clear, consume all pending dispatches
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

        validate_dispatch_target(self.graph, source_node_name, target)

        # Route deliver to target node's deliver_store via coordinator.
        assert self._ctx is not None
        deliver_id = route_deliver_from_dispatch(
            self._ctx, self.graph, source_node_name, target, payload
        )
        if deliver_id is not None:
            self._scheduled_deliver_ids.add(deliver_id)

        trigger = self._resolve_trigger(target)

        if trigger == NodeTrigger.ON_RECEIVE:
            # ON_RECEIVE dispatches are serialized per-node — use cautiously.
            # Queued dispatches are not persisted across crashes.
            # If the target node already has an in-flight instance (DORMANT,
            # READY, or RUNNING), the dispatch queues in a per-node FIFO
            # instead of firing immediately. When the in-flight instance
            # completes, the next queued dispatch fires.
            if self._is_node_running(target):
                self._on_receive_queue.setdefault(target, deque()).append(target)
            else:
                self._fire_on_receive(target)
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

    def _fire_on_receive(self, target: str) -> None:
        """Create a READY instance for an ON_RECEIVE dispatch.

        Called both for immediate fires and queued-dispatch drains. Input
        payloads flow through the coordinator's deliver store.
        """
        target_id = self._create_instance(target)
        self._mark_ready(target_id)

    def _drain_on_receive_queue(self, node_name: str) -> None:
        """After a node's instance completes, fire the next queued
        ON_RECEIVE dispatch (FIFO) for that node, if any.

        Dequeues one target and fires it via ``_fire_on_receive``. The newly created instance becomes
        in-flight, so subsequent drains are deferred until it completes —
        this preserves the per-node serial execution invariant.
        """
        queue = self._on_receive_queue.get(node_name)
        if not queue:
            return
        target = queue.popleft()
        if not queue:
            self._on_receive_queue.pop(node_name, None)
        self._fire_on_receive(target)

    def _resolve_trigger(self, node_name: str) -> NodeTrigger:
        node = self.graph.nodes.get(node_name)
        if node is not None and node.trigger is not None:
            return node.trigger
        return self.graph.default_trigger

    def _can_reach_active(self, target: str, *, exclude: str | None = None) -> bool:
        """BFS from all PENDING/READY/RUNNING instances (excluding `exclude`),
        all pending-dispatch targets (ON_ALL_PREDS nodes with queued
        dispatches that will become future instances), and all nodes with
        unconsumed PENDING delivers in their DeliverStore, along declared
        outgoing edges. Returns True if any can reach `target`.

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

        Nodes with unconsumed PENDING delivers are included because those
        delivers represent future work not yet consumed by any invocation.
        Without this start source, fan-in closure fails when workers complete
        sequentially — Worker#1 completes but Worker#2/#3 haven't been
        created yet (their items are still PENDING in the DeliverStore),
        causing Reduce to fire prematurely with partial results.
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

        # Third start source: nodes with unconsumed PENDING delivers.
        # When a worker has PENDING items in its DeliverStore (not yet consumed
        # by any invocation), those items represent future work that may
        # eventually deliver to downstream targets. The BFS must see these
        # nodes as reachable sources to prevent premature fan-in firing.
        if self._ctx is not None:
            coordinator = self._ctx.coordinator
            for node_name, node in self.graph.nodes.items():
                if node_name in (GraphNode.START, GraphNode.END):
                    continue
                if node_name == target:
                    continue
                if node_name in start_nodes:
                    continue
                delivers = coordinator.collect_consumable_delivers(node.node_id, 0)
                if any(d.status == DeliverConsumptionStatus.PENDING for d in delivers):
                    start_nodes.add(node_name)

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
        dispatch AND the per-node serial gate is clear (no in-flight
        instance of the same Node object) AND reachability is clear.
        Consumes ALL pending dispatches for this target (not just one
        per source) — the node fires once per "all sources have
        dispatched" event, not once per dispatch.

        The per-node serial gate (`_is_node_running`) enforces the same
        invariant as ON_RECEIVE: the same Node object never executes
        concurrently. Without this check, a second ON_ALL_PREDS group
        could fire while the first instance is still RUNNING, racing
        `_pending_delivers` / `_submit_result` / `_graph_ref` /
        `node_scratch[self.node_id]`. When the gate blocks, pending
        dispatches stay queued and are retried via `_recheck_pending`
        after the in-flight instance completes.
        """
        activated = self._activated_sources.get(target)
        if not activated:
            return
        pending = self._pending_dispatches.get(target, {})
        for source in activated:
            if not pending.get(source):
                return
        if self._is_node_running(target):
            return
        if self._can_reach_active(target):
            return
        self._pending_dispatches.pop(target, None)
        self._activated_sources.pop(target, None)
        target_id = self._create_instance(target)
        self._mark_ready(target_id)

    def _recheck_pending(self) -> None:
        """Re-check ON_ALL_PREDS queues and scan deliver store for PENDING delivers.

        Unified admission: both live dispatch (_handle_dispatch) and recovery/pending
        delivers flow through this single method. Live dispatch updates in-memory
        queues; this method also scans the persisted store for PENDING delivers
        that need instance creation (recovery path and any straggler delivers).
        """
        if self._ctx is None:
            return
        coordinator = self._ctx.coordinator

        node_names_by_id = {node.node_id: name for name, node in self.graph.nodes.items()}
        for node_name, node in self.graph.nodes.items():
            if node_name in (GraphNode.START, GraphNode.END):
                continue
            delivers = coordinator.collect_consumable_delivers(node.node_id, 0)
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
                if not self._is_node_running(node_name):
                    target_id = self._create_instance(node_name)
                    self._mark_ready(target_id)
                    self._scheduled_deliver_ids.update(d.deliver_id for d in pending)
            else:
                for deliver in pending:
                    source_node_name = node_names_by_id.get(
                        deliver.source_node_id, deliver.source_node_id
                    )
                    payload: dict[str, Any] | None = {
                        "delivered": deliver.content,
                        "_source_node": deliver.source_node_id,
                    }
                    self._activated_sources.setdefault(node_name, set()).add(source_node_name)
                    self._pending_dispatches.setdefault(node_name, {}).setdefault(
                        source_node_name, []
                    ).append(payload)
                    self._scheduled_deliver_ids.add(deliver.deliver_id)

        for tgt in list(self._pending_dispatches.keys()):
            self._try_fire_on_all_preds(tgt)


__all__ = ["ParallelScheduler"]
