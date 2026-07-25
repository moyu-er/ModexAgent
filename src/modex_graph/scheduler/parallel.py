"""``ParallelScheduler[S]`` — continuous multi-instance execution strategy.

Implements the continuous scheduling model (ADR-0034 D2): instances start as
independent ``asyncio.create_task`` coroutines the moment their dependencies
are satisfied — there is no batch barrier. Features generation-based conflict
detection, async checkpointing, and trigger-mode routing.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from typing import TYPE_CHECKING, Any, cast

from ..channel import LastValue
from ..checkpoint_store import CheckpointStore, MemoryCheckpointStore
from ..conflict_detector import GenerationWriteTracker, WriteConflictDetector
from ..constants import GraphNode, NodeInstanceStatus, NodeTrigger, SchedulerKind
from ..dispatch_store import DispatchStore, InMemoryDispatchStore
from ..exceptions import GraphRecursionError, RoutingError
from ..result import DispatchEvent, NodeResult
from .base import Scheduler
from .instance import NodeInstance

if TYPE_CHECKING:
    from ..checkpoint_store import CheckpointData
    from ..compiled_graph import CompiledGraph
    from ..context import GraphContext
    from ..state import GraphState


class ParallelScheduler[S: "GraphState"](Scheduler[S]):
    """Continuous multi-instance scheduler with conflict detection + checkpointing.

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

    **Checkpoint** (D19): after each instance merge, a background
    `asyncio.create_task` saves scheduler state to `CheckpointStore`.
    The save is non-blocking; failures are swallowed (logging can be
    added later).

    **Other features:**

    - `ctx.dispatch(target, state_update)` routing: validates target against
      the source node's outgoing edges, creates a `DispatchEvent`, and
      creates/queues the target instance.
    - `GraphNode.END` dispatch: terminal signal, does NOT create an instance.
    - `max_iterations`: global per-instance-execution counter; raises
      `GraphRecursionError` on overflow.
    - Routing compilation: after `node.execute` returns, `NodeResult` is
      compiled into `ctx.dispatch` calls.
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
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self.graph = graph
        # Reset at the top of each `run_async` call — stateless across calls.
        self._main_state: S | None = None
        self._instances: dict[str, NodeInstance[S]] = {}
        self._instance_seq: int = 0
        # Dispatch persistence (Task 09): store survives across runs; each
        # run gets a fresh run_id. The _dispatch_log property reads from the
        # store for backward compat with direct-access callers.
        self._dispatch_store: DispatchStore = (
            dispatch_store if dispatch_store is not None else InMemoryDispatchStore()
        )
        self._run_id: str | None = None
        self._active: set[str] = set()
        self._ready: set[str] = set()
        self._iteration_count: int = 0
        # Conflict detection (ADR-0034 D18): generation-based write tracking.
        # `_current_version` mirrors the detector's version via advance()'s
        # return value (the ABC doesn't expose current_version as a property).
        self._conflict_detector: WriteConflictDetector = (
            conflict_detector if conflict_detector is not None else GenerationWriteTracker()
        )
        self._current_version: int = 0
        # Checkpoint persistence (ADR-0034 D19): async save after each merge.
        self._checkpoint_store: CheckpointStore = (
            checkpoint_store if checkpoint_store is not None else MemoryCheckpointStore()
        )
        # ── Trigger mode state (Task 06) ──────────────────────────────────
        # Per-target activated sources: which source NODE NAMES have dispatched
        # to this target. A source is "activated" on first dispatch; it stays
        # activated for the rest of the run (used by ON_ALL_PREDS grouping).
        self._activated_sources: dict[str, set[str]] = {}
        # Per-target pending dispatch queues: target -> source -> [payloads].
        # ON_ALL_PREDS consumes one payload per source when firing a group.
        # ON_RECEIVE does not use this (instances are created immediately).
        self._pending_dispatches: dict[str, dict[str, list[dict[str, Any] | None]]] = {}
        self._wakeup: asyncio.Event | None = None
        self._checkpoint_tasks: set[asyncio.Task[None]] = set()

    @property
    def _dispatch_log(self) -> list[DispatchEvent]:
        """Backward-compat accessor: events for the current run from the store."""
        if self._run_id is None:
            return []
        return self._dispatch_store.query_all(self._run_id)

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

        Error handling (D13): if any instance raises, all remaining running
        tasks are cancelled and the exception propagates to the caller.

        Returns `ctx.state` (the shared `main_state`).
        """
        self._main_state = ctx.state
        self._instances = {}
        self._instance_seq = 0
        self._run_id = uuid.uuid4().hex
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

    # ── Instance lifecycle ────────────────────────────────────────────

    def _create_instance(self, node_name: str, initial_state: S | None = None) -> str:
        """Create a new `NodeInstance` for `node_name` in DORMANT status.

        Assigns the next global seq number and registers the instance in
        `_instances` and `_active`. Returns the `instance_id`.

        If ``initial_state`` is provided (e.g. from a ``Task.state``), it
        is stored on the instance as ``forked_state`` and used as the
        execution state instead of forking ``main_state``.

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
        compiled into ``ctx.dispatch`` calls, pending instances are
        re-checked, and an async checkpoint is scheduled.

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

        # Execute the node. Sync/async unified via inspect.isawaitable.
        # GraphBubbleUp exceptions propagate — NOT caught here.
        raw_result = node.execute(exec_ctx)
        if inspect.isawaitable(raw_result):
            result: NodeResult = await raw_result
        else:
            result = raw_result

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

        assert self._run_id is not None
        manual_dispatches = len(self._dispatch_store.query_by_source(instance_id, self._run_id))
        self._compile_routing(instance_id, exec_ctx, result, manual_dispatches)

        self._recheck_pending()

        self._schedule_checkpoint()

    # ── Routing compilation (Task 04) ──────────────────────────────────

    def _compile_routing(
        self,
        instance_id: str,
        ctx: GraphContext[S],
        result: NodeResult,
        manual_dispatches: int,
    ) -> None:
        """Compile `NodeResult.transition` / `Command.goto` into dispatches.

        Called after `node.execute` returns and `state_update` is applied.
        Manual dispatches made during `execute` are independent — both
        sets fire (not mutually exclusive).

        Priority (mirrors LinearScheduler D12, adapted for fan-out):

        1. `Command.goto` — `str` dispatches to one target; `list[Task]`
           dispatches to each Task's node (`Task.state` handling deferred
           to Task 05). Exclusive with transition.
        2. `transition` — dispatch to ALL matching static-edge targets
           (fan-out). No match → fall back to default edges. No default
           either → raise `RoutingError`.
        3. No transition, no Command — if no manual dispatch was made,
           dispatch to all default-edge targets; else silent skip.

        `NodeResult.state_update` is the payload for compiled dispatches;
        manual `ctx.dispatch` payloads are set by the caller.
        """
        instance = self._instances[instance_id]
        node_name = instance.node_name
        payload = result.state_update

        # 1. Command.goto (highest priority — exclusive with transition).
        if result.command is not None and result.command.goto is not None:
            goto = result.command.goto
            if isinstance(goto, str):
                ctx.dispatch(goto, state_update=payload)
            elif isinstance(goto, list):
                for task in goto:
                    if task.state is not None:
                        self._handle_dispatch(
                            instance_id, task.node, payload,
                            initial_state=cast("S | None", task.state),
                        )
                    else:
                        ctx.dispatch(task.node, state_update=payload)
            return

        # 2. transition — static edge fan-out.
        if result.transition is not None:
            targets = self.graph.next_nodes_by_transition(node_name, result.transition)
            if targets:
                for target in targets:
                    ctx.dispatch(target, state_update=payload)
                return
            default_targets = self.graph.default_edge_targets(node_name)
            if default_targets:
                for target in default_targets:
                    ctx.dispatch(target, state_update=payload)
                return
            raise RoutingError(
                f"No routing match from node {node_name!r}: "
                f"transition={result.transition!r} matched no static edge "
                f"and no default edge exists."
            )

        # 3. No transition, no Command — default edge fallback. Only
        # auto-dispatch defaults if the node didn't route manually.
        if manual_dispatches == 0:
            default_targets = self.graph.default_edge_targets(node_name)
            for target in default_targets:
                ctx.dispatch(target, state_update=payload)

    # ── Dispatch handling (Task 06: trigger modes) ────────────────────

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

        if target == GraphNode.END:
            return

        trigger = self._resolve_trigger(target)

        if trigger == NodeTrigger.ON_RECEIVE:
            # ON_RECEIVE (D4): create instance and mark READY immediately.
            # No reachability check, no PENDING state — the run_async loop
            # picks it up directly.
            target_id = self._create_instance(target, initial_state=initial_state)
            self._mark_ready(target_id)
        else:
            self._activated_sources.setdefault(target, set()).add(source_node_name)
            self._pending_dispatches.setdefault(target, {}).setdefault(
                source_node_name, []
            ).append(payload)

    # ── Trigger mode helpers (Task 06) ────────────────────────────────

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
        for source in list(pending.keys()):
            pending[source].clear()
        self._pending_dispatches.pop(target, None)
        self._activated_sources.pop(target, None)
        target_id = self._create_instance(target)
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

    # ── Checkpoint (ADR-0034 D19) ─────────────────────────────────────

    def _schedule_checkpoint(self) -> None:
        """Snapshot scheduler state synchronously, then save asynchronously.

        The ``CheckpointData`` is built NOW (synchronously, in the merge
        segment) so it captures the exact post-merge state — not a stale
        view from when the background task eventually runs. The async save
        is tracked in ``_checkpoint_tasks`` to prevent GC and cleaned up
        via a done-callback.
        """
        if self._run_id is None or self._main_state is None:
            return
        from ..checkpoint_store import CheckpointData, InstanceRecord

        data = CheckpointData(
            main_state=self._main_state.checkpoint(),
            pending_on_all_preds={
                tgt: {src: list(payloads) for src, payloads in queues.items()}
                for tgt, queues in self._pending_dispatches.items()
            },
            completed_instances=[
                InstanceRecord(
                    instance_id=iid,
                    node_name=inst.node_name,
                    fork_version=inst.fork_version,
                    status=inst.status,
                )
                for iid, inst in self._instances.items()
                if inst.status == NodeInstanceStatus.COMPLETED
            ],
            dispatch_events=self._dispatch_log,
        )
        run_id = self._run_id
        task = asyncio.create_task(self._save_checkpoint_async(data, run_id))
        self._checkpoint_tasks.add(task)
        task.add_done_callback(self._checkpoint_tasks.discard)

    async def _save_checkpoint_async(
        self, data: CheckpointData, run_id: str
    ) -> None:
        import logging

        try:
            await self._checkpoint_store.save(data, run_id)
        except Exception:
            logging.getLogger(__name__).warning(
                "Checkpoint save failed for run %s", run_id, exc_info=True
            )


__all__ = ["ParallelScheduler"]
